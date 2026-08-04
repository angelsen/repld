"""`Browser` — one Chrome instance: its WebSocket, its watch patterns, its tabs.

Carved out of the package `__init__` alongside `pool.py`, which stacks N of
these behind one object. Everything here is scoped to a single debug port;
anything that fans out across ports is next door.
"""

import asyncio
import logging
import os
import time
from fnmatch import fnmatch

from ..events import BrowserTabAttached, BrowserTabDetached, emit
from .cdp import CDPSession
from .row import Rows
from .session import WORKER_TYPES, BrowserSession
from .tab import Tab
from .target import (
    _is_target_id,
    _NO_TABS,
    _print_browser_help,
    _split_target,
    make_target,
    TabNotFoundError,
)

__all__ = ["Browser"]

logger = logging.getLogger(__name__)

_ATTACH_POLL_INTERVAL_S = 0.3  # retry cadence while waiting for a tab to appear/attach
# Ceiling on waiting out a concurrent auto-attach for a target — see
# `_attach_racing`. Generous: it only elapses if that attach never completes,
# which is a real failure worth reporting rather than waiting on further.
_ATTACH_TIMEOUT_S = 5.0

# The *sync* methods below snapshot `_sessions` / `_browsers` with `list(...)`
# before walking them, and that is load-bearing rather than stylistic. Both
# dicts are written from the asyncio loop — attach, detach, and the recv loop's
# Target.targetDestroyed handler all mutate `_sessions` on every tab that opens
# or closes — while these methods run on whatever thread called them. Two such
# threads exist and neither is exotic: `browser_dispatch` answers some tools on
# the IPC reader thread, and `runtime._eval` runs every pure-sync exec cell in
# `asyncio.to_thread`, so `browser.tabs` typed into a cell is already off-loop.
# Iterating a live dict from there raises "dictionary changed size during
# iteration" the moment a page opens a popup — reproducible, unattributable,
# and dependent on the user's browsing rather than on anything repld did.
# `list(d.values())` is atomic under the GIL (the copy happens in C, with no
# bytecode boundary for a thread switch), so the snapshot closes it outright.
#
# Being on the loop is *not* on its own a reason to skip the snapshot, and
# reading the rule that way is what left `BrowserPool`'s fan-out methods
# unguarded. What matters is whether the loop body can yield: an `await` inside
# `for b in self._browsers.values()` hands control back to the loop, another
# task runs `connect()` (`self._browsers[port] = b`) or `disconnect()`
# (`.pop()`), and resuming the iteration raises the same "dictionary changed
# size during iteration". `_rpc_browser_connect` / `_rpc_browser_disconnect`
# are `async def` on this loop, so a dashboard Connect click during a
# `browser.watch("*x*")` fan-out reaches it. So: snapshot when the body can
# yield (any `await` in it) or when the method may run off-loop. An async loop
# whose body never awaits, and one that `return`s out on the first match
# without ever resuming, are the two cases that genuinely don't need it.
#
# Snapshotting fixes the crash, not the semantics: a browser connected midway
# through a fan-out simply misses that sweep. That matches the sync siblings
# and is fine — the alternative is holding a lock across N WebSocket round
# trips.


