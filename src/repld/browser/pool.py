"""`BrowserPool` — N Chrome instances behind one object — and the lazy descriptor.

`BrowserPool` is what `browser` actually is in a cell: a facade that routes a
target ID to the port named in it, and fans a glob out across every connected
`Browser`. `LazyBrowser` sits in front of *that*, so importing repld — or
merely touching `browser` — opens no WebSocket.

Not a forwarding layer, which is why it is a class rather than a set of module
functions: each method here has real fan-out semantics of its own (one shared
deadline across browsers in `get`, first-connected-wins in `open`, join-and-drop
-empties in `format_tabs_nested`) that a single `Browser` has no notion of.

**The snapshot rule applies here too, and the reason is different from the one
in `browser.py`.** The methods below are mostly `async`, so the off-loop
argument doesn't reach them — but an `await` inside `for b in
self._browsers.values()` hands control back to the loop, another task runs
`connect()` or `disconnect()`, and resuming raises the same "dictionary changed
size during iteration". Reading the rule as "sync methods snapshot" is exactly
what left this class unguarded for five releases. See the full argument in
`browser.py`; the short form is: snapshot when the body can yield, or when the
method may run off-loop.
"""

import asyncio
import logging
import os
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from .browser import Browser
from .row import Rows
from .tab import Tab
from .target import (
    _NO_TABS,
    TabNotFoundError,
    _is_target_id,
    _print_browser_help,
    _split_target,
    make_target,
)

__all__ = ["BrowserPool", "LazyBrowser"]

logger = logging.getLogger(__name__)


