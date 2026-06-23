"""CDPSession: per-target DuckDB event store.

Owns an in-memory DuckDB. Events are inserted synchronously on the asyncio
event loop (microsecond inserts). FIFO pruning at 50k events.
"""

import asyncio
import json
import logging
import re
from typing import Any

from .har import _create_views

__all__ = ["CDPSession"]

logger = logging.getLogger(__name__)

# Event storage limits
MAX_EVENTS = 50_000
PRUNE_BATCH_SIZE = 5_000
PRUNE_CHECK_INTERVAL = 1_000

# ---------------------------------------------------------------------------
# Console error suppress + cross-tab dedup
# ---------------------------------------------------------------------------

_suppress_patterns: set[str] = set()

_DEDUP_WINDOW = 2.0  # seconds


class _DedupEntry:
    __slots__ = ("count", "text", "meta", "handle")

    def __init__(self, text: str, meta: dict, handle: object):
        self.count = 0
        self.text = text
        self.meta = meta
        self.handle = handle


_dedup_pending: dict[str, _DedupEntry] = {}


def _is_suppressed(text: str) -> bool:
    return any(pat in text for pat in _suppress_patterns)


def _flush_dedup(key: str) -> None:
    entry = _dedup_pending.pop(key, None)
    if entry is None or entry.count == 0:
        return
    try:
        from ..kernel import push_channel

        total = entry.count + 1
        push_channel(
            f"{entry.text} (×{total} tabs)",
            entry.meta,
        )
    except Exception:
        pass


def _dedup_push(
    text: str,
    meta: dict,
    dedup_key: str,
    loop: asyncio.AbstractEventLoop | None,
) -> None:
    if dedup_key in _dedup_pending:
        _dedup_pending[dedup_key].count += 1
        return

    try:
        from ..kernel import push_channel

        push_channel(text, meta)
    except Exception:
        return

    handle = loop.call_later(_DEDUP_WINDOW, _flush_dedup, dedup_key) if loop else None
    _dedup_pending[dedup_key] = _DedupEntry(text, meta, handle)

# Regex to strip lone surrogates from JSON strings (DuckDB rejects \uD800-\uDFFF)
# Keeps valid surrogate pairs intact (high \uD800-\uDBFF followed by low \uDC00-\uDFFF)
_LONE_HIGH_SURROGATE = re.compile(
    r"(?<!\\)\\u[dD][89aAbB][0-9a-fA-F]{2}(?!\\u[dD][cCdDeEfF][0-9a-fA-F]{2})"
)
_LONE_LOW_SURROGATE = re.compile(
    r"(?<!\\u[dD][89aAbB][0-9a-fA-F]{2})(?<!\\)\\u[dD][cCdDeEfF][0-9a-fA-F]{2}"
)


def _json_dumps_safe(data: Any) -> str:
    """json.dumps with lone surrogate sanitization for DuckDB compatibility."""
    s = json.dumps(data)
    if "\\ud" in s or "\\uD" in s:
        s = _LONE_HIGH_SURROGATE.sub("", s)
        s = _LONE_LOW_SURROGATE.sub("", s)
    return s


_CONTROLS_PREFIX = "__controls__"


def _check_controls_observation(params: dict, target_id: str) -> None:
    """Detect __controls__ console.debug messages and push as channel notifications."""
    args = params.get("args", [])
    if not args or params.get("type") != "debug":
        return
    first = args[0].get("value", "")
    if first != _CONTROLS_PREFIX or len(args) < 2:
        return
    raw = args[1].get("value", "")
    try:
        obs = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return
    control = obs.get("control", "?")
    action = obs.get("action", "?")
    summary = obs.get("summary", f"{control}.{action}()")
    before = obs.get("stateBefore", "")
    after = obs.get("stateAfter", "")
    duration = obs.get("duration", 0)
    error = obs.get("error")
    line = f"[controls] {summary}"
    if before != after:
        line += f" — state: {before!r} → {after!r}"
    if duration:
        line += f" ({duration}ms)"
    if error:
        line += f" ERROR: {error}"
    try:
        from ..kernel import push_channel

        meta: dict[str, str] = {
            "kind": "controls",
            "control": control,
            "action": action,
            "target": target_id,
        }
        if before:
            meta["stateBefore"] = before
        if after:
            meta["stateAfter"] = after
        push_channel(line, meta)
    except Exception:
        pass


