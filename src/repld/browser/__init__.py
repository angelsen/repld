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
from .session import WORKER_TYPES
from .tab import Rows, Tab

__all__ = ["Browser", "LazyBrowser"]

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

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get(
        self,
        target: str,
        *,
        timeout: float | None = None,
        fresh: bool = False,
    ) -> Tab:
        """Find one tab by URL glob or target ID. Attach on demand.

        **Glob** (e.g. ``"*github.com*"``): searches pages and iframes,
        skips workers. ``timeout`` polls until a match appears. ``fresh``
        skips tabs that already matched at call time.

        **Target ID** (e.g. ``"9222:a81998"``): resolves any type including
        workers. Attaches if not already attached. ``timeout``/``fresh``
        are ignored.
        """
        if _is_target_id(target):
            return await self._get_by_id(target)
        return await self._get_by_glob(target, timeout=timeout, fresh=fresh)

    async def _get_by_id(self, target: str) -> Tab:
        """Resolve a target ID, attaching on demand if needed."""
        # Fast path: already attached
        _, prefix = target.split(":", 1)
        for cdp in self._session._sessions.values():
            chrome_id = cdp.target_info.get("targetId", "")
            if chrome_id[:6].lower() == prefix:
                return Tab(cdp, chrome_id, self.port)

        # Slow path: find in all targets and attach
        await self._ensure_connected()
        for t in await self._session.list_targets():
            tid = t.get("targetId", "")
            if tid and tid[:6].lower() == prefix:
                cdp = await self._session.attach(tid)
                if cdp is not None:
                    return Tab(cdp, tid, self.port)

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
    ) -> Tab:
        """Find one tab matching a URL glob. Skips workers."""
        # Snapshot existing matches so fresh=True can exclude them.
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
            # 1. Check already-attached tabs (skip workers)
            for cdp in self._session._sessions.values():
                if cdp.target_info.get("type", "") in WORKER_TYPES:
                    continue
                url = cdp.target_info.get("url", "")
                tid = cdp.target_info.get("targetId", "")
                if fnmatch.fnmatch(url, pattern) and tid not in exclude:
                    return Tab(cdp, tid, self.port)

            # 2. Search all Chrome targets, attach first match (skip workers)
            await self._ensure_connected()
            for t in await self._session.list_targets():
                if t.get("type", "") in WORKER_TYPES:
                    continue
                url = t.get("url", "")
                tid = t.get("targetId", "")
                if fnmatch.fnmatch(url, pattern) and tid and tid not in exclude:
                    cdp = await self._session.attach(tid)
                    if cdp is not None:
                        return Tab(cdp, tid, self.port)

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
        for cdp in self._session._sessions.values():
            chrome_id = cdp.target_info.get("targetId", "")
            if chrome_id[:6].lower() == prefix:
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
        for t in targets:
            if t.get("type", "") in WORKER_TYPES:
                continue
            tid = t.get("targetId", "")
            url = t.get("url", "")
            if fnmatch.fnmatch(url, pattern) and tid:
                # Check if already attached
                already = any(
                    cdp.target_info.get("targetId") == tid
                    for cdp in self._session._sessions.values()
                )
                if not already:
                    try:
                        await self._session.attach(tid)
                        newly_attached.append(tid)
                        self._session._watched_patterns.setdefault(pattern, set()).add(
                            tid
                        )
                    except Exception as exc:
                        logger.debug("Attach %s: %s", tid, exc)

        total = len(self._session._sessions)
        return (
            f"Attached {len(newly_attached)} new tab(s) for pattern '{pattern}'. "
            f"Total attached: {total}."
        )

    async def open(self, url: str) -> "Tab":
        """Create a new tab and attach to it.

        Target.createTarget → attach → return Tab.
        """
        await self._ensure_connected()
        result = await self._session.execute("Target.createTarget", {"url": url})
        tid = result["targetId"]
        await self._session.attach(tid)
        return self._resolve_attached(make_target(self.port, tid))

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
        return Rows(
            Tab(cdp, cdp.target_info.get("targetId", ""), self.port)
            for cdp in self._session._sessions.values()
        )

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

    async def disconnect(self) -> None:
        """Disconnect from Chrome."""
        if self._connected:
            try:
                await self._session.disconnect()
            except Exception:
                pass
            self._connected = False

    def format_tabs_nested(self) -> str:
        """Format attached tabs as nested text showing target hierarchy."""
        entries: list[dict] = []
        for cdp in self._session._sessions.values():
            info = cdp.target_info
            entries.append(
                {
                    "target": make_target(self.port, info.get("targetId", "")),
                    "type": info.get("type", "unknown"),
                    "url": info.get("url", ""),
                    "title": info.get("title", ""),
                    "parent_frame_id": info.get("parentFrameId", ""),
                    "opener_id": info.get("openerId", ""),
                }
            )

        # Build parent lookup: full chrome ID → short target ID
        id_to_short: dict[str, str] = {}
        for cdp in self._session._sessions.values():
            info = cdp.target_info
            full_id = info.get("targetId", "")
            id_to_short[full_id] = make_target(self.port, full_id)

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


class LazyBrowser:
    """Lazy descriptor injected into __main__.

    On first attribute access, bootstraps the real Browser object and
    replaces itself in __main__.__dict__.
    """

    def __init__(self) -> None:
        self._real: Browser | None = None

    def _bootstrap(self) -> Browser:
        if self._real is None:
            self._real = Browser()
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
        return "<Browser (lazy — call browser.watch() to connect)>"

    def __reduce__(self):  # type: ignore[override]
        raise TypeError("LazyBrowser is not serializable")
