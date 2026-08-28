"""CDPSession: per-target DuckDB event store.

Owns an in-memory DuckDB. Events are inserted synchronously on the asyncio
event loop (microsecond inserts). FIFO pruning at 50k events.
"""

import asyncio
import contextlib
import json
import logging
import re
import threading
import time
from typing import Any

from .. import bg
from ..channel import push_channel
from .har import _create_views

__all__ = ["CDPSession"]

logger = logging.getLogger(__name__)

# Event storage limits
MAX_EVENTS = 50_000
PRUNE_BATCH_SIZE = 5_000
PRUNE_CHECK_INTERVAL = 1_000

# Backstop for requests that never report a terminal event. The bound that
# matters is the longest deadline any *caller* gives settle, not the 5s default
# on `observe.settle` itself — nothing passes that. The real ceiling is
# `browser_dispatch._SETTLE_NAVIGATION_S` (8s, for navigate/open); this sits
# well clear of it, so an entry is only ever evicted long after any settle that
# could legitimately have been waiting on it. Raise it if that ceiling rises.
_INFLIGHT_MAX_AGE = 60.0

# Response mime types whose body stays open by design: the response has
# arrived, and waiting for `loadingFinished` means waiting for the stream to
# end. Matched against Network.responseReceived, where Chrome has already
# parsed the header for us.
_STREAMING_MIME_TYPES = ("text/event-stream",)


def _is_streaming(params: dict) -> bool:
    mime = (params.get("response") or {}).get("mimeType") or ""
    return mime.split(";")[0].strip().lower() in _STREAMING_MIME_TYPES


def _on_loop(loop: asyncio.AbstractEventLoop | None) -> bool:
    """Whether the calling thread is running *loop* right now.

    Half this class is reachable from both sides — `browser_dispatch` answers
    some tools on the IPC reader thread, `runtime._eval` puts every pure-sync
    exec cell in `asyncio.to_thread`, and the recv handler runs on the loop —
    so "am I on the loop" is a question two methods here have to ask before
    deciding how to reach it.
    """
    if loop is None:
        return False
    try:
        return asyncio.get_running_loop() is loop
    except RuntimeError:
        return False


# ---------------------------------------------------------------------------
# Console error suppress + cross-tab dedup
# ---------------------------------------------------------------------------

_suppress_patterns: set[str] = set()

# URL globs exempted from Fetch-domain body capture (browser.no_capture) —
# Chrome's Fetch.continueResponse/fulfillRequest replay path applies CORB/ORB
# more strictly than a natively-fetched response, which false-positives as a
# CORS block on same-origin resources from servers with no CORS/CORP headers
# (old embedded-device admin UIs are the common case).
_no_capture_patterns: set[str] = set()

_DEDUP_WINDOW = 2.0  # seconds — collapse rapid bursts
_HINT_WINDOW = 30.0  # seconds — track recurring errors for suppress hint
_HINT_THRESHOLD = 3  # show hint after this many pushes in the hint window


class _DedupEntry:
    __slots__ = ("count", "text", "meta")

    def __init__(self, text: str, meta: dict):
        self.count = 0
        self.text = text
        self.meta = meta


_dedup_pending: dict[str, _DedupEntry] = {}


# key → how many times this error has been seen inside `_HINT_WINDOW`. A
# `__slots__` class wrapping the one int bought nothing: the counter is only
# ever incremented and compared, and `_flush_dedup` had to build a throwaway
# instance just to serve as a `dict.get` default.
_hint_counts: dict[str, int] = {}


def _expire_hint(key: str) -> None:
    _hint_counts.pop(key, None)


def _track_hint(key: str, loop: asyncio.AbstractEventLoop) -> bool:
    """Increment hint counter, return True if hint should be shown."""
    if key not in _hint_counts:
        loop.call_later(_HINT_WINDOW, _expire_hint, key)
        _hint_counts[key] = 0
    _hint_counts[key] += 1
    return _hint_counts[key] >= _HINT_THRESHOLD


_SUPPRESS_HINT = ' — browser.suppress("...") to mute'


def _is_suppressed(text: str) -> bool:
    return any(pat in text for pat in _suppress_patterns)


