"""repld.browser — CDP integration for repld.

PUBLIC API:
  - LazyBrowser: Descriptor injected into __main__; lazy-bootstraps on first access.
  - Browser: Manages BrowserSession, watch patterns, and Tab resolution.

Usage in kernel:
    setattr(__main__, "browser", LazyBrowser(loop))

Then in user code:
    tab = await browser.get("*github.com*")   # find one tab by glob
    tab = await browser.get("9222:887d3d")    # find one tab by target ID
    await browser.watch("*github.com*")       # watch all matching
    await tab.js("document.title")
"""

import asyncio
import fnmatch
import logging
import os
import re
from typing import Any

from ..events import BrowserTabAttached, BrowserTabDetached, emit
from .cdp import CDPSession
from .session import WORKER_TYPES
from .row import Rows
from .tab import Tab

__all__ = ["Browser", "BrowserPool", "LazyBrowser"]

logger = logging.getLogger(__name__)


_TARGET_ID_RE = re.compile(r"^\d+:[0-9a-f]{6}$")


def _is_target_id(s: str) -> bool:
    """True if s looks like a short target ID (e.g. '9222:a81998')."""
    return bool(_TARGET_ID_RE.match(s))


def make_target(port: int, chrome_id: str) -> str:
    """Create short target ID from port and Chrome target ID.

    Format: "{port}:{6-char-lowercase-hex}"
    Example: make_target(9222, "887D3D7FA9473DCF...") -> "9222:887d3d"
    """
    return f"{port}:{chrome_id[:6].lower()}"