class Browser:
    """Manages the BrowserSession + watch patterns + Tab resolution.

    Injected into __main__ by the kernel after lazy initialization.
    """

    def __init__(
        self,
        port: int | None = None,
    ) -> None:
        self.port = port or int(os.environ.get("REPLD_CHROME_PORT", "9222"))
        self._session: BrowserSession = BrowserSession(self.port)
        self._connected: bool = False
        # Serializes the first connect. BrowserSession already guards the
        # *re*connect path with its own lock; this is the same shape one step
        # earlier — see `_ensure_connected`.
        self._connect_lock = asyncio.Lock()

    async def _ensure_connected(self) -> None:
        """Connect if we aren't, reconnect if the socket died. Idempotent.

        Locked because the check and the connect straddle an await, and
        `BrowserSession.connect` unconditionally rebinds `_ws` and `_recv_task`:
        N callers arriving together produced N WebSockets and N recv loops, with
        only the last reachable and the rest left open against Chrome forever.
        Two concurrent `browser_*` tool calls on a cold pool is enough to do it
        — `connect()` yields for milliseconds inside `to_thread` fetching
        /json/version — and so is any `gather()` in user code.

        The fast path stays lock-free so the common case (already connected,
        every call after the first) doesn't serialize on it.
        """
        if self._connected and self._session._is_connected():
            return
        async with self._connect_lock:
            # Re-check: a caller that queued behind the lock is usually looking
            # at work the holder already did.
            if not self._connected:
                await self._session.connect()
                self._session._on_target_created = self._on_target_created
                self._session._on_target_destroyed = self._on_target_destroyed
                self._connected = True
                logger.debug("BrowserSession connected on port %s", self.port)
            elif not self._session._is_connected():
                # _reconnect takes _reconnect_lock, never _connect_lock, and
                # nothing under it re-enters here — so holding this across it
                # can't deadlock.
                await self._session._reconnect()

    def _on_target_created(self, target_info: dict, target_id: str) -> None:
        """Called when a new tab is auto-attached."""
        url = target_info.get("url", "")
        title = target_info.get("title", "")
        emit(BrowserTabAttached(target_id, url, title))

    def _on_target_destroyed(self, target_id: str) -> None:
        """Called when a tab is destroyed."""
        emit(BrowserTabDetached(target_id))

    def _iter_tabs(self) -> list[Tab]:
        """Wrap all attached CDPSessions as Tab objects."""
        return [
            Tab(cdp, cdp.target_info.get("targetId", ""), self.port)
            for cdp in list(self._session._sessions.values())
        ]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get(
        self,
        target: str,
        *,
        timeout: float | None = None,
        fresh: bool = False,
        ready: str | None = None,
    ) -> Tab:
        """Find one tab by URL glob or target ID. Attach on demand.

        **Glob** (e.g. ``"*github.com*"``): searches pages and iframes,
        skips workers. ``timeout`` polls until a match appears. ``fresh``
        skips tabs that already matched at call time.

        **Target ID** (e.g. ``"9222:a81998"``): resolves any type including
        workers. Attaches if not already attached. ``fresh`` is ignored;
        ``timeout`` bounds only the wait for a concurrent attach to land
        (default ``_ATTACH_TIMEOUT_S``) — an unknown ID fails at once either
        way, since polling for a target that isn't there buys nothing.

        **ready**: CSS selector or JS expression that must be truthy before
        the Tab is returned. Also used for auto-recovery on HMR/navigation.
        Default (None) uses ``document.readyState === 'complete'``.

        Enables proactive Fetch body capture on freshly attached tabs.
        """
        if _is_target_id(target):
            return await self._get_by_id(target, ready=ready, timeout=timeout)
        return await self._get_by_glob(
            target, timeout=timeout, fresh=fresh, ready=ready
        )

    def _find_by_prefix(self, prefix: str) -> tuple[str | None, CDPSession | None, str]:
        """Look up an already-attached session by 6-char lowercase target-ID prefix.

        Returns (session_id, cdp, full_chrome_id); session_id and cdp are
        None on miss.
        """
        prefix = prefix.lower()
        for sid, cdp in list(self._session._sessions.items()):
            chrome_id = cdp.target_info.get("targetId", "")
            if chrome_id[:6].lower() == prefix:
                return sid, cdp, chrome_id
        return None, None, ""

    async def _attach_and_wrap(
        self, tid: str, t: dict | None = None, *, ready: str | None = None
    ) -> "Tab | None":
        """Attach, enable Fetch body capture, and wrap the result in a Tab.

        Returns None if attach failed (e.g. a concurrent attach for the same
        target is already in flight) — callers keep searching in that case.
        """
        cdp = await self._session.attach(tid, t)
        if cdp is None:
            return None
        await cdp.enable_fetch()
        return Tab(cdp, tid, self.port, ready=ready)

    async def _attach_racing(
        self,
        tid: str,
        t: dict | None = None,
        *,
        ready: str | None = None,
        budget: float | None = None,
    ) -> "Tab | None":
        """Attach to `tid`, waiting out a concurrent attach for the same target.

        `session.attach` returns None while another attach for this target is
        already in flight, and the way that happens is that we lose a race with
        ourselves: a watch pattern matching the URL makes `Target.targetCreated`
        fire an `_auto_attach` task that claims the target first. Both callers
        hit exactly that — `open()` on a URL the caller also watches
        (`watch("*myapp.com*")` then `open("https://myapp.com/x")`), and
        `_get_by_id` on a target that has just started matching one — and both
        want the same recovery: wait for the in-flight attach to land, then take
        its session. Re-calling `attach()` is the whole of it, since it returns
        the existing session once one exists.

        Returns None if nothing lands within `budget` (default
        `_ATTACH_TIMEOUT_S`). The two callers report that differently: a named
        target that never appears is a `TabNotFoundError` the pool may retry
        elsewhere, while a tab we just created and cannot attach to is a plain
        failure. `_get_by_id` passes the caller's own `timeout` so that
        `get(id, timeout=1)` means one second here too, rather than silently
        taking the five-second default.
        """
        deadline = time.monotonic() + (_ATTACH_TIMEOUT_S if budget is None else budget)
        while True:
            tab = await self._attach_and_wrap(tid, t, ready=ready)
            if tab is not None:
                return tab
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(_ATTACH_POLL_INTERVAL_S)

    async def _get_by_id(
        self, target: str, ready: str | None = None, timeout: float | None = None
    ) -> Tab:
        """Resolve a target ID, attaching on demand if needed.

        `timeout` bounds only the attach race — an ID that matches no target at
        all still fails immediately, since there is nothing to wait for.
        """
        _, prefix = _split_target(target)
        _sid, cdp, chrome_id = self._find_by_prefix(prefix)
        if cdp is not None:
            return Tab(cdp, chrome_id, self.port, ready=ready)

        await self._ensure_connected()
        for t in await self._session.list_targets():
            tid = t.get("targetId", "")
            if tid and tid[:6].lower() == prefix:
                tab = await self._attach_racing(tid, t, ready=ready, budget=timeout)
                if tab is not None:
                    return tab

        raise TabNotFoundError(
            f"No tab '{target}'. Attached: {self._attached_short_ids()}"
        )

    def _attached_short_ids(self) -> list[str]:
        """Short target IDs of all attached sessions, for error messages."""
        return [
            make_target(self.port, cdp.target_info.get("targetId", ""))
            for cdp in list(self._session._sessions.values())
        ]

    async def _get_by_glob(
        self,
        pattern: str,
        *,
        timeout: float | None = None,
        fresh: bool = False,
        ready: str | None = None,
    ) -> Tab:
        """Find one tab matching a URL glob. Skips workers."""
        exclude: set[str] = set()
        if fresh:
            for cdp in self._session._sessions.values():
                tid = self._glob_target_id(cdp.target_info, pattern, exclude)
                if tid:
                    exclude.add(tid)
            await self._ensure_connected()
            for t in await self._session.list_targets():
                tid = self._glob_target_id(t, pattern, exclude)
                if tid:
                    exclude.add(tid)

        deadline = (
            asyncio.get_running_loop().time() + timeout if timeout is not None else None
        )
        while True:
            for cdp in self._session._sessions.values():
                tid = self._glob_target_id(cdp.target_info, pattern, exclude)
                if tid:
                    return Tab(cdp, tid, self.port, ready=ready)

            await self._ensure_connected()
            for t in await self._session.list_targets():
                tid = self._glob_target_id(t, pattern, exclude)
                if tid:
                    # `_attach_racing`, not `_attach_and_wrap`: a target that
                    # matches this glob may equally match a *watch* pattern, in
                    # which case our own `_auto_attach` holds it and `attach()`
                    # answers None. Treating that as a miss made a glob `get()`
                    # with the default timeout=None raise TabNotFoundError for
                    # a tab that was seconds from being attached — the exact
                    # race `_get_by_id` and `open()` already wait out. The
                    # budget is the caller's own remaining deadline, so
                    # `get(glob, timeout=1)` still means one second in total
                    # rather than one second per matching target.
                    budget = (
                        max(0.0, deadline - asyncio.get_running_loop().time())
                        if deadline is not None
                        else None
                    )
                    tab = await self._attach_racing(tid, t, ready=ready, budget=budget)
                    if tab is not None:
                        return tab

            if deadline is None or asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(_ATTACH_POLL_INTERVAL_S)

        raise TabNotFoundError(f"No tab matching '{pattern}'")

    @staticmethod
    def _glob_target_id(info: dict, pattern: str, exclude: set[str]) -> str | None:
        """targetId if info matches: non-worker, url glob, not excluded; else None."""
        if info.get("type", "") in WORKER_TYPES:
            return None
        tid = info.get("targetId", "")
        if not tid or tid in exclude:
            return None
        return tid if fnmatch(info.get("url", ""), pattern) else None

    async def watch(self, pattern: str) -> str:
        """Register a URL glob pattern and attach currently-matching tabs.

        Future tabs matching the pattern auto-attach. Workers are skipped.
        Returns a summary string.
        """
        await self._ensure_connected()

        # Add pattern (registers it in _watched_patterns)
        self._session.add_pattern(pattern)

        # Attach any targets that match the pattern and aren't already attached
        targets = await self._session.list_targets()
        to_attach: list[tuple[str, dict]] = []
        for t in targets:
            tid = self._glob_target_id(t, pattern, set())
            if tid and self._session.find_by_target_id(tid) is None:
                to_attach.append((tid, t))

        failures: list[tuple[str, str]] = []

        async def _attach_one(tid: str, info: dict) -> str | None:
            try:
                await self._session.attach(tid, info)
                self._session._watched_patterns.setdefault(pattern, set()).add(tid)
                return tid
            except Exception as exc:
                logger.debug("Attach %s: %s", tid, exc)
                failures.append((tid, str(exc)))
                return None

        results = await asyncio.gather(
            *[_attach_one(tid, info) for tid, info in to_attach]
        )
        newly_attached = [tid for tid in results if tid]

        total = len(self._session._sessions)
        msg = (
            f"Attached {len(newly_attached)} new tab(s) for pattern '{pattern}'. "
            f"Total attached: {total}."
        )
        if failures:
            # Surface the reason directly — logger.debug alone is invisible
            # by default (no logging configured), which would make a failed
            # attach look identical to "nothing matched the pattern".
            detail = "; ".join(f"{tid[:6]}: {reason}" for tid, reason in failures)
            msg += f" {len(failures)} attach attempt(s) failed: {detail}"
        return msg

    async def open(self, url: str) -> "Tab":
        """Create a new tab and attach to it.

        Target.createTarget → attach → enable Fetch → return Tab.

        The attach goes through `_attach_racing` because we may lose a race with
        ourselves when the new URL matches a watch pattern — see that method for
        the mechanics; `_get_by_id` waits out the same race the same way. Note
        it uses the session `attach()` returns directly rather than looking the
        target up afterwards: the new session isn't always registered under its
        targetId yet, so a sync lookup would race it.
        """
        await self._ensure_connected()
        result = await self._session.execute("Target.createTarget", {"url": url})
        tid = result["targetId"]
        tab = await self._attach_racing(tid)
        if tab is None:
            raise RuntimeError(f"Failed to attach to new tab '{tid}'")
        return tab

    async def detach(self, pattern: str | None = None) -> str:
        """Detach tabs by pattern; detach all if pattern is None."""
        if not self._connected:
            return "No browser connection."

        if pattern is None:
            # Detach everything
            sessions = list(self._session._sessions.items())
            for sid, cdp in sessions:
                await self._unpin_and_detach(sid, cdp)
            self._session._watched_patterns.clear()
            return f"Detached {len(sessions)} tab(s). All patterns cleared."

        # Detach sessions matching this pattern
        to_detach: list[tuple[str, CDPSession]] = []
        for sid, cdp in list(self._session._sessions.items()):
            url = cdp.target_info.get("url", "")
            if fnmatch(url, pattern):
                to_detach.append((sid, cdp))

        for sid, cdp in to_detach:
            await self._unpin_and_detach(sid, cdp)

        # Remove pattern
        self._session._watched_patterns.pop(pattern, None)
        return f"Detached {len(to_detach)} tab(s) for pattern '{pattern}'."

    @property
    def tabs(self) -> Rows:
        """List currently attached Tab objects."""
        return Rows(self._iter_tabs())

    async def pages(self) -> list[dict]:
        """List all Chrome targets (attached or not)."""
        await self._ensure_connected()
        return await self._session.list_targets()

    @property
    def patterns(self) -> list[str]:
        """List active watch patterns."""
        return list(self._session._watched_patterns.keys())

    def clear(self, target: str | None = None) -> str:
        """Clear captured events. Specify target for one tab, or None for all."""
        if target is not None:
            if not _is_target_id(target):
                raise RuntimeError(
                    f"Invalid target ID '{target}'. Expected format: '9222:a1b2c3'"
                )
            _, prefix = _split_target(target)
            _sid, cdp, _chrome_id = self._find_by_prefix(prefix)
            if cdp is None:
                raise RuntimeError(
                    f"No attached tab '{target}'. Attached: {self._attached_short_ids()}"
                )
            cdp.clear_events()
            return f"Cleared events for {target}."
        count = 0
        for cdp in list(self._session._sessions.values()):
            cdp.clear_events()
            count += 1
        return f"Cleared events for {count} tab(s)."

    async def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        body: "dict | str | None" = None,
        headers: "dict[str, str] | None" = None,
    ) -> dict:
        """In-page fetch using any attached tab (inherits cookies/session)."""
        tabs = self._iter_tabs()
        if not tabs:
            raise RuntimeError("No attached tabs — open or get a tab first")
        return await tabs[0].fetch(url, method=method, body=body, headers=headers)

    @staticmethod
    async def _safe_unpin(tab: Tab) -> None:
        """Unpin before detach/disconnect; a failed unpin must not block either."""
        if tab._pinned:
            try:
                await tab.unpin()
            except Exception:
                pass

    async def _unpin_and_detach(self, sid: str, cdp: CDPSession) -> None:
        """Unpin a tab (if pinned) and detach its session. Never raises."""
        try:
            await self._safe_unpin(
                Tab(cdp, cdp.target_info.get("targetId", ""), self.port)
            )
            await self._session.detach(sid)
        except Exception as exc:
            logger.debug("Detach %s: %s", sid, exc)

    async def disconnect(self) -> None:
        """Disconnect from Chrome. Unpins all tabs first (removes pill,
        beforeunload guard, and heartbeat task before dropping the socket)."""
        if self._connected:
            for tab in self._iter_tabs():
                await self._safe_unpin(tab)
            try:
                await self._session.disconnect()
            except Exception:
                pass
            self._connected = False

    async def detach_target(self, target_id: str) -> str:
        """Detach a single target by its short ID (e.g. '9222:abc123').
        Unpins first if the tab is pinned."""
        _, prefix = _split_target(target_id)
        sid, cdp, _full_id = self._find_by_prefix(prefix)
        if sid is not None and cdp is not None:
            await self._unpin_and_detach(sid, cdp)
            return f"Detached {target_id}."
        return f"Target {target_id} not found."

    def format_tabs_nested(self) -> str:
        """Format attached tabs as nested text showing target hierarchy."""
        entries: list[dict] = []
        id_to_short: dict[str, str] = {}
        for tab in self._iter_tabs():
            info = tab._session.target_info
            full_id = info.get("targetId", "")
            short = make_target(self.port, full_id)
            id_to_short[full_id] = short
            entries.append(
                {
                    "target": short,
                    "type": info.get("type", "unknown"),
                    "url": info.get("url", ""),
                    "title": info.get("title", ""),
                    "parent_frame_id": info.get("parentFrameId", ""),
                    "opener_id": info.get("openerId", ""),
                }
            )

        # Separate top-level vs children
        children: dict[str, list[dict]] = {}
        top_level: list[dict] = []

        for e in entries:
            parent_id = e["parent_frame_id"] or e["opener_id"]
            parent_short = id_to_short.get(parent_id)
            if parent_short:
                children.setdefault(parent_short, []).append(e)
            else:
                top_level.append(e)

        # Format output
        lines: list[str] = []
        for e in top_level:
            lines.append(f"{e['target']}  {e['type']}  {e['url']}")
            for child in children.get(e["target"], []):
                lines.append(f"  {child['target']}  {child['type']}  {child['url']}")

        # Orphaned children (parent not attached)
        shown = {e["target"] for e in top_level}
        for parent_short, kids in children.items():
            if parent_short not in shown:
                for child in kids:
                    lines.append(
                        f"{child['target']}  {child['type']} → {parent_short}  {child['url']}"
                    )

        return "\n".join(lines) if lines else _NO_TABS

    def help(self) -> None:
        """Print the Python API reference for the browser object."""
        _print_browser_help()

    def __repr__(self) -> str:
        n = len(self._session._sessions) if self._connected else 0
        return f"<Browser port={self.port} tabs={n} patterns={self.patterns}>"