def _flush_dedup(key: str) -> None:
    entry = _dedup_pending.pop(key, None)
    if entry is None or entry.count == 0:
        return
    try:
        total = entry.count + 1
        msg = f"{entry.text} (×{total} tabs)"
        if _hint_counts.get(key, 0) >= _HINT_THRESHOLD:
            msg += _SUPPRESS_HINT
        push_channel(msg, entry.meta)
    except Exception:
        pass


def _dedup_push(
    text: str,
    meta: dict,
    dedup_key: str,
    loop: asyncio.AbstractEventLoop | None,
) -> None:
    if loop is None:
        # Both windows below are opened by registering a dict entry and closed
        # only by a `call_later` callback. With no loop to schedule one, the
        # entry would never be flushed or expired, and since the first branch
        # collapses into an existing entry and returns, this would be the last
        # time this error text was ever reported — silence, from an error
        # reporter, for the rest of the session. Degrade to no dedup instead:
        # noisier is the right direction to fail in here.
        #
        # Unreachable today (BrowserSession.attach always passes the running
        # loop), which is exactly why it is spelled out rather than left as a
        # falsy check that reads like it was considered.
        try:
            push_channel(text, meta)
        except Exception:
            pass
        return

    if dedup_key in _dedup_pending:
        _dedup_pending[dedup_key].count += 1
        _track_hint(dedup_key, loop)
        return

    try:
        show_hint = _track_hint(dedup_key, loop)
        msg = text + _SUPPRESS_HINT if show_hint else text
        push_channel(msg, meta)
    except Exception:
        return

    loop.call_later(_DEDUP_WINDOW, _flush_dedup, dedup_key)
    _dedup_pending[dedup_key] = _DedupEntry(text, meta)


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

# Per-session sequence for the injected engine's frameSeq option: it prefixes
# every aria-ref (f<seq>eN), so refs from different sessions (a tab and its
# OOPIF iframe children) never collide in a composed snapshot.
_frame_seq_counter = iter(range(1, 1 << 30))


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


def _push_error_text(
    text: str,
    target_id: str,
    port: int,
    loop: asyncio.AbstractEventLoop | None,
) -> None:
    """Suppression-check, truncate, and dedup-push a console error line."""
    text = text[:300]
    if not text or _is_suppressed(text):
        return
    short_id = f"{port}:{target_id[:6].lower()}"
    _dedup_push(
        f"[console:error] {short_id}: {text}",
        {"kind": "console_error", "target": short_id},
        text[:100],
        loop,
    )


def _push_console_error(
    params: dict,
    target_id: str,
    port: int,
    loop: asyncio.AbstractEventLoop | None = None,
) -> None:
    """Push console.error messages as channel notifications."""
    parts = []
    for a in params.get("args", []):
        val = (
            a.get("value")
            or a.get("description")
            or a.get("preview", {}).get("description", "")
        )
        if val:
            parts.append(str(val))
    _push_error_text(" ".join(parts), target_id, port, loop)