class Browser:
    """Manages the BrowserSession + watch patterns + Tab resolution.

    Injected into __main__ by the kernel after lazy initialization.
    """

    def __init__(
        self,
        port: int | None = None,
    ) -> None:
        from .session import BrowserSession

        self.port = port or int(os.environ.get("REPLD_CHROME_PORT", "9222"))
        self._session: BrowserSession = BrowserSession(self.port)
        self._connected: bool = False

    @classmethod
    def from_profile(cls, path: str) -> "Browser":
        """Connect to a Chrome instance by its user-data-dir.

        Reads the debug port from DevToolsActivePort (written by Chrome
        when launched with --remote-debugging-port).
        """
        port_file = os.path.join(path, "DevToolsActivePort")
        try:
            with open(port_file) as f:
                port = int(f.readline().strip())
        except (FileNotFoundError, ValueError) as exc:
            raise RuntimeError(
                f"No DevToolsActivePort in {path} — is Chrome running with --remote-debugging-port?"
            ) from exc
        return cls(port=port)

    async def _ensure_connected(self) -> None:
        if not self._connected:
            await self._session.connect()
            self._session._on_target_created = self._on_target_created
            self._session._on_target_destroyed = self._on_target_destroyed
            self._connected = True
            logger.debug("BrowserSession connected on port %s", self.port)
        elif not self._session._is_connected():
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
            for cdp in self._session._sessions.values()
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
        workers. Attaches if not already attached. ``timeout``/``fresh``
        are ignored.

        **ready**: CSS selector or JS expression that must be truthy before
        the Tab is returned. Also used for auto-recovery on HMR/navigation.
        Default (None) uses ``document.readyState === 'complete'``.

        Enables proactive Fetch body capture on freshly attached tabs.
        """
        if _is_target_id(target):
            return await self._get_by_id(target, ready=ready)
        return await self._get_by_glob(
            target, timeout=timeout, fresh=fresh, ready=ready
        )

    def _find_by_prefix(self, prefix: str) -> tuple[str | None, CDPSession | None, str]:
        """Look up an already-attached session by 6-char lowercase target-ID prefix.

        Returns (session_id, cdp, full_chrome_id); session_id and cdp are
        None on miss.
        """
        for sid, cdp in self._session._sessions.items():
            chrome_id = cdp.target_info.get("targetId", "")
            if chrome_id[:6].lower() == prefix:
                return sid, cdp, chrome_id
        return None, None, ""

    async def _get_by_id(self, target: str, ready: str | None = None) -> Tab:
        """Resolve a target ID, attaching on demand if needed."""
        _, prefix = target.split(":", 1)
        _sid, cdp, chrome_id = self._find_by_prefix(prefix)
        if cdp is not None:
            return Tab(cdp, chrome_id, self.port, ready=ready)

        await self._ensure_connected()
        for t in await self._session.list_targets():
            tid = t.get("targetId", "")
            if tid and tid[:6].lower() == prefix:
                cdp = await self._session.attach(tid)
                if cdp is not None:
                    await cdp.enable_fetch()
                    return Tab(cdp, tid, self.port, ready=ready)

        attached = [
            make_target(self.port, cdp.target_info.get("targetId", ""))
            for cdp in self._session._sessions.values()
        ]
        raise RuntimeError(f"No tab '{target}'. Attached: {attached}")

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
                url = cdp.target_info.get("url", "")
                if fnmatch.fnmatch(url, pattern):
                    exclude.add(cdp.target_info.get("targetId", ""))
            await self._ensure_connected()
            for t in await self._session.list_targets():
                url = t.get("url", "")
                tid = t.get("targetId", "")
                if fnmatch.fnmatch(url, pattern) and tid:
                    exclude.add(tid)

        deadline = (
            asyncio.get_running_loop().time() + timeout if timeout is not None else None
        )
        while True:
            for cdp in self._session._sessions.values():
                if cdp.target_info.get("type", "") in WORKER_TYPES:
                    continue
                url = cdp.target_info.get("url", "")
                tid = cdp.target_info.get("targetId", "")
                if fnmatch.fnmatch(url, pattern) and tid not in exclude:
                    return Tab(cdp, tid, self.port, ready=ready)

            await self._ensure_connected()
            for t in await self._session.list_targets():
                if t.get("type", "") in WORKER_TYPES:
                    continue
                url = t.get("url", "")
                tid = t.get("targetId", "")
                if fnmatch.fnmatch(url, pattern) and tid and tid not in exclude:
                    cdp = await self._session.attach(tid)
                    if cdp is not None:
                        await cdp.enable_fetch()
                        return Tab(cdp, tid, self.port, ready=ready)

            if deadline is None or asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(0.3)

        raise RuntimeError(f"No tab matching '{pattern}'")

    def _resolve_attached(self, target: str) -> Tab:
        """Sync lookup of an already-attached tab by short target ID.

        Used by clear() and open() where the tab is guaranteed attached.
        """
        if ":" not in target:
            raise RuntimeError(
                f"Invalid target ID '{target}'. Expected format: '9222:a1b2c3'"
            )
        _, prefix = target.split(":", 1)
        _sid, cdp, chrome_id = self._find_by_prefix(prefix)
        if cdp is not None:
            return Tab(cdp, chrome_id, self.port)

        attached = [
            make_target(self.port, cdp.target_info.get("targetId", ""))
            for cdp in self._session._sessions.values()
        ]
        raise RuntimeError(f"No attached tab '{target}'. Attached: {attached}")

    async def watch(self, pattern: str) -> str:
        """Register a URL glob pattern and attach currently-matching tabs.

        Future tabs matching the pattern auto-attach. Workers are skipped.
        Returns a summary string.
        """
        await self._ensure_connected()

        # Add pattern (registers it in _watched_patterns)
        self._session.add_pattern(pattern)

        # Attach any targets that match the pattern and aren't already attached
        newly_attached: list[str] = []
        targets = await self._session.list_targets()
        to_attach: list[str] = []
        for t in targets:
            if t.get("type", "") in WORKER_TYPES:
                continue
            tid = t.get("targetId", "")
            url = t.get("url", "")
            if fnmatch.fnmatch(url, pattern) and tid:
                already = any(
                    cdp.target_info.get("targetId") == tid
                    for cdp in self._session._sessions.values()
                )
                if not already:
                    to_attach.append(tid)

        failures: list[tuple[str, str]] = []

        async def _attach_one(tid: str) -> str | None:
            try:
                await self._session.attach(tid)
                self._session._watched_patterns.setdefault(pattern, set()).add(tid)
                return tid
            except Exception as exc:
                logger.debug("Attach %s: %s", tid, exc)
                failures.append((tid, str(exc)))
                return None

        results = await asyncio.gather(*[_attach_one(tid) for tid in to_attach])
        newly_attached = [tid for tid in results if tid]

        total = len(self._session._sessions)
        msg = (
            f"Attached {len(newly_attached)} new tab(s) for pattern '{pattern}'. "
            f"Total attached: {total}."
        )
        if failures:
            # Surface the reason directly — logger.debug alone is invisible
            # by default (no logging configured), so a failed attach used to
            # look identical to "nothing matched the pattern".
            detail = "; ".join(f"{tid[:6]}: {reason}" for tid, reason in failures)
            msg += f" {len(failures)} attach attempt(s) failed: {detail}"
        return msg

    async def open(self, url: str) -> "Tab":
        """Create a new tab and attach to it.

        Target.createTarget → attach → enable Fetch → return Tab.
        """
        await self._ensure_connected()
        result = await self._session.execute("Target.createTarget", {"url": url})
        tid = result["targetId"]
        # Use the session attach() returns directly (same as get()). The previous code
        # re-looked-up the tab via the sync _resolve_attached(), which raced the attach:
        # the new session isn't always registered with its targetId yet, so the lookup
        # would fail with "No attached tab". attach()'s return value is authoritative.
        cdp = await self._session.attach(tid)
        if cdp is None:
            raise RuntimeError(f"Failed to attach to new tab '{tid}'")
        await cdp.enable_fetch()
        return Tab(cdp, tid, self.port)

    async def detach(self, pattern: str | None = None) -> str:
        """Detach tabs by pattern; detach all if pattern is None."""
        if not self._connected:
            return "No browser connection."

        if pattern is None:
            # Detach everything
            session_ids = list(self._session._sessions.keys())
            for sid in session_ids:
                try:
                    await self._session.detach(sid)
                except Exception as exc:
                    logger.debug("Detach %s: %s", sid, exc)
            self._session._watched_patterns.clear()
            return f"Detached {len(session_ids)} tab(s). All patterns cleared."

        # Detach sessions matching this pattern
        to_detach: list[str] = []
        for sid, cdp in list(self._session._sessions.items()):
            url = cdp.target_info.get("url", "")
            if fnmatch.fnmatch(url, pattern):
                to_detach.append(sid)

        for sid in to_detach:
            try:
                await self._session.detach(sid)
            except Exception as exc:
                logger.debug("Detach %s: %s", sid, exc)

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
            tab = self._resolve_attached(target)
            tab.clear()
            return f"Cleared events for {target}."
        count = 0
        for cdp in self._session._sessions.values():
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

    async def disconnect(self) -> None:
        """Disconnect from Chrome. Unpins all tabs first (removes pill,
        beforeunload guard, and heartbeat task before dropping the socket)."""
        if self._connected:
            for tab in self._iter_tabs():
                if tab._pinned:
                    try:
                        await tab.unpin()
                    except Exception:
                        pass
            try:
                await self._session.disconnect()
            except Exception:
                pass
            self._connected = False

    async def detach_target(self, target_id: str) -> str:
        """Detach a single target by its short ID (e.g. '9222:abc123').
        Unpins first if the tab is pinned."""
        prefix = target_id.split(":")[-1]
        sid, cdp, full_id = self._find_by_prefix(prefix)
        if sid is not None and cdp is not None:
            tab = Tab(cdp, full_id, self.port)
            if tab._pinned:
                try:
                    await tab.unpin()
                except Exception:
                    pass
            await self._session.detach(sid)
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

        return "\n".join(lines) if lines else "(no attached tabs)"

    def help(self) -> None:
        """Print the Python API reference for the browser object."""
        from ..help import _TOPICS

        print(_TOPICS["browser"])

    def __repr__(self) -> str:
        n = len(self._session._sessions) if self._connected else 0
        return f"<Browser port={self.port} tabs={n} patterns={self.patterns}>"


class BrowserPool:
    """Manages multiple Browser instances across Chrome ports.

    Delegates watch/get/tabs/pages across all connected instances.
    Target IDs (e.g. ``42829:abc123``) route to the right Browser by port prefix.
    """

    def __init__(self) -> None:
        self._browsers: dict[int, Browser] = {}

    async def _ensure_any(self) -> None:
        """Auto-connect to the default port if no browsers are connected."""
        if not any(b._connected for b in self._browsers.values()):
            await self.connect()

    @staticmethod
    def _save_hint() -> None:
        try:
            from ..dashboard import save_hint

            save_hint()
        except Exception:
            pass

    async def connect(self, port: int = 9222) -> Browser:
        """Connect to a Chrome instance. Returns the Browser (new or existing)."""
        if port in self._browsers and self._browsers[port]._connected:
            return self._browsers[port]
        b = Browser(port=port)
        await b._ensure_connected()
        self._browsers[port] = b
        self._save_hint()
        return b

    async def disconnect(self, port: int | None = None) -> str:
        """Disconnect one or all browsers. Returns a summary string."""
        if port is not None:
            b = self._browsers.pop(port, None)
            if b:
                await b.disconnect()
                self._save_hint()
                return f"Disconnected from Chrome on port {port}."
            return f"No browser on port {port}."
        count = len(self._browsers)
        for b in self._browsers.values():
            try:
                await b.disconnect()
            except Exception:
                pass
        self._browsers.clear()
        self._save_hint()
        return f"Disconnected {count} browser(s)."

    def browser_for(self, target: str) -> Browser:
        """Resolve a target ID like '42829:abc123' to its Browser instance."""
        port_str = target.split(":")[0]
        try:
            port = int(port_str)
        except ValueError:
            raise RuntimeError(f"Invalid target ID: {target}")
        b = self._browsers.get(port)
        if b is None:
            raise RuntimeError(
                f"No browser on port {port}. Connected: {list(self._browsers.keys())}"
            )
        return b

    @property
    def ports(self) -> list[int]:
        return list(self._browsers.keys())

    @property
    def tabs(self) -> Rows:
        """Tabs from all connected browsers."""
        all_tabs = []
        for b in self._browsers.values():
            if b._connected:
                all_tabs.extend(b.tabs)
        return Rows(all_tabs)

    @property
    def patterns(self) -> list[str]:
        """Watch patterns from all connected browsers."""
        result = []
        for b in self._browsers.values():
            if b._connected:
                result.extend(b.patterns)
        return result

    async def pages(self) -> list[dict]:
        """All Chrome targets across all connected browsers."""
        await self._ensure_any()
        result = []
        for b in self._browsers.values():
            if b._connected:
                result.extend(await b.pages())
        return result

    async def watch(self, pattern: str) -> str:
        """Watch a pattern across all connected browsers."""
        await self._ensure_any()
        results = []
        for b in self._browsers.values():
            results.append(await b.watch(pattern))
        self._save_hint()
        return "\n".join(results)

    async def detach(self, pattern: str | None = None) -> str:
        """Detach tabs across all connected browsers."""
        results = []
        for b in self._browsers.values():
            results.append(await b.detach(pattern))
        self._save_hint()
        return "\n".join(results)

    def suppress(self, pattern: str) -> str:
        """Mute console errors containing this substring."""
        from .cdp import _suppress_patterns

        _suppress_patterns.add(pattern)
        self._save_hint()
        return f"suppressed {pattern!r} ({len(_suppress_patterns)} active)"

    def unsuppress(self, pattern: str) -> str:
        """Un-mute a previously suppressed pattern."""
        from .cdp import _suppress_patterns

        _suppress_patterns.discard(pattern)
        self._save_hint()
        return f"unsuppressed {pattern!r} ({len(_suppress_patterns)} active)"

    @property
    def suppressed(self) -> list[str]:
        """Currently suppressed error patterns."""
        from .cdp import _suppress_patterns

        return sorted(_suppress_patterns)

    async def get(
        self,
        target: str,
        *,
        timeout: float | None = None,
        fresh: bool = False,
        ready: str | None = None,
    ) -> Tab:
        """Find a tab by target ID or URL glob across all browsers."""
        if _is_target_id(target):
            b = self.browser_for(target)
            return await b.get(target, ready=ready)
        await self._ensure_any()
        for b in self._browsers.values():
            if not b._connected:
                continue
            try:
                return await b.get(target, timeout=timeout, fresh=fresh, ready=ready)
            except RuntimeError:
                continue
        raise RuntimeError(
            f"No tab matching '{target}' across {len(self._browsers)} browser(s)"
        )

    async def open(self, url: str) -> Tab:
        """Open a URL in the first connected browser."""
        await self._ensure_any()
        for b in self._browsers.values():
            if b._connected:
                return await b.open(url)
        raise RuntimeError("No browsers connected")

    async def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        body: "dict | str | None" = None,
        headers: "dict[str, str] | None" = None,
    ) -> dict:
        """In-page fetch using any attached tab (inherits cookies/session)."""
        await self._ensure_any()
        for b in self._browsers.values():
            if not b._connected:
                continue
            if b._iter_tabs():
                return await b.fetch(url, method=method, body=body, headers=headers)
        raise RuntimeError("No attached tabs — open or get a tab first")

    def clear(self, target: str | None = None) -> str:
        if target is not None:
            b = self.browser_for(target)
            return b.clear(target)
        results = []
        for b in self._browsers.values():
            results.append(b.clear())
        return "\n".join(results)

    def format_tabs_nested(self) -> str:
        parts = []
        for b in self._browsers.values():
            if b._connected:
                text = b.format_tabs_nested()
                if text != "(no attached tabs)":
                    parts.append(text)
        return "\n".join(parts) if parts else "(no attached tabs)"

    @property
    def port(self) -> int:
        """Port of the first connected browser (backwards compat)."""
        for b in self._browsers.values():
            return b.port
        return int(os.environ.get("REPLD_CHROME_PORT", "9222"))

    @property
    def _connected(self) -> bool:
        return any(b._connected for b in self._browsers.values())

    @property
    def _session(self):
        """Session of the first browser (backwards compat for protocol.py)."""
        for b in self._browsers.values():
            if b._connected:
                return b._session
        raise RuntimeError("No browsers connected")

    def help(self) -> None:
        from ..help import _TOPICS

        print(_TOPICS["browser"])

    def __repr__(self) -> str:
        if not self._browsers:
            return "<BrowserPool (no connections)>"
        parts = []
        for port, b in self._browsers.items():
            n = len(b._session._sessions) if b._connected else 0
            parts.append(f"{port}({n})")
        return f"<BrowserPool [{', '.join(parts)}] patterns={self.patterns}>"


class LazyBrowser:
    """Lazy descriptor injected into __main__.

    On first attribute access, bootstraps a BrowserPool and connects
    to the default Chrome port.
    """

    def __init__(self) -> None:
        self._real: BrowserPool | None = None

    def _bootstrap(self) -> BrowserPool:
        if self._real is None:
            self._real = BrowserPool()
        return self._real

    def help(self) -> None:
        """Print the Python API reference (no Chrome connection needed)."""
        from ..help import _TOPICS

        print(_TOPICS["browser"])

    def __getattr__(self, name: str) -> Any:
        return getattr(self._bootstrap(), name)

    def __repr__(self) -> str:
        if self._real is not None:
            return repr(self._real)
        return "<Browser (lazy — call browser.connect() to connect)>"

    def __reduce__(self):  # type: ignore[override]
        raise TypeError("LazyBrowser is not serializable")
