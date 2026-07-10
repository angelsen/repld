"""BrowserSession: async WebSocket + sessionId multiplexing.

Single websockets connection to Chrome, _recv_loop task dispatching by
message shape, pending-command Futures keyed by msg_id, target discovery
via Target.setDiscoverTargets.

Auto-reconnects on WebSocket failure (network change, laptop sleep,
Chrome restart). execute() detects a dead connection, triggers reconnect,
re-attaches all previously attached targets (preserving CDPSession state
including DuckDB event stores), and retries the command once.
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

        # Reconnect state
        self._reconnect_lock = asyncio.Lock()
        self._reconnecting: bool = False
        # old sessionId → new sessionId (valid for one reconnect cycle)
        self._session_remap: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self, *, discover: bool = True) -> None:
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
        if discover:
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

    def _is_connected(self) -> bool:
        """True if the WebSocket is alive and the recv loop is running."""
        if self._ws is None:
            return False
        if self._recv_task is not None and self._recv_task.done():
            return False
        return True

    # ------------------------------------------------------------------
    # Auto-reconnect
    # ------------------------------------------------------------------

    @staticmethod
    def _is_recoverable(exc: Exception) -> bool:
        """True if exc indicates a dead WebSocket (not a CDP-level error)."""
        # TimeoutError inherits from OSError but is a CDP command timeout, not a socket death.
        if isinstance(exc, TimeoutError):
            return False
        import websockets.exceptions  # type: ignore[import-untyped]

        if isinstance(exc, websockets.exceptions.ConnectionClosed):
            return True
        # recv_loop died, or execute() ran before connect — futures get these.
        if isinstance(exc, RuntimeError) and (
            "recv loop ended" in str(exc) or "not connected" in str(exc)
        ):
            return True
        # Socket-level errors (broken pipe, connection reset)
        if isinstance(exc, OSError):
            return True
        return False

    async def _reconnect(self) -> None:
        """Tear down the dead WebSocket, reconnect, and re-attach all targets.

        CDPSession objects (and their DuckDB event stores) are preserved;
        only the Chrome sessionId is updated. Watch patterns survive.
        Serialized by _reconnect_lock so concurrent callers don't race.
        """
        async with self._reconnect_lock:
            if self._is_connected():
                return  # another caller already reconnected

            self._reconnecting = True
            try:
                # Save CDPSessions keyed by Chrome targetId
                old_cdps: dict[str, CDPSession] = {}
                for cdp in self._sessions.values():
                    tid = cdp.chrome_target_id
                    if tid:
                        old_cdps[tid] = cdp

                # Tear down old connection
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
                self._pending.clear()
                self._sessions.clear()
                self._session_remap.clear()

                # New WebSocket (no target discovery yet — re-attach first)
                await self.connect(discover=False)

                # Re-attach old targets, preserving CDPSession state
                for target_id, cdp in old_cdps.items():
                    old_sid = cdp._session_id
                    try:
                        result = await self.execute(
                            "Target.attachToTarget",
                            {"targetId": target_id, "flatten": True},
                        )
                        new_sid = result["sessionId"]
                        cdp._session_id = new_sid
                        self._sessions[new_sid] = cdp
                        self._session_remap[old_sid] = new_sid
                        had_fetch = cdp._fetch_enabled
                        cdp._fetch_enabled = False
                        await cdp._enable_domains()
                        if had_fetch:
                            await cdp.enable_fetch()
                    except Exception as exc:
                        logger.debug(
                            "reconnect: re-attach %s failed: %s", target_id, exc
                        )
                        cdp.cleanup()

                # Now enable target discovery (picks up new tabs)
                await self.execute("Target.setDiscoverTargets", {"discover": True})

                logger.info(
                    "Reconnected to Chrome on port %d (%d sessions restored)",
                    self.port,
                    len(self._sessions),
                )
            finally:
                self._reconnecting = False

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
        """Send a CDP command and await the response.

        On WebSocket failure, triggers reconnect and retries once.
        During reconnect (_reconnecting=True), failures propagate directly
        to avoid recursive reconnect loops.
        """
        try:
            return await self._execute_once(method, params, session_id, timeout)
        except Exception as exc:
            if self._reconnecting or not self._is_recoverable(exc):
                raise
            # Connection died — reconnect and retry
            await self._reconnect()
            # Remap session_id if it changed during reconnect
            if session_id and session_id in self._session_remap:
                session_id = self._session_remap[session_id]
            return await self._execute_once(method, params, session_id, timeout)

    def _build_msg(
        self, method: str, params: dict | None, session_id: str | None
    ) -> tuple[int, str]:
        """Allocate a msg id and serialize a CDP request."""
        msg_id = self._next_id
        self._next_id += 1

        msg: dict[str, Any] = {"id": msg_id, "method": method}
        if params:
            msg["params"] = params
        if session_id:
            msg["sessionId"] = session_id

        return msg_id, json.dumps(msg)

    async def _execute_once(
        self,
        method: str,
        params: dict | None = None,
        session_id: str | None = None,
        timeout: float = 30,
    ) -> dict:
        """Send a CDP command without retry."""
        if self._ws is None:
            raise RuntimeError("BrowserSession not connected")

        msg_id, payload = self._build_msg(method, params, session_id)

        fut: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut

        await self._ws.send(payload)

        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(msg_id, None)
            raise TimeoutError(f"CDP command {method} timed out after {timeout}s")
        except asyncio.CancelledError:
            self._pending.pop(msg_id, None)
            raise

    async def send_nowait(
        self,
        method: str,
        params: dict | None = None,
        session_id: str | None = None,
    ) -> None:
        """Send a CDP command without awaiting the response.

        For fire-and-forget commands (Fetch.continueResponse, etc.) where
        the result is always empty and the latency of the roundtrip is waste.
        """
        if self._ws is None:
            raise RuntimeError("BrowserSession not connected")

        _msg_id, payload = self._build_msg(method, params, session_id)
        await self._ws.send(payload)

    # ------------------------------------------------------------------
    # Target management
    # ------------------------------------------------------------------

    def find_by_target_id(self, target_id: str) -> CDPSession | None:
        """Return the attached CDPSession for a Chrome targetId, or None."""
        for cdp in self._sessions.values():
            if cdp.target_info.get("targetId") == target_id:
                return cdp
        return None

    async def attach(self, target_id: str) -> CDPSession | None:
        """Attach to a target and return a CDPSession.

        Returns the existing CDPSession if this target_id is already attached,
        or None if another attach call is already in-flight for this target.
        """
        # Guard: return existing session if already attached to this target
        existing = self.find_by_target_id(target_id)
        if existing is not None:
            return existing

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
                send_nowait=self.send_nowait,
                browser_session=self,
            )
            self._sessions[session_id] = cdp
            asyncio.create_task(
                cdp._enable_domains(),
                name=f"repld-domains-{target_id[:8]}",
            )
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
        if self.find_by_target_id(target_id) is not None:
            return None  # already attached

        # 2. URL glob match
        for pattern in self._watched_patterns:
            if fnmatch(url, pattern):
                return target_id

        # 3. Opener match — OAuth popup or new tab from watched tab
        opener_id = target_info.get("openerId", "")
        if opener_id and self.find_by_target_id(opener_id) is not None:
            return target_id

        return None

    # ------------------------------------------------------------------
    # Recv loop
    # ------------------------------------------------------------------

    async def _recv_loop(self) -> None:
        """Receive and dispatch all WebSocket messages from Chrome."""
        count = 0
        try:
            async for raw in self._ws:  # type: ignore[union-attr]
                try:
                    data = json.loads(raw)
                except Exception:
                    continue
                await self._dispatch(data)
                count += 1
                if count % 50 == 0:
                    await asyncio.sleep(0)
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
                    err = data["error"]
                    if isinstance(err, dict):
                        msg = err.get("message", str(err))
                        if "data" in err:
                            msg = f"{msg} ({err['data']})"
                    else:
                        msg = str(err)
                    fut.set_exception(RuntimeError(msg))
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
            cdp = self.find_by_target_id(chrome_tid)
            if cdp is not None:
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
