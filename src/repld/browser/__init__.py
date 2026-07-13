"""repld.browser — CDP integration for repld.

PUBLIC API:
  - LazyBrowser: Descriptor injected into __main__; lazy-bootstraps on first access.
  - Browser: Manages BrowserSession, watch patterns, and Tab resolution.
  - BrowserPool: Multi-port façade over one Browser per Chrome instance.

Usage in kernel:
    setattr(__main__, "browser", LazyBrowser())

Then in user code:
    tab = await browser.get("*github.com*")   # find one tab by glob
    tab = await browser.get("9222:887d3d")    # find one tab by target ID
    await browser.watch("*github.com*")       # watch all matching
    await tab.js("document.title")
"""

import asyncio
from fnmatch import fnmatch
import logging
import os
import re
from pathlib import Path
from typing import Any

from ..events import BrowserTabAttached, BrowserTabDetached, emit
from .cdp import CDPSession
from .session import WORKER_TYPES
from .row import Rows
from .tab import Tab

__all__ = ["Browser", "BrowserPool", "LazyBrowser"]

logger = logging.getLogger(__name__)


class TabNotFoundError(RuntimeError):
    """Raised when a target ID or glob pattern matches no tab — distinct from
    other RuntimeErrors (CDP/ready-signal/reattach failures) so BrowserPool.get()
    can retry across browsers on this specific error without masking real ones."""


_TARGET_ID_RE = re.compile(r"^\d+:[0-9a-f]{6}$")


def _is_target_id(s: str) -> bool:
    """True if s looks like a short target ID (e.g. '9222:a81998')."""
    return bool(_TARGET_ID_RE.match(s))


def _split_target(target: str) -> tuple[str, str]:
    """Split a short target ID like '9222:a1b2c3' into (port_str, prefix)."""
    port_str, _, prefix = target.partition(":")
    return port_str, prefix


def make_target(port: int, chrome_id: str) -> str:
    """Create short target ID from port and Chrome target ID.

    Format: "{port}:{6-char-lowercase-hex}"
    Example: make_target(9222, "887D3D7FA9473DCF...") -> "9222:887d3d"
    """
    return f"{port}:{chrome_id[:6].lower()}"


