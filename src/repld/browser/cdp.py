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
    ) -> None:
        self._send = send
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

        # Whether to capture response/request bodies via Fetch
        self.capture_bodies: bool = True

        # Fetch handler (set by FetchCapture.enable)
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
        """Enable required CDP domains on attach."""
        from .capture import enable as fetch_enable

        for method in (
            "Page.enable",
            "Network.enable",
            "Runtime.enable",
            "Log.enable",
            "Accessibility.enable",
        ):
            try:
                await self.execute(method)
            except Exception as exc:
                logger.debug("Domain enable %s: %s", method, exc)

        # Enable Fetch body capture
        if self.capture_bodies:
            try:
                await fetch_enable(self)
            except Exception as exc:
                logger.debug("Fetch.enable: %s", exc)

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

            # Periodic pruning
            if self._event_count % PRUNE_CHECK_INTERVAL == 0:
                self._prune_if_needed()

        except Exception as exc:
            logger.debug("_handle_event error: %s", exc)

    def _prune_if_needed(self) -> None:
        """FIFO prune if event count exceeds MAX_EVENTS."""
        if self._event_count <= MAX_EVENTS:
            return
        excess = self._event_count - MAX_EVENTS
        delete_count = max(excess, PRUNE_BATCH_SIZE)
        try:
            self.db.execute(
                "DELETE FROM events WHERE rowid IN "
                "(SELECT rowid FROM events ORDER BY rowid LIMIT ?)",
                [delete_count],
            )
            self._event_count -= delete_count
            logger.debug(
                "Pruned %d events; ~%d remaining", delete_count, self._event_count
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