def _push_console_error(
    params: dict,
    target_id: str,
    port: int,
    loop: asyncio.AbstractEventLoop | None = None,
) -> None:
    """Push console.error messages as channel notifications."""
    args = params.get("args", [])
    parts = []
    for a in args:
        val = (
            a.get("value")
            or a.get("description")
            or a.get("preview", {}).get("description", "")
        )
        if val:
            parts.append(str(val))
    text = " ".join(parts)[:300]
    if not text or _is_suppressed(text):
        return
    short_id = f"{port}:{target_id[:6]}"
    _dedup_push(
        f"[console:error] {short_id}: {text}",
        {"kind": "console_error", "target": short_id},
        text[:100],
        loop,
    )


def _push_exception(
    params: dict,
    target_id: str,
    port: int,
    loop: asyncio.AbstractEventLoop | None = None,
) -> None:
    """Push uncaught exceptions as channel notifications."""
    details = params.get("exceptionDetails", {})
    exc = details.get("exception", {})
    text = exc.get("description") or details.get("text", "")
    text = text[:300]
    if not text or _is_suppressed(text):
        return
    short_id = f"{port}:{target_id[:6]}"
    _dedup_push(
        f"[console:error] {short_id}: {text}",
        {"kind": "console_error", "target": short_id},
        text[:100],
        loop,
    )