def _print_browser_help() -> None:
    """Print the Python API reference for the browser object."""
    from ..help import _TOPICS

    print(_TOPICS["browser"])


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
        prefix = prefix.lower()
        for sid, cdp in self._session._sessions.items():
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

    async def _get_by_id(self, target: str, ready: str | None = None) -> Tab:
        """Resolve a target ID, attaching on demand if needed."""
        _, prefix = _split_target(target)
        _sid, cdp, chrome_id = self._find_by_prefix(prefix)
        if cdp is not None:
            return Tab(cdp, chrome_id, self.port, ready=ready)

        await self._ensure_connected()
        for t in await self._session.list_targets():
            tid = t.get("targetId", "")
            if tid and tid[:6].lower() == prefix:
                tab = await self._attach_and_wrap(tid, t, ready=ready)
                if tab is not None:
                    return tab

        raise TabNotFoundError(
            f"No tab '{target}'. Attached: {self._attached_short_ids()}"
        )

    def _attached_short_ids(self) -> list[str]:
        """Short target IDs of all attached sessions, for error messages."""
        return [
            make_target(self.port, cdp.target_info.get("targetId", ""))
            for cdp in self._session._sessions.values()
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
                    tab = await self._attach_and_wrap(tid, t, ready=ready)
                    if tab is not None:
                        return tab

            if deadline is None or asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(0.3)

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
        newly_attached: list[str] = []
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
        tab = await self._attach_and_wrap(tid)
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
                try:
                    await self._safe_unpin(
                        Tab(cdp, cdp.target_info.get("targetId", ""), self.port)
                    )
                    await self._session.detach(sid)
                except Exception as exc:
                    logger.debug("Detach %s: %s", sid, exc)
            self._session._watched_patterns.clear()
            return f"Detached {len(sessions)} tab(s). All patterns cleared."

        # Detach sessions matching this pattern
        to_detach: list[tuple[str, CDPSession]] = []
        for sid, cdp in list(self._session._sessions.items()):
            url = cdp.target_info.get("url", "")
            if fnmatch(url, pattern):
                to_detach.append((sid, cdp))

        for sid, cdp in to_detach:
            try:
                await self._safe_unpin(
                    Tab(cdp, cdp.target_info.get("targetId", ""), self.port)
                )
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

    @staticmethod
    async def _safe_unpin(tab: Tab) -> None:
        """Unpin before detach/disconnect; a failed unpin must not block either."""
        if tab._pinned:
            try:
                await tab.unpin()
            except Exception:
                pass

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
        sid, cdp, full_id = self._find_by_prefix(prefix)
        if sid is not None and cdp is not None:
            await self._safe_unpin(Tab(cdp, full_id, self.port))
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
        _print_browser_help()

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

    async def connect(
        self, port: int | None = None, *, profile: str | None = None
    ) -> Browser:
        """Connect to a Chrome instance. Returns the Browser (new or existing).

        Pass profile=<user-data-dir> to resolve the port from that profile's
        DevToolsActivePort file — Chrome writes it when launched with
        --remote-debugging-port (including port 0 for an ephemeral port).
        """
        if profile is not None:
            port_file = Path(profile).expanduser() / "DevToolsActivePort"
            try:
                port = int(port_file.read_text().splitlines()[0].strip())
            except (OSError, ValueError, IndexError) as exc:
                raise RuntimeError(
                    f"No DevToolsActivePort in {profile} — is Chrome running "
                    "with --remote-debugging-port?"
                ) from exc
        if port is None:
            port = int(os.environ.get("REPLD_CHROME_PORT", "9222"))
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
        port_str, _ = _split_target(target)
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

    def resolve_tab(self, target_id: str) -> "Tab":
        """Find an attached Tab by its raw Chrome targetId, across all ports."""
        from .tab import Tab

        for port, b in self._browsers.items():
            if not b._connected:
                continue
            cdp = b._session.find_by_target_id(target_id)
            if cdp is not None:
                return Tab(cdp, target_id, port)
        raise RuntimeError(f"tab not attached: {target_id}")

    def snapshot(self) -> dict:
        """Serializable state for the dashboard: connection + tab list."""
        tab_list = []
        for port, b in self._browsers.items():
            if not b._connected:
                continue
            for cdp in b._session._sessions.values():
                info = cdp.target_info
                tab_list.append(
                    {
                        "id": make_target(port, info.get("targetId", "")),
                        "target_id": info.get("targetId", ""),
                        "port": port,
                        "type": info.get("type", ""),
                        "url": info.get("url", ""),
                        "title": info.get("title", ""),
                    }
                )
        return {
            "connected": self._connected,
            "ports": self.ports,
            "patterns": self.patterns if self._connected else [],
            "tabs": tab_list,
        }

    @property
    def ports(self) -> list[int]:
        return list(self._browsers.keys())

    @property
    def connected_ports(self) -> list[int]:
        """Ports whose Browser is currently connected (for hint persistence)."""
        return [p for p, b in self._browsers.items() if b._connected]

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
        # One deadline shared across all browsers — otherwise each browser
        # gets the full `timeout`, so N browsers can take up to N*timeout.
        deadline = (
            asyncio.get_running_loop().time() + timeout if timeout is not None else None
        )
        for b in self._browsers.values():
            if not b._connected:
                continue
            remaining = (
                max(0.0, deadline - asyncio.get_running_loop().time())
                if deadline is not None
                else None
            )
            try:
                return await b.get(target, timeout=remaining, fresh=fresh, ready=ready)
            except TabNotFoundError:
                continue
        raise TabNotFoundError(
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
    def _connected(self) -> bool:
        return any(b._connected for b in self._browsers.values())

    def help(self) -> None:
        _print_browser_help()

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

    def peek(self) -> "BrowserPool | None":
        """Return the underlying pool without triggering bootstrap/connect."""
        return self._real

    def help(self) -> None:
        """Print the Python API reference (no Chrome connection needed)."""
        _print_browser_help()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._bootstrap(), name)

    def __repr__(self) -> str:
        if self._real is not None:
            return repr(self._real)
        return "<Browser (lazy — call browser.connect() to connect)>"

    def __reduce__(self):  # type: ignore[override]
        raise TypeError("LazyBrowser is not serializable")