def _push_exception(
    params: dict,
    target_id: str,
    port: int,
    loop: asyncio.AbstractEventLoop | None = None,
) -> None:
    """Push uncaught exceptions as channel notifications."""
    details = params.get("exceptionDetails", {})
    exc = details.get("exception", {})
    _push_error_text(
        exc.get("description") or details.get("text", ""), target_id, port, loop
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
        browser_session: Any = None,  # owning BrowserSession, for re-attach
    ) -> None:
        self._send = send
        self._send_nowait = send_nowait or send
        self._loop = loop
        self.browser_session = browser_session
        self._session_id = session_id
        self.target_info = target_info
        self.port = port
        self.chrome_target_id = target_info.get("targetId", "")

        # In-memory DuckDB.  The main connection is written only from the
        # asyncio loop thread (store_event/_async_prune); query/fetch_body/
        # clear_events may run on IPC reader threads, so they open a
        # per-call cursor — a duplicated connection sharing this in-memory
        # DB, DuckDB's sanctioned cross-thread pattern (MVCC isolates
        # readers from the loop-thread writer).
        import duckdb

        self.db = duckdb.connect(":memory:")
        self._setup_schema()

        # Guards the per-call cursors against `cleanup()` closing the
        # connection under them. The read side got the cross-thread treatment
        # and the close did not: `cleanup()` runs on the loop (disconnect,
        # _reconnect, Target.targetDestroyed) while `query`/`query_dicts`/
        # `clear_events`/`fetch_body` are off-loop by design, so a tab closing
        # mid-query made `self.db.cursor()` raise a bare DuckDB "connection
        # already closed" that reached the agent as `browser_network:
        # Connection Error`. See `_cursor`.
        self._db_lock = threading.Lock()
        self._db_closed = False

        # Event count for FIFO pruning, maintained by store_event so it covers
        # *every* insert. It used to be incremented in _handle_event instead,
        # which meant capture.py's synthetic rows (Network.requestBodyCaptured
        # / responseBodyCaptured, written by calling store_event directly) were
        # the only ones never counted — and those are the rows that dominate
        # the store's memory, at up to _MAX_BODY_SIZE each.
        self._event_count = 0
        # Next count at which _handle_event checks whether a prune is due. A
        # threshold rather than `_event_count % PRUNE_CHECK_INTERVAL == 0`:
        # body captures now advance the counter too, so it can step by more
        # than one between checks and skip an exact multiple entirely.
        self._next_prune_check = PRUNE_CHECK_INTERVAL

        # In-flight requestIds -> monotonic time they were opened. Maintained
        # by _handle_event, read by observe.settle() via inflight_count().
        # Keyed by id (not a counter) so it stays self-correcting: redirect
        # re-emits are idempotent, terminal events for pre-attach requests are
        # no-op discards.  WS never enters (no Network.requestWillBeSent for
        # WebSockets).  The timestamp is what lets inflight_count() age out a
        # response whose body never ends — see _INFLIGHT_MAX_AGE.
        self._inflight: dict[str, float] = {}

        # Serializes state-preserving reattach (BrowserSession.reattach_session)
        self._reattach_lock = asyncio.Lock()

        # Fetch body capture state.  False by default — enabled by get()/open(),
        # or explicitly via tab.capture_bodies = True / tab.enable_capture().
        self.capture_bodies: bool = False
        self._fetch_enabled: bool = False
        # Serializes enable_fetch/disable_fetch. `Tab.capture_bodies`'s setter
        # is fire-and-forget (it schedules a task and returns), so a
        # True-then-False pair races: both claim their flag before awaiting,
        # then interleave, and the pair could settle as capture_bodies=True with
        # Fetch actually disabled. Under the lock the last call issued is the
        # one that decides.
        self._fetch_lock = asyncio.Lock()

        # Fetch handler (set by capture.enable, cleared by capture.disable)
        self._fetch_handler: Any | None = None

        # Binding handler (set by Tab._setup_binding)
        self._binding_handler: Any | None = None

        # Pin + label state — lives here (not on Tab) because Tab wrappers
        # are ephemeral (a fresh Tab is constructed on every _iter_tabs()/
        # get() call) while CDPSession persists for the life of the
        # attachment.
        self._pinned: bool = False
        self._pin_reason: str = ""
        self._pin_origin: str = ""
        self._pin_guard_unload: bool = True
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._label_text: str | None = None
        self._label_color: str | None = None
        self._label_script_id: str | None = None

        # Injected-engine handle (inject.EngineHandle) — lives here for the
        # same reason as pin/label state, and is *cache*, not registration:
        # per-document (executionContextsCleared drops it) and per-session
        # (_reattach_core drops it — the objectId dies with its sessionId).
        self._injected: Any | None = None
        self._injected_lock = asyncio.Lock()
        self._frame_seq: int = next(_frame_seq_counter)

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

    async def _enable_fetch_core(self) -> None:
        """Unlocked core of `enable_fetch`, for the reconnect path only.

        `_reattach_core` re-enables Fetch on a session that had it, and it can
        be reached *from inside* an in-flight `enable_fetch`: that call's
        `Fetch.enable` goes through `BrowserSession.execute`, which on a dead
        socket triggers `_reconnect` → `_reattach_core` → back here. Taking
        `_fetch_lock` again on the same task would deadlock outright, since
        asyncio.Lock is not reentrant — the identical reason
        `reattach_session` is a locked wrapper over `_reattach_core` rather
        than locking the core itself.
        """
        if self._fetch_enabled:
            return
        self._fetch_enabled = True  # claim before the await below yields
        from .capture import enable as fetch_enable

        try:
            await fetch_enable(self)
            self.capture_bodies = True
        except Exception:
            self._fetch_enabled = False
            raise

    async def enable_fetch(self) -> None:
        """Enable Fetch body capture on this session. Idempotent."""
        async with self._fetch_lock:
            await self._enable_fetch_core()

    async def disable_fetch(self) -> None:
        """Disable Fetch body capture on this session. Idempotent."""
        from .capture import disable as fetch_disable

        async with self._fetch_lock:
            if not self._fetch_enabled:
                return
            self._fetch_enabled = False  # claim before the await below yields
            self.capture_bodies = False
            try:
                await fetch_disable(self)
            except Exception:
                # Live, not decorative: `capture.disable` swallowed its own
                # exception until it was made to propagate, so this branch was
                # dead and a failed disable read as a success. It restores both
                # flags because `capture.disable` leaves `_fetch_handler` in
                # place when Chrome refuses — the session is still capturing,
                # and saying otherwise would strand paused requests.
                self._fetch_enabled = True
                self.capture_bodies = True
                raise

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

    def store_event(self, event: dict, method: str, request_id: str | None) -> None:
        """Insert an event row into this session's DuckDB event store.

        Counts the row as well as writing it — capture.py stores its synthetic
        body rows through here, and those have to age out of the FIFO like
        anything else.
        """
        self.db.execute(
            "INSERT INTO events (event, method, request_id, target) VALUES (?, ?, ?, ?)",
            [
                _json_dumps_safe(event),
                method,
                request_id,
                self.target_info.get("targetId", ""),
            ],
        )
        self._event_count += 1

    def _handle_event(self, data: dict) -> None:
        """Store event in DuckDB and dispatch Fetch handler if needed."""
        try:
            method = data.get("method", "")
            params = data.get("params", {})
            request_id = params.get("requestId") or params.get("networkId")

            target_id = self.target_info.get("targetId", "")

            # Synchronous insert — microseconds, no contention. store_event
            # owns the counter (see there); the prune trigger stays here so it
            # is driven by real CDP traffic rather than by a body-capture task.
            #
            # Guarded separately from the dispatches below, which is the whole
            # point: everything after this is *control flow*, not recording.
            # The `Fetch.requestPaused` branch is the only thing that ever
            # resumes a paused request, and the `Runtime.bindingCalled` branch
            # is how a pill answers a human gate — under the single outer
            # `except` a raise from here (a closed DuckDB after a torn-down
            # session, a value the store rejects) skipped both, hanging the
            # request in Chrome with `settle` waiting on it and swallowing the
            # gate answer, at debug level. Losing an event from the store is a
            # gap in history; losing the resume wedges the tab.
            try:
                self.store_event(data, method, request_id)
            except Exception as exc:
                logger.debug("store_event(%s) failed: %s", method, exc)

            if request_id:
                if method == "Network.requestWillBeSent":
                    self._inflight[request_id] = time.monotonic()
                elif method in ("Network.loadingFinished", "Network.loadingFailed"):
                    self._inflight.pop(request_id, None)
                elif method == "Network.responseReceived" and _is_streaming(params):
                    # The response arrived; only the body is open-ended. An SSE
                    # stream never emits loadingFinished until it closes, so
                    # counting it as pending work means this tab can never
                    # settle again — every later mutation would burn its full
                    # deadline. Settle asks "has the network gone quiet", and
                    # for a stream the answer is yes the moment the headers land.
                    self._inflight.pop(request_id, None)

            if method == "Fetch.requestPaused":
                # Dispatch async handler without blocking the recv loop.
                # `bg.spawn`, not a bare create_task: this coroutine is the only
                # thing that ever resumes the paused request, so a task
                # collected mid-flight leaves it hanging in Chrome until the
                # request times out — with `settle` waiting on it the whole
                # time. See bg.py.
                if self._fetch_handler is not None:
                    bg.spawn(
                        self._fetch_handler(self, params),
                        name=f"repld-fetch-{params.get('requestId', '?')[:8]}",
                    )

            if method == "Runtime.bindingCalled":
                if self._binding_handler is not None:
                    bg.spawn(
                        self._binding_handler(self, params),
                        name=f"repld-binding-{params.get('name', '?')}",
                    )

            if method == "Runtime.executionContextsCleared":
                # Navigation replaced the document; the injected engine (and
                # every aria-ref inside it) died with the old context. Plain
                # attribute reset — inject.ensure_engine re-instantiates lazily.
                self._injected = None

            if method == "Runtime.consoleAPICalled":
                _check_controls_observation(params, target_id)
                if params.get("type") == "error":
                    _push_console_error(params, target_id, self.port, self._loop)

            if method == "Runtime.exceptionThrown":
                _push_exception(params, target_id, self.port, self._loop)

            # Periodic pruning — async task to avoid blocking the recv loop
            if self._event_count >= self._next_prune_check:
                self._next_prune_check = self._event_count + PRUNE_CHECK_INTERVAL
                if self._event_count > MAX_EVENTS:
                    bg.spawn(self._async_prune(), name="repld-prune")

        except Exception as exc:
            logger.debug("_handle_event error: %s", exc)

    async def _async_prune(self) -> None:
        """FIFO prune oldest events, on the loop — deliberately, see below.

        `create_task` only gets this out of the recv *handler*; the SQL still
        runs on the shared loop. That is fine here, and measured: the delete is
        a rowid-range op on a columnar store, so it costs ~7 ms flat at
        MAX_EVENTS — independent of row width (checked at 2 KB/event) — and
        fires at most once per PRUNE_CHECK_INTERVAL events, i.e. under 10 µs
        amortised per event. That is at the level the observation pipeline was
        *optimised down to*, not above it, so `to_thread` here would buy single
        -digit milliseconds in exchange for a second writer to the event store
        (its own cursor, since `store_event` holds the main connection) plus an
        in-flight guard, because the loop keeps counting past MAX_EVENTS while
        a thread works and would spawn a concurrent delete over the same rows.
        Not worth it. Re-measure before assuming otherwise.
        """
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
            self._next_prune_check = self._event_count + PRUNE_CHECK_INTERVAL
            logger.debug(
                "Pruned %d events; %d remaining", delete_count, self._event_count
            )
        except Exception as exc:
            logger.debug("Prune error: %s", exc)

    # ------------------------------------------------------------------
    # Query interface
    # ------------------------------------------------------------------

    @contextlib.contextmanager
    def _cursor(self):
        """A per-call cursor, held against `cleanup()` for its whole lifetime.

        The lock spans execute-and-fetch rather than just `self.db.cursor()`,
        because a close landing anywhere in between is the same crash. That
        means `cleanup()` waits out an in-flight query — bounded by the slowest
        CTE the views build (tens of milliseconds, the figure `observe.py`
        measured), and only when a read is genuinely running. Deliberate: the
        alternative is closing the connection out from under a live cursor.

        It does *not* serialize against the loop's own writes. `store_event`
        and `_async_prune` go through `self.db` directly, and so does
        `cleanup()` — all three run on the loop, so they are already ordered
        with respect to each other, and DuckDB's MVCC isolates this cursor from
        that writer. Only the off-loop readers need the guard.
        """
        with self._db_lock:
            if self._db_closed:
                raise RuntimeError(
                    f"event store for target {self.chrome_target_id[:8]} is closed "
                    "— the tab was detached or the browser reconnected"
                )
            cur = self.db.cursor()
            try:
                yield cur
            finally:
                cur.close()

    def query(self, sql: str, params: list | None = None) -> list:
        """Execute arbitrary SQL against the events DB.

        Runs on a per-call cursor: callers may be on IPC reader threads
        while the loop thread writes via the main connection.
        """
        with self._cursor() as cur:
            if params:
                return cur.execute(sql, params).fetchall()
            return cur.execute(sql).fetchall()

    def query_dicts(self, sql: str, params: list | None = None) -> list[dict]:
        """Like `query`, but rows are dicts keyed by the query's live column
        names — a `SELECT *` reorder or rename can't silently misassign
        fields the way a positionally-zipped tuple would."""
        with self._cursor() as cur:
            result = cur.execute(sql, params) if params else cur.execute(sql)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row, strict=True)) for row in result.fetchall()]

    def fetch_body(self, request_id: str) -> dict:
        """Return captured body; fall back to Network.getResponseBody CDP call."""
        # Check DuckDB for captured body first
        try:
            rows = self.query(
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
            )
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
        if _on_loop(self._loop):
            # Blocking on fut.result() here would stall the loop the
            # coroutine needs, timing out after 10s. Fail fast instead.
            return {
                "error": "body not in capture store; a blocking CDP fetch would "
                "stall the kernel loop — use await tab.cdp("
                "'Network.getResponseBody', requestId=...) instead"
            }
        fut = asyncio.run_coroutine_threadsafe(
            self.execute("Network.getResponseBody", {"requestId": request_id}),
            self._loop,
        )
        try:
            return fut.result(timeout=10)
        except Exception as exc:
            return {"error": str(exc)}

    def inflight_count(self) -> int:
        """Requests still pending, for observe.settle(). Ages out the stuck ones.

        `_is_streaming` handles the case we can name, but it can only act on a
        response we actually saw: a request whose `Network.responseReceived`
        never arrives (a hung connection, a body Chrome stops reporting on, a
        stream with a mime type we didn't recognise) would otherwise sit here
        forever and pin this tab's settle at its full deadline for the rest of
        the session. `_INFLIGHT_MAX_AGE` is deliberately far longer than any
        request settle would legitimately wait on, so this evicts wedged
        entries without ever cutting a slow-but-live one short.

        Snapshots before scanning: `clear_events` empties this map and is
        reachable off-loop (`tab.clear()` in a pure-sync exec cell runs in
        `asyncio.to_thread`), so iterating it live could raise "dictionary
        changed size during iteration" on the loop, out of `settle`, and fail
        the whole observed mutation.
        """
        if not self._inflight:
            return 0
        cutoff = time.monotonic() - _INFLIGHT_MAX_AGE
        stale = [
            rid for rid, started in list(self._inflight.items()) if started < cutoff
        ]
        for rid in stale:
            self._inflight.pop(rid, None)
        return len(self._inflight)

    def _reset_counters(self) -> None:
        """Loop-owned half of `clear_events`. Only ever run on the loop."""
        self._event_count = 0
        self._next_prune_check = PRUNE_CHECK_INTERVAL
        self._inflight.clear()

    def clear_events(self) -> None:
        """Delete all stored events.  Cursor per call — may run on IPC threads.

        The delete is deliberately off-loop-safe; the counters are not, and the
        two halves are reached differently for that reason. `_event_count`,
        `_next_prune_check` and `_inflight` are written by `store_event` and
        `_handle_event` from the recv handler, and `self._event_count += 1` is
        a read-modify-write that a foreign thread's `= 0` can land inside and
        lose — leaving the FIFO prune trigger describing a store it no longer
        matches.

        `browser_dispatch._run_sync_on_loop` already puts the MCP `browser_clear`
        path on the loop, but that is not the only caller: a `browser.clear()`
        typed into a pure-sync exec cell runs in `asyncio.to_thread`, and the
        dashboard reaches `tab.clear()` the same way. Routing here rather than at
        each entry point is what makes the rule hold for all of them.

        The scheduled reset can miss events that arrive between the delete and
        it — the count drifts low by however many, never high — and
        `_async_prune` recounts with `SELECT COUNT(*)` on its next sweep, so the
        drift is bounded and self-correcting. A lost increment is not.
        """
        with self._cursor() as cur:
            cur.execute("DELETE FROM events")
        if self._loop is None or _on_loop(self._loop):
            self._reset_counters()
        else:
            self._loop.call_soon_threadsafe(self._reset_counters)

    def cleanup(self) -> None:
        """Close the DuckDB connection.

        Under `_db_lock`, so it waits out any off-loop reader holding a cursor
        rather than closing the connection beneath it, and marks the store
        closed so a reader arriving afterwards gets an attributable error
        instead of a raw DuckDB one. Idempotent — every teardown path
        (`disconnect`, a failed `_reconnect`, `Target.targetDestroyed`) can
        reach it, and two of them can reach it for the same session.
        """
        with self._db_lock:
            if self._db_closed:
                return
            self._db_closed = True
            try:
                self.db.close()
            except Exception:
                pass