class CDPSession:
    """Session-multiplexed CDP client with in-memory DuckDB event storage.

    Routes commands through BrowserSession WebSocket. Stores CDP events
    as-is in DuckDB for minimal overhead and maximum flexibility.
    """

    def __init__(
        self,
        send: Any,  # BrowserSession.execute bound method
        session_id: str,
        target_info: dict,
        port: int,
        loop: asyncio.AbstractEventLoop | None = None,
        send_nowait: Any = None,  # BrowserSession.send_nowait bound method
    ) -> None:
        self._send = send
        self._send_nowait = send_nowait or send
        self._loop = loop
        self._session_id = session_id
        self.target_info = target_info
        self.port = port
        self.chrome_target_id = target_info.get("targetId", "")

        # In-memory DuckDB — single writer on the asyncio event loop (no threads)
        import duckdb

        self.db = duckdb.connect(":memory:")
        self._setup_schema()

        # Event count for FIFO pruning
        self._event_count = 0

        # Paused-request counter (Fetch.requestPaused increments, continue decrements)
        self.paused_count = 0

        # Fetch body capture state.  False by default — enabled by get()/open(),
        # or explicitly via tab.capture_bodies = True / tab.enable_capture().
        self.capture_bodies: bool = False
        self._fetch_enabled: bool = False

        # Fetch handler (set by capture.enable, cleared by capture.disable)
        self._fetch_handler: Any | None = None

        # Binding handler (set by Tab._setup_binding)
        self._binding_handler: Any | None = None

    def _setup_schema(self) -> None:
        """Create events table, indexes, and HAR views."""
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS events (
                event JSON,
                method VARCHAR,
                request_id VARCHAR,
                target VARCHAR
            )"""
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_method ON events(method)"
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_request_id ON events(request_id)"
        )
        _create_views(self.db.execute)

    async def _enable_domains(self) -> None:
        """Enable required CDP domains on attach.

        Fire-and-forget — all enables are sent over the single multiplexed
        WebSocket without awaiting Chrome's ack.  This keeps attach near-instant
        even with many concurrent tabs.  Fetch interception is separate
        (enable_fetch) and triggered by get()/open() or tab.capture_bodies = True.
        """
        for method in (
            "Inspector.enable",
            "DOM.enable",
            "Page.enable",
            "Network.enable",
            "Runtime.enable",
            "Log.enable",
            "Accessibility.enable",
            "Page.setLifecycleEventsEnabled",
        ):
            try:
                params = (
                    {"enabled": True}
                    if method == "Page.setLifecycleEventsEnabled"
                    else None
                )
                await self.send_nowait(method, params)
            except Exception as exc:
                logger.debug("Domain enable %s: %s", method, exc)

    async def enable_fetch(self) -> None:
        """Enable Fetch body capture on this session. Idempotent."""
        if self._fetch_enabled:
            return
        from .capture import enable as fetch_enable

        await fetch_enable(self)
        self.capture_bodies = True
        self._fetch_enabled = True

    async def disable_fetch(self) -> None:
        """Disable Fetch body capture on this session. Idempotent."""
        if not self._fetch_enabled:
            return
        from .capture import disable as fetch_disable

        self.capture_bodies = False
        await fetch_disable(self)
        self._fetch_enabled = False

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    async def execute(
        self,
        method: str,
        params: dict | None = None,
        timeout: float = 30,
    ) -> dict:
        """Execute a CDP command on this session."""
        return await self._send(method, params, self._session_id, timeout)

    async def send_nowait(
        self,
        method: str,
        params: dict | None = None,
    ) -> None:
        """Send a CDP command without awaiting the response."""
        return await self._send_nowait(method, params, self._session_id)

    # ------------------------------------------------------------------
    # Event handling (sync — called from recv loop on asyncio thread)
    # ------------------------------------------------------------------

    def _handle_event(self, data: dict) -> None:
        """Store event in DuckDB and dispatch Fetch handler if needed."""
        try:
            method = data.get("method", "")
            params = data.get("params", {})
            request_id = params.get("requestId") or params.get("networkId")

            target_id = self.target_info.get("targetId", "")

            # Synchronous insert — microseconds, no contention
            self.db.execute(
                "INSERT INTO events (event, method, request_id, target) VALUES (?, ?, ?, ?)",
                [_json_dumps_safe(data), method, request_id, target_id],
            )
            self._event_count += 1

            if method == "Fetch.requestPaused":
                self.paused_count += 1
                # Dispatch async handler without blocking the recv loop
                if self._fetch_handler is not None:
                    asyncio.create_task(
                        self._fetch_handler(self, params),
                        name=f"repld-fetch-{params.get('requestId', '?')[:8]}",
                    )

            if method == "Runtime.bindingCalled":
                if self._binding_handler is not None:
                    asyncio.create_task(
                        self._binding_handler(self, params),
                        name=f"repld-binding-{params.get('name', '?')}",
                    )

            if method == "Runtime.consoleAPICalled":
                _check_controls_observation(params, target_id)
                if params.get("type") == "error":
                    _push_console_error(params, target_id, self.port, self._loop)

            if method == "Runtime.exceptionThrown":
                _push_exception(params, target_id, self.port, self._loop)

            # Periodic pruning — async task to avoid blocking the recv loop
            if self._event_count % PRUNE_CHECK_INTERVAL == 0:
                if self._event_count > MAX_EVENTS:
                    asyncio.create_task(self._async_prune(), name="repld-prune")

        except Exception as exc:
            logger.debug("_handle_event error: %s", exc)

    async def _async_prune(self) -> None:
        """FIFO prune oldest events. Runs as a task to avoid blocking recv."""
        excess = self._event_count - MAX_EVENTS
        delete_count = max(excess, PRUNE_BATCH_SIZE)
        try:
            self.db.execute(
                "DELETE FROM events WHERE rowid IN "
                "(SELECT rowid FROM events ORDER BY rowid LIMIT ?)",
                [delete_count],
            )
            row = self.db.execute("SELECT COUNT(*) FROM events").fetchone()
            self._event_count = row[0] if row else 0
            logger.debug(
                "Pruned %d events; %d remaining", delete_count, self._event_count
            )
        except Exception as exc:
            logger.debug("Prune error: %s", exc)

    # ------------------------------------------------------------------
    # Query interface
    # ------------------------------------------------------------------

    def query(self, sql: str, params: list | None = None) -> list:
        """Execute arbitrary SQL against the events DB."""
        if params:
            return self.db.execute(sql, params).fetchall()
        return self.db.execute(sql).fetchall()

    def fetch_body(self, request_id: str) -> dict:
        """Return captured body; fall back to Network.getResponseBody CDP call."""
        # Check DuckDB for captured body first
        try:
            rows = self.db.execute(
                """
                SELECT
                    json_extract_string(event, '$.params.body') as body,
                    json_extract(event, '$.params.base64Encoded') as b64,
                    json_extract(event, '$.params.capture') as capture
                FROM events
                WHERE method = 'Network.responseBodyCaptured'
                  AND request_id = ?
                LIMIT 1
                """,
                [request_id],
            ).fetchall()
            if rows:
                body, b64, capture_json = rows[0]
                capture = json.loads(capture_json) if capture_json else None
                if capture and not capture.get("ok"):
                    return {
                        "error": capture.get("error", "capture failed"),
                        "capture": capture,
                    }
                result: dict = {"body": body, "base64Encoded": b64 in ("true", True)}
                if capture:
                    result["capture"] = capture
                return result
        except Exception as exc:
            logger.debug("fetch_body DB check %s: %s", request_id, exc)

        # Synchronous CDP fallback — must be called from a thread, not the event loop.
        # self._loop is set at construction time by BrowserSession.attach().
        if self._loop is None:
            raise RuntimeError(
                "CDPSession has no event loop — cannot fetch body via CDP"
            )
        fut = asyncio.run_coroutine_threadsafe(
            self.execute("Network.getResponseBody", {"requestId": request_id}),
            self._loop,
        )
        try:
            return fut.result(timeout=10)
        except Exception as exc:
            return {"error": str(exc)}

    def clear_events(self) -> None:
        """Delete all stored events."""
        self.db.execute("DELETE FROM events")
        self._event_count = 0

    def cleanup(self) -> None:
        """Close the DuckDB connection."""
        try:
            self.db.close()
        except Exception:
            pass
