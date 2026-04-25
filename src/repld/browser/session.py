"""BrowserSession: async WebSocket + sessionId multiplexing.

Single websockets connection to Chrome, _recv_loop task dispatching by
message shape, pending-command Futures keyed by msg_id, target discovery
via Target.setDiscoverTargets.
"""

import asyncio
import json
import logging
import urllib.request
from fnmatch import fnmatch
from typing import Any, Callable

from .cdp import CDPSession

# Target types that are infrastructure, not user-visible pages/iframes.
# Excluded from glob-based resolution in get()/watch()/_resolve_target().
WORKER_TYPES = frozenset({"service_worker", "shared_worker", "worker"})

logger = logging.getLogger(__name__)

__all__ = ["BrowserSession"]


class BrowserSession:
    """Browser-level WebSocket connection with CDP session multiplexing.

    Owns a single websockets connection to /devtools/browser/<id>.
    Multiple CDPSessions attach through this browser connection.
    """

    def __init__(self, port: int = 9222) -> None:
        self.port = port
        self._ws: Any = None  # websockets.ClientConnection
        self._recv_task: asyncio.Task | None = None

        # msg_id → asyncio.Future (globally unique per WS)
        self._next_id: int = 1
        self._pending: dict[int, asyncio.Future] = {}

        # sessionId → CDPSession
        self._sessions: dict[str, CDPSession] = {}

        # Watch patterns: glob pattern → set of target_ids matched
        self._watched_patterns: dict[str, set[str]] = {}

        # target_ids currently being attached (guards against concurrent duplicates)
        self._attaching: set[str] = set()

        # Callbacks for target lifecycle (set by Browser namespace)
        self._on_target_created: Callable[[dict, str], None] | None = None
        self._on_target_destroyed: Callable[[str], None] | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Connect to Chrome DevTools WebSocket endpoint."""
        import websockets  # type: ignore[import-untyped]

        # Fetch browser WS URL from /json/version using stdlib urllib
        try:
            with urllib.request.urlopen(
                f"http://localhost:{self.port}/json/version", timeout=5
            ) as resp:
                version_info = json.loads(resp.read())
        except Exception as exc:
            raise RuntimeError(
                f"Cannot reach Chrome on port {self.port}: {exc}"
            ) from exc

        ws_url = version_info.get("webSocketDebuggerUrl")
        if not ws_url:
            raise RuntimeError(
                f"No webSocketDebuggerUrl in /json/version response: {version_info}"
            )

        self._ws = await websockets.connect(
            ws_url,
            max_size=64 * 1024 * 1024,  # 64 MB
            ping_interval=30,
            ping_timeout=10,
        )

        # Start receive loop
        self._recv_task = asyncio.create_task(self._recv_loop(), name="repld-cdp-recv")

        # Enable target discovery for lifecycle events
        await self.execute("Target.setDiscoverTargets", {"discover": True})

    async def disconnect(self) -> None:
        """Close the WebSocket and cancel recv task."""
        if self._recv_task and not self._recv_task.done():
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

        # Fail all pending futures
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError("BrowserSession disconnected"))
        self._pending.clear()

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    async def execute(
        self,
        method: str,
        params: dict | None = None,
        session_id: str | None = None,
        timeout: float = 30,
    ) -> dict:
        """Send a CDP command and await the response."""
        if self._ws is None:
            raise RuntimeError("BrowserSession not connected")

        msg_id = self._next_id
        self._next_id += 1

        fut: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut

        msg: dict[str, Any] = {"id": msg_id, "method": method}
        if params:
            msg["params"] = params
        if session_id:
            msg["sessionId"] = session_id

        await self._ws.send(json.dumps(msg))

        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(msg_id, None)
            raise TimeoutError(f"CDP command {method} timed out after {timeout}s")

    # ------------------------------------------------------------------
    # Target management
    # ------------------------------------------------------------------

    async def attach(self, target_id: str) -> CDPSession | None:
        """Attach to a target and return a CDPSession.

        Returns the existing CDPSession if this target_id is already attached,
        or None if another attach call is already in-flight for this target.
        """
        # Guard: return existing session if already attached to this target
        for cdp in self._sessions.values():
            if cdp.target_info.get("targetId") == target_id:
                return cdp

        # Guard: another attach() call is already in flight for this target
        if target_id in self._attaching:
            return None
        self._attaching.add(target_id)

        try:
            result = await self.execute(
                "Target.attachToTarget", {"targetId": target_id, "flatten": True}
            )
            session_id = result["sessionId"]

            # Chrome may have already routed this session via auto-attach
            if session_id in self._sessions:
                return self._sessions[session_id]

            # Fetch target info
            targets_result = await self.execute("Target.getTargets")
            target_infos = targets_result.get("targetInfos", [])
            target_info = next(
                (t for t in target_infos if t.get("targetId") == target_id), {}
            )

            cdp = CDPSession(
                self.execute,
                session_id,
                target_info,
                self.port,
                loop=asyncio.get_running_loop(),
            )
            self._sessions[session_id] = cdp
            # Enable domains
            asyncio.create_task(cdp._enable_domains())
            return cdp
        finally:
            self._attaching.discard(target_id)

    async def detach(self, session_id: str) -> None:
        """Detach a session."""
        try:
            await self.execute("Target.detachFromTarget", {"sessionId": session_id})
        except Exception as exc:
            logger.debug("Detach %s: %s", session_id, exc)
        cdp = self._sessions.pop(session_id, None)
        if cdp is not None:
            cdp.cleanup()

    async def list_targets(self) -> list[dict]:
        """Return all Chrome targets."""
        result = await self.execute("Target.getTargets")
        return result.get("targetInfos", [])

    # ------------------------------------------------------------------
    # Pattern watch
    # ------------------------------------------------------------------

    def add_pattern(self, pattern: str) -> None:
        """Register a URL glob pattern and track already-matching sessions."""
        if pattern not in self._watched_patterns:
            self._watched_patterns[pattern] = set()
        for cdp in self._sessions.values():
            url = cdp.target_info.get("url", "")
            if fnmatch(url, pattern):
                self._watched_patterns[pattern].add(cdp.target_info.get("targetId", ""))

    def _resolve_target(self, target_info: dict) -> str | None:
        """Dual-key resolution: target_id → URL exact → opener → pattern.

        Returns the Chrome targetId string if it should be auto-attached,
        or None if it doesn't match any watch. Workers are never auto-attached.
        """
        target_id = target_info.get("targetId", "")
        url = target_info.get("url", "")

        # 0. Workers are infrastructure — never auto-attach via pattern.
        if target_info.get("type", "") in WORKER_TYPES:
            return None

        # 1. Already have a session or attach in flight for this target_id?
        if target_id in self._attaching:
            return None
        for cdp in self._sessions.values():
            if cdp.target_info.get("targetId") == target_id:
                return None  # already attached

        # 2. URL glob match
        for pattern in self._watched_patterns:
            if fnmatch(url, pattern):
                return target_id

        # 3. Opener match — OAuth popup or new tab from watched tab
        opener_id = target_info.get("openerId", "")
        if opener_id:
            for cdp in self._sessions.values():
                if cdp.target_info.get("targetId") == opener_id:
                    return target_id

        return None

    # ------------------------------------------------------------------
    # Recv loop
    # ------------------------------------------------------------------

    async def _recv_loop(self) -> None:
        """Receive and dispatch all WebSocket messages from Chrome."""
        try:
            async for raw in self._ws:  # type: ignore[union-attr]
                try:
                    data = json.loads(raw)
                except Exception:
                    continue
                await self._dispatch(data)
        except Exception as exc:
            logger.debug("recv_loop ended: %s", exc)
            # Fail any remaining pending futures
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(RuntimeError(f"WS recv loop ended: {exc}"))
            self._pending.clear()

    async def _dispatch(self, data: dict) -> None:
        """Route a parsed CDP message to the right handler."""
        if "id" in data:
            # Command response
            msg_id = data["id"]
            fut = self._pending.pop(msg_id, None)
            if fut and not fut.done():
                if "error" in data:
                    fut.set_exception(RuntimeError(str(data["error"])))
                else:
                    fut.set_result(data.get("result", {}))

        elif "method" in data:
            session_id = data.get("sessionId")
            if session_id:
                # Session-scoped event → route to CDPSession
                cdp = self._sessions.get(session_id)
                if cdp is not None:
                    cdp._handle_event(data)
            else:
                # Browser-level event
                try:
                    self._handle_browser_event(data)
                except Exception as exc:
                    logger.exception("_handle_browser_event error: %s", exc)

    def _handle_browser_event(self, data: dict) -> None:
        """Handle browser-level CDP events (target lifecycle)."""
        method = data.get("method")
        params = data.get("params", {})

        if method == "Target.targetCreated":
            target_info = params.get("targetInfo", {})
            matched_id = self._resolve_target(target_info)
            if matched_id and self._on_target_created:
                asyncio.create_task(
                    self._auto_attach(target_info, matched_id),
                    name=f"repld-auto-attach-{matched_id[:8]}",
                )

        elif method == "Target.targetDestroyed":
            chrome_tid = params.get("targetId", "")
            # Find and remove any session for this target
            to_remove = [
                sid
                for sid, cdp in self._sessions.items()
                if cdp.target_info.get("targetId") == chrome_tid
            ]
            for sid in to_remove:
                cdp = self._sessions.pop(sid, None)
                if cdp:
                    cdp.cleanup()
            if to_remove and self._on_target_destroyed:
                self._on_target_destroyed(chrome_tid)

        elif method == "Target.targetInfoChanged":
            target_info = params.get("targetInfo", {})
            chrome_tid = target_info.get("targetId", "")
            # Update target_info on existing session
            for cdp in self._sessions.values():
                if cdp.target_info.get("targetId") == chrome_tid:
                    cdp.target_info = target_info
                    return
            # Not attached yet — check if it now matches a pattern
            matched_id = self._resolve_target(target_info)
            if matched_id and self._on_target_created:
                asyncio.create_task(
                    self._auto_attach(target_info, matched_id),
                    name=f"repld-auto-attach-changed-{chrome_tid[:8]}",
                )

        elif method == "Inspector.detached":
            reason = params.get("reason", "")
            if "Render process gone" in reason:
                # Tab crashed — clean up sessions on that target
                session_id = data.get("sessionId", "")
                cdp = self._sessions.pop(session_id, None)
                if cdp:
                    cdp.cleanup()

    async def _auto_attach(self, target_info: dict, target_id: str) -> None:
        """Auto-attach to a newly-matched target."""
        try:
            await self.attach(target_id)
            # Update pattern tracking
            url = target_info.get("url", "")
            for pattern, ids in self._watched_patterns.items():
                if fnmatch(url, pattern):
                    ids.add(target_id)
            if self._on_target_created:
                self._on_target_created(target_info, target_id)
        except Exception as exc:
            logger.debug("Auto-attach %s failed: %s", target_id, exc)