class BrowserPool:
    """Manages multiple Browser instances across Chrome ports.

    Delegates watch/get/tabs/pages across all connected instances.
    Target IDs (e.g. ``42829:abc123``) route to the right Browser by port prefix.
    """

    def __init__(self) -> None:
        self._browsers: dict[int, Browser] = {}
        # Guards the registry mutation in `connect`, which is the same
        # check-then-act-across-an-await as Browser._ensure_connected one level
        # up: two callers both missing the port both built a Browser, both
        # connected it, and the loser was left in `self._browsers`' place with a
        # live WebSocket nothing would ever close.
        self._connect_lock = asyncio.Lock()

    async def _ensure_any(self) -> None:
        """Auto-connect to the default port if no browsers are connected.

        Deliberately unlocked: `connect` does its own check-then-act under the
        lock, so a second caller arriving here concurrently finds the port
        already connected and gets the incumbent back. Taking the lock here too
        would just deadlock on the re-entrant call.
        """
        if not any(b._connected for b in list(self._browsers.values())):
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
        async with self._connect_lock:
            existing = self._browsers.get(port)
            if existing is not None and existing._connected:
                return existing
            # Reuse a disconnected Browser rather than replacing it: callers may
            # be holding the object, and `_ensure_connected` on it either
            # reconnects in place (preserving its CDPSessions and their event
            # stores) or connects fresh, both of which beat orphaning it.
            b = existing if existing is not None else Browser(port=port)
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
        # Snapshot, then remove by key rather than `clear()`. A `connect()`
        # landing on the loop mid-sweep is invisible to the snapshot, and a
        # blanket clear would drop that brand-new Browser from the registry
        # with a live WebSocket and recv task nothing would ever close —
        # exactly the leak `connect`'s own lock exists to prevent. `count`
        # comes from the snapshot for the same reason: it has to report what
        # was actually disconnected.
        targets = list(self._browsers.items())
        count = len(targets)
        for port_, b in targets:
            try:
                await b.disconnect()
            except Exception:
                pass
            self._browsers.pop(port_, None)
        self._save_hint()
        return f"Disconnected {count} browser(s)."

    def browser_for(self, target: str) -> Browser:
        """Resolve a target ID like '42829:abc123' to its Browser instance."""
        port_str, _ = _split_target(target)
        try:
            port = int(port_str)
        except ValueError:
            # `from None`: the int() failure says "invalid literal for int()"
            # about a substring the caller never wrote, under a "during
            # handling of the above exception" banner. This message already
            # names the whole target — the cause is noise in a traceback that
            # lands in an agent's exec output.
            raise RuntimeError(f"Invalid target ID: {target}") from None
        b = self._browsers.get(port)
        if b is None:
            raise RuntimeError(
                f"No browser on port {port}. Connected: {list(self._browsers.keys())}"
            )
        return b

    def resolve_tab(self, target_id: str) -> "Tab":
        """Find an attached Tab by its raw Chrome targetId, across all ports."""
        for port, b in list(self._browsers.items()):
            if not b._connected:
                continue
            cdp = b._session.find_by_target_id(target_id)
            if cdp is not None:
                return Tab(cdp, target_id, port)
        raise RuntimeError(f"tab not attached: {target_id}")

    def snapshot(self) -> dict:
        """Serializable state for the dashboard: connection + tab list."""
        tab_list = []
        for port, b in list(self._browsers.items()):
            if not b._connected:
                continue
            for cdp in list(b._session._sessions.values()):
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
        # No _connected guard: `patterns` already skips disconnected browsers,
        # so when nothing is connected it is empty and this is [] either way.
        patterns = [
            {"pattern": p, "count": sum(1 for t in tab_list if fnmatch(t["url"], p))}
            for p in self.patterns
        ]
        return {
            "connected": self._connected,
            "ports": self.ports,
            "patterns": patterns,
            "tabs": tab_list,
        }

    @property
    def ports(self) -> list[int]:
        return list(self._browsers.keys())

    @property
    def connected_ports(self) -> list[int]:
        """Ports whose Browser is currently connected (for hint persistence)."""
        return [p for p, b in list(self._browsers.items()) if b._connected]

    @property
    def tabs(self) -> Rows:
        """Tabs from all connected browsers."""
        all_tabs = []
        for b in list(self._browsers.values()):
            if b._connected:
                all_tabs.extend(b.tabs)
        return Rows(all_tabs)

    @property
    def patterns(self) -> list[str]:
        """Watch patterns from all connected browsers."""
        result = []
        for b in list(self._browsers.values()):
            if b._connected:
                result.extend(b.patterns)
        return result

    async def pages(self) -> list[dict]:
        """All Chrome targets across all connected browsers."""
        await self._ensure_any()
        result = []
        for b in list(self._browsers.values()):
            if b._connected:
                result.extend(await b.pages())
        return result

    async def watch(self, pattern: str) -> str:
        """Watch a pattern across all connected browsers."""
        await self._ensure_any()
        results = []
        for b in list(self._browsers.values()):
            results.append(await b.watch(pattern))
        self._save_hint()
        return "\n".join(results)

    async def detach(self, pattern: str | None = None) -> str:
        """Detach tabs across all connected browsers."""
        results = []
        for b in list(self._browsers.values()):
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

    def no_capture(self, pattern: str) -> str:
        """Skip Fetch body capture on tabs whose URL matches this glob.

        For servers where Chrome's Fetch-domain response replay trips CORB/ORB
        on same-origin resources — see cdp.py's `_no_capture_patterns` comment.
        """
        from .cdp import _no_capture_patterns

        _no_capture_patterns.add(pattern)
        self._save_hint()
        return f"no_capture {pattern!r} ({len(_no_capture_patterns)} active)"

    def capture_ok(self, pattern: str) -> str:
        """Undo `no_capture` for this pattern."""
        from .cdp import _no_capture_patterns

        _no_capture_patterns.discard(pattern)
        self._save_hint()
        return f"capture_ok {pattern!r} ({len(_no_capture_patterns)} active)"

    @property
    def no_capture_patterns(self) -> list[str]:
        """URL globs currently exempted from Fetch body capture."""
        from .cdp import _no_capture_patterns

        return sorted(_no_capture_patterns)

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
            # Auto-connect here too, not only on the glob path below. A target
            # ID names the port it belongs to, so `browser.get("9222:abc123")`
            # on a cold pool used to fail with "No browser on port 9222.
            # Connected: []" — a lazy pool refusing to do the one thing it is
            # lazy in order to do, on the more specific of the two arguments.
            await self._ensure_any()
            b = self.browser_for(target)
            return await b.get(target, timeout=timeout, ready=ready)
        await self._ensure_any()
        # One deadline shared across all browsers — otherwise each browser
        # gets the full `timeout`, so N browsers can take up to N*timeout.
        deadline = (
            asyncio.get_running_loop().time() + timeout if timeout is not None else None
        )
        for b in list(self._browsers.values()):
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

    async def open(self, url: str, *, ready: str | None = None) -> Tab:
        """Open a URL in the first connected browser."""
        await self._ensure_any()
        for b in list(self._browsers.values()):
            if b._connected:
                return await b.open(url, ready=ready)
        raise RuntimeError("No browsers connected")

    async def acquire(
        self,
        pattern: str,
        *,
        open: str | None = None,
        ready: str | None = None,
        timeout: float | None = None,
    ) -> Tab:
        """Find a tab matching `pattern`, or open one if none is open yet.

        `open` is the URL to navigate to on a miss — defaults to `pattern`
        itself, so `browser.acquire(url)` works when pattern and open-URL are
        the same. Replaces the try-`get`/except-`open` dance every gist that
        talks to one recurring site ends up hand-rolling.
        """
        try:
            return await self.get(pattern, timeout=timeout, ready=ready)
        except TabNotFoundError:
            return await self.open(open if open is not None else pattern, ready=ready)

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
        for b in list(self._browsers.values()):
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
        for b in list(self._browsers.values()):
            results.append(b.clear())
        return "\n".join(results)

    def format_tabs_nested(self) -> str:
        parts = []
        for b in list(self._browsers.values()):
            if b._connected:
                text = b.format_tabs_nested()
                if text != _NO_TABS:
                    parts.append(text)
        return "\n".join(parts) if parts else _NO_TABS

    @property
    def _connected(self) -> bool:
        return any(b._connected for b in list(self._browsers.values()))

    def help(self) -> None:
        _print_browser_help()

    def __repr__(self) -> str:
        if not self._browsers:
            return "<BrowserPool (no connections)>"
        parts = []
        for port, b in list(self._browsers.items()):
            n = len(b._session._sessions) if b._connected else 0
            parts.append(f"{port}({n})")
        return f"<BrowserPool [{', '.join(parts)}] patterns={self.patterns}>"


class LazyBrowser:
    """Lazy descriptor injected into __main__.

    First attribute access constructs an empty BrowserPool; nothing reaches
    Chrome until an operation needs a browser, at which point `_ensure_any`
    connects to the default port. That split is the point — importing repld,
    or merely touching `browser`, must not open a WebSocket.
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
