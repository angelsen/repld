"""Tab facade — user-friendly API over CDPSession.

Wraps CDPSession with JS eval, DOM interaction, network queries, and
console queries. Row/Rows types live in row.py; selector resolution
lives in selector.py.
"""

import asyncio
import base64
import json
import pathlib
from typing import Any

from .cdp import _CONTROLS_PREFIX, CDPSession
from .pin import _handle_binding, _LABEL_JS, _next_label_color, _PIN_JS
from .png import _model_dims, _resize_png
from .row import (
    Row,
    Rows,
    _dict_from_har_entry,
    _row_from_console,
    _row_from_har,
    _row_from_lifecycle,
    _row_from_sse,
)
from .selector import resolve as _resolve_selector

__all__ = ["Tab", "BrowserJSError"]

# Pin/pill heartbeat cadence. The JS-side pill self-removes if it hasn't
# heard from Python in _HEARTBEAT_STALE_MS — kept in lockstep with the
# Python-side give-up point (interval * max misses) via the same constants,
# substituted into _PIN_JS at injection time (see tab.py:_inject_pin).
_HEARTBEAT_INTERVAL_S = 5
_HEARTBEAT_MAX_MISSES = 3
_HEARTBEAT_STALE_MS = _HEARTBEAT_INTERVAL_S * _HEARTBEAT_MAX_MISSES * 1000


def _format_stack_trace(stack_trace: dict | None) -> str:
    """Render a CDP Runtime.StackTrace as a JS-style multi-line stack string."""
    if not stack_trace:
        return ""
    lines = []
    for frame in stack_trace.get("callFrames", []):
        name = frame.get("functionName") or "<anonymous>"
        url = frame.get("url", "")
        line = frame.get("lineNumber", 0) + 1
        col = frame.get("columnNumber", 0) + 1
        lines.append(f"    at {name} ({url}:{line}:{col})")
    return "\n".join(lines)


class BrowserJSError(Exception):
    """Raised when JavaScript evaluation throws an exception in the browser."""

    def __init__(
        self, text: str, stack: str = "", url: str = "", line: int = 0
    ) -> None:
        self.text = text
        self.stack = stack
        self.url = url
        self.line = line
        super().__init__(text)


class Tab:
    """User-facing facade over a CDPSession.

    Wraps a CDPSession and exposes a clean API for JavaScript eval,
    DOM interaction, network/console queries, and CDP passthrough.
    """

    def __init__(
        self,
        session: CDPSession,
        chrome_target_id: str,  # long Chrome-native ID; .target_id is the short form
        port: int = 9222,
        ready: str | None = None,
    ) -> None:
        self._session = session
        self._chrome_target_id = chrome_target_id
        self._port = port
        self._ready = ready

    @property
    def _pinned(self) -> bool:
        """Pin state lives on CDPSession (persists across Tab re-wrapping)."""
        return self._session._pinned

    @property
    def target_id(self) -> str:
        """Short target ID in '{port}:{6-char-hex}' format."""
        from . import make_target

        return make_target(self._port, self._chrome_target_id)

    @property
    def url(self) -> str:
        return self._session.target_info.get("url", "")

    @property
    def title(self) -> str:
        return self._session.target_info.get("title", "")

    @property
    def type(self) -> str:
        return self._session.target_info.get("type", "page")

    @property
    def parent_frame_id(self) -> str:
        return self._session.target_info.get("parentFrameId", "")

    @property
    def capture_bodies(self) -> bool:
        return self._session.capture_bodies

    @capture_bodies.setter
    def capture_bodies(self, value: bool) -> None:
        """Fire-and-forget toggle — the flag flips only once enable_fetch()/
        disable_fetch() actually completes, not when this setter returns.
        Callers that need the ordering guarantee should await
        enable_capture()/disable_capture() instead."""
        loop = self._session._loop
        if loop is None:
            raise RuntimeError("tab session has no event loop (not attached)")
        if value:
            loop.create_task(
                self._session.enable_fetch(),
                name=f"repld-fetch-enable-{self._chrome_target_id[:8]}",
            )
        else:
            loop.create_task(
                self._session.disable_fetch(),
                name=f"repld-fetch-disable-{self._chrome_target_id[:8]}",
            )

    async def enable_capture(self) -> None:
        """Enable proactive Fetch body capture. Awaitable."""
        await self._session.enable_fetch()

    async def disable_capture(self) -> None:
        """Disable proactive Fetch body capture. Awaitable."""
        await self._session.disable_fetch()

    # ------------------------------------------------------------------
    # Label API
    # ------------------------------------------------------------------

    @property
    def label(self) -> str | None:
        """Current label text, or None."""
        return self._session._label_text

    @label.setter
    def label(self, value: str | tuple[str, str] | None) -> None:
        """Set or clear the label bar. Async work is scheduled internally.

        Usage:
            tab.label = "Skantz Tools"              # auto-color
            tab.label = ("Skantz Tools", "#3b82f6")  # explicit color
            tab.label = None                         # remove
        """
        loop = self._session._loop
        if loop is None:
            raise RuntimeError("tab session has no event loop (not attached)")
        loop.create_task(self._set_label(value), name="repld-label-set")

    async def _set_label(self, value: str | tuple[str, str] | None) -> None:
        """Apply or remove the label bar."""
        session = self._session
        # Remove existing label script + DOM
        if session._label_script_id is not None:
            try:
                await session.execute(
                    "Page.removeScriptToEvaluateOnNewDocument",
                    {"identifier": session._label_script_id},
                )
            except Exception:
                pass
            try:
                await self.js(
                    "var el = document.getElementById('__repld_label_bar');"
                    "if (el) { el.remove(); document.body.style.paddingTop = ''; }"
                )
            except Exception:
                pass
            session._label_script_id = None
            session._label_text = None
            session._label_color = None

        if value is None:
            return

        if isinstance(value, tuple):
            text, color = value
        else:
            text, color = value, _next_label_color()

        js = _LABEL_JS.replace("%TEXT%", json.dumps(text)).replace("%COLOR%", color)

        result = await session.execute(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": js, "runImmediately": True},
        )
        session._label_script_id = result.get("identifier")
        session._label_text = text
        session._label_color = color

    # ------------------------------------------------------------------
    # Pin API
    # ------------------------------------------------------------------

    async def pin(self, reason: str = "") -> None:
        """Inject pill + beforeunload guard + heartbeat. Idempotent."""
        session = self._session
        if not session._pinned:
            session._pin_origin = await self.js("location.origin")
            session._pin_reason = reason
            await self._inject_pin()
            session._pinned = True
            session._heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(), name="repld-pill-heartbeat"
            )
        elif reason:
            session._pin_reason = reason
            await self.js(f"__repld_update({{reason: {json.dumps(reason)}}})")

    async def _inject_pin(self) -> None:
        """Set up the binding + inject the pill JS + re-apply the reason label."""
        session = self._session
        await self._setup_binding()
        js = _PIN_JS.replace("%STALE_MS%", str(_HEARTBEAT_STALE_MS)).replace(
            "%CHECK_MS%", str(_HEARTBEAT_INTERVAL_S * 1000)
        )
        await self.js(js)
        if session._pin_reason:
            await self.js(
                f"__repld_update({{reason: {json.dumps(session._pin_reason)}}})"
            )

    async def unpin(self) -> None:
        """Remove pill + beforeunload + heartbeat."""
        session = self._session
        if session._pinned:
            if session._heartbeat_task is not None:
                session._heartbeat_task.cancel()
                session._heartbeat_task = None
            await self.js("window.__repld_remove && window.__repld_remove()")
            session._pinned = False
            session._pin_reason = ""
            session._pin_origin = ""

    async def _heartbeat_loop(self) -> None:
        """Beat every 5s. Re-inject on same-origin reload; unpin on cross-origin."""
        session = self._session
        origin = session._pin_origin
        check = (
            "window.__repld_pill"
            " ? (window.__repld_hb = Date.now(), 'ok')"
            " : location.origin === %s ? 'reload' : 'gone'"
        ) % json.dumps(origin)
        misses = 0
        while True:
            await asyncio.sleep(_HEARTBEAT_INTERVAL_S)
            try:
                status = await self.js(check)
                misses = 0
            except Exception:
                misses += 1
                if misses >= _HEARTBEAT_MAX_MISSES:
                    break
                continue
            if status == "ok":
                continue
            if status == "reload":
                try:
                    await self._inject_pin()
                except Exception:
                    misses += 1
                    if misses >= _HEARTBEAT_MAX_MISSES:
                        break
                continue
            # Cross-origin — pin contract broken.
            from ..kernel import push_channel

            push_channel(
                f"pinned tab navigated away from {origin}",
                {"kind": "pin_lost", "target": self.target_id},
            )
            break
        session._pinned = False
        session._heartbeat_task = None
        session._pin_reason = ""
        session._pin_origin = ""

    async def _setup_binding(self) -> None:
        """Register __repld_resolve CDP binding for gate callbacks."""
        await self._session.execute("Runtime.addBinding", {"name": "__repld_resolve"})
        self._session._binding_handler = _handle_binding

    async def _show_gate(
        self, gate_id: str, kind: str, prompt: str, options: list[str] | None
    ) -> None:
        """Present a gate in this tab's pin UI."""
        if kind == "confirm":
            buttons = [
                {"label": "No", "value": False, "style": ""},
                {"label": "Yes", "value": True, "style": "primary"},
            ]
        elif kind == "choose" and options:
            buttons = [{"label": opt, "value": opt, "style": ""} for opt in options]
        else:
            return  # ask() not supported in pill — terminal only
        await self.js(
            f"__repld_gate({json.dumps(gate_id)}, {json.dumps(prompt)}, {json.dumps(buttons)})"
        )

    # ------------------------------------------------------------------
    # Gate convenience methods
    # ------------------------------------------------------------------

    async def confirm(self, prompt: str, **kw: Any) -> bool:
        """Gate routed to this tab's pill UI."""
        from ..gates import confirm as _confirm

        return await _confirm(prompt, tab=self, **kw)

    async def choose(self, prompt: str, options: list[str], **kw: Any) -> str:
        """Gate routed to this tab's pill UI."""
        from ..gates import choose as _choose

        return await _choose(prompt, options, tab=self, **kw)

    async def ask(self, prompt: str, **kw: Any) -> str:
        """Gate routed like confirm/choose — but the pill UI has no text
        input, so the response is always typed in the terminal."""
        from ..gates import ask as _ask

        return await _ask(prompt, tab=self, **kw)

    # ------------------------------------------------------------------
    # JS interaction
    @staticmethod
    def _is_session_gone(exc: Exception) -> bool:
        """True if the error indicates the CDP session was invalidated (HMR, navigation)."""
        msg = str(exc).lower()
        return (
            "session with given id not found" in msg
            or "no session with given id" in msg
        )

    async def _reattach(self) -> None:
        """Re-attach to the same target after session invalidation (HMR, navigation).

        The target ID usually survives — only the CDP session ID changes.
        The CDPSession object is preserved (event history, capture, pin and
        label state); the pin pill re-injects via the heartbeat loop, but the
        label's addScriptToEvaluateOnNewDocument registration died with the
        old session and must be re-applied here.  Waits for the ready signal
        (CSS selector or JS expression) if set, otherwise waits for
        document.readyState === "complete".
        """
        browser_session = self._session.browser_session
        if browser_session is None:
            raise RuntimeError("Cannot re-attach: no BrowserSession reference")

        await browser_session.reattach_session(self._session)

        if self._session._label_text is not None:
            await self._set_label(
                (self._session._label_text, self._session._label_color or "")
            )

        await self._await_ready_signal(
            self._ready or "document.readyState === 'complete'"
        )
        await asyncio.sleep(0.3)

    async def _await_ready_signal(self, ready: str, timeout: float = 10) -> None:
        """Wait for a ready signal — CSS selector or JS expression, by shape."""
        if ready.startswith((".", "#", "[", "data-")):
            # Poll via Runtime.evaluate — a DOM.getDocument nodeId goes stale
            # when the document is replaced mid-load, silently never matching.
            expr = f"!!document.querySelector({json.dumps(ready)})"
            failure = f"Ready signal not found after re-attach: {ready}"
        else:
            expr = ready
            failure = f"Ready signal not satisfied after re-attach: {ready}"
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            result = await self._session.execute(
                "Runtime.evaluate",
                {"expression": expr, "returnByValue": True},
            )
            if result.get("result", {}).get("value"):
                return
            await asyncio.sleep(0.1)
        raise RuntimeError(failure)

    async def _exec(
        self, method: str, params: dict | None = None, timeout: float = 30
    ) -> dict:
        """Execute a CDP command with session-gone recovery.

        On HMR reload or navigation, the Chrome session ID changes but the
        target ID stays the same. Detects "session not found", re-attaches,
        waits for the ready signal, and retries once.
        """
        try:
            return await self._session.execute(method, params, timeout)
        except RuntimeError as exc:
            if not self._is_session_gone(exc):
                raise
            await self._reattach()
            return await self._session.execute(method, params, timeout)

    # ------------------------------------------------------------------

    async def js(
        self,
        expr: str,
        *,
        await_promise: bool = True,
        user_gesture: bool = True,
    ) -> Any:
        """Evaluate JavaScript expression in the page context.

        Args:
            expr: JavaScript expression to evaluate.
            await_promise: Await a Promise result. Default awaits (like the
                DevTools console); pass False to return without awaiting.
            user_gesture: Simulate a user gesture (makes isTrusted=true).

        Returns:
            The evaluated result as a Python value.

        Raises:
            BrowserJSError: If the JS throws an exception.
        """
        result = await self._exec(
            "Runtime.evaluate",
            {
                "expression": expr,
                "userGesture": user_gesture,
                # replMode is how the DevTools console supports top-level await
                # (and let/const redeclaration across calls). Without it, any
                # `await` outside an async function is a parse-time SyntaxError.
                # replMode wraps the evaluation in its own completion promise,
                # which awaitPromise unwraps here — a promise *returned by* the
                # code keeps its identity (objectId) and is awaited below.
                "replMode": True,
                "awaitPromise": True,
            },
        )
        rv = self._js_result(result)

        if "objectId" in rv:
            if rv.get("subtype") == "promise" and await_promise is not False:
                result = await self._exec(
                    "Runtime.awaitPromise",
                    {"promiseObjectId": rv["objectId"], "returnByValue": True},
                )
            else:
                # Serialize object results by value (returnByValue on the
                # initial evaluate would flatten promises to {} instead).
                result = await self._exec(
                    "Runtime.callFunctionOn",
                    {
                        "objectId": rv["objectId"],
                        "functionDeclaration": "function () { return this; }",
                        "returnByValue": True,
                    },
                )
            rv = self._js_result(result)

        return rv.get("value")

    @staticmethod
    def _js_result(result: dict) -> dict:
        """Extract the result object from an evaluate-style response, raising
        BrowserJSError if the response carries exceptionDetails."""
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            exc_obj = details.get("exception", {})
            text = exc_obj.get("description") or details.get("text", "JavaScript error")
            stack = _format_stack_trace(details.get("stackTrace"))
            url = details.get("url", "")
            line = details.get("lineNumber", 0)
            raise BrowserJSError(text, stack, url, line)
        return result.get("result", {})

    async def _wait_for_node(
        self, selector: str, timeout: float = 2.0
    ) -> tuple[int, str]:
        """Auto-wait for an element. Returns (nodeId, js_expr).

        CSS selectors use DOM.querySelector (no JS eval, no focus steal).
        Custom selectors use Runtime.evaluate.  nodeId is 0 for the JS path.
        """
        resolved = _resolve_selector(selector)
        deadline = asyncio.get_running_loop().time() + timeout

        root_id = 0
        if resolved.css is not None:
            doc = await self._exec("DOM.getDocument")
            root_id = doc["root"]["nodeId"]

        while True:
            if resolved.css is not None:
                result = await self._exec(
                    "DOM.querySelector", {"nodeId": root_id, "selector": resolved.css}
                )
                node_id = result.get("nodeId", 0)
                if node_id:
                    return node_id, resolved.js
            else:
                result = await self._exec(
                    "Runtime.evaluate",
                    {
                        "expression": f"!!({resolved.js})",
                        "returnByValue": True,
                    },
                )
                if result.get("result", {}).get("value"):
                    return 0, resolved.js
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError(f"Element not found: {selector}")
            await asyncio.sleep(0.1)

    @staticmethod
    def _quad_center(quads: list) -> tuple[float, float]:
        """Center point from DOM.getContentQuads result."""
        quad = quads[0]
        xs = [quad[i] for i in range(0, 8, 2)]
        ys = [quad[i] for i in range(1, 8, 2)]
        return sum(xs) / 4, sum(ys) / 4

    async def _element_center(self, selector: str) -> tuple[float, float]:
        """Resolve selector to (x, y) center coordinates. Auto-waits up to 2s.

        CSS selectors: DOM.querySelector → DOM.getContentQuads (no JS).
        Custom selectors: Runtime.evaluate → getBoundingClientRect (JS).
        """
        node_id, js_expr = await self._wait_for_node(selector)
        if node_id:
            quads = await self._exec("DOM.getContentQuads", {"nodeId": node_id})
            return self._quad_center(quads["quads"])
        coords = await self._exec(
            "Runtime.evaluate",
            {
                "expression": f"""
(function() {{
    const el = {js_expr};
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return {{x: r.left + r.width/2, y: r.top + r.height/2}};
}})()
""",
                "returnByValue": True,
            },
        )
        result = coords.get("result", {})
        if result.get("value") is None:
            raise RuntimeError(f"Element not found: {selector}")
        pos = result["value"]
        return pos["x"], pos["y"]

    async def click(
        self,
        selector: str,
        *,
        button: str = "left",
        click_count: int = 1,
    ) -> None:
        """Click an element. Auto-waits up to 2s for the element to appear.

        Selector: CSS, text=Label, role=button[name='OK'], or tag:has-text('...')
        """
        x, y = await self._element_center(selector)

        for event_type in ("mousePressed", "mouseReleased"):
            await self._exec(
                "Input.dispatchMouseEvent",
                {
                    "type": event_type,
                    "x": x,
                    "y": y,
                    "button": button,
                    "clickCount": click_count,
                },
            )

    async def type_text(
        self,
        selector: str,
        text: str,
        *,
        delay_ms: int = 0,
        press_enter: bool = False,
    ) -> None:
        """Clear field and type text. Auto-waits up to 2s for the element.

        Selects all existing content then types over it.
        Selector: CSS, text=Label, role=textbox, label=Name, or tag:has-text('...')
        """
        node_id, js_expr = await self._wait_for_node(selector)

        if node_id:
            await self._exec("DOM.focus", {"nodeId": node_id})
            # Select all existing content so first keystroke replaces it
            await self._exec(
                "Runtime.evaluate",
                {
                    "expression": "document.execCommand('selectAll')",
                    "returnByValue": True,
                },
            )
        else:
            await self._exec(
                "Runtime.evaluate",
                {
                    "expression": (
                        f"(function() {{ const el = {js_expr};"
                        f" if (el) {{ el.focus(); if (el.select) el.select(); }} }})()"
                    ),
                    "returnByValue": True,
                },
            )

        # Type new text via key events
        for char in text:
            for event_type in ("keyDown", "keyUp"):
                await self._exec(
                    "Input.dispatchKeyEvent",
                    {
                        "type": event_type,
                        "text": char if event_type == "keyDown" else "",
                    },
                )
            if delay_ms > 0:
                await asyncio.sleep(delay_ms / 1000)

        if press_enter:
            await self.key("Enter")

    async def key(self, key: str) -> None:
        """Dispatch a keyDown+keyUp pair for a named key (e.g. "Enter", "Escape")."""
        for event_type in ("keyDown", "keyUp"):
            await self._exec(
                "Input.dispatchKeyEvent",
                {"type": event_type, "key": key, "code": key},
            )

    async def _touch(
        self, type: str, touch_points: list[dict], timeout: float = 3
    ) -> None:
        """Dispatch a touch event with a timeout. Raises TimeoutError if the page's
        touch handler blocks (e.g. preventDefault on complex apps like Messenger)."""
        await self._exec(
            "Input.dispatchTouchEvent",
            {"type": type, "touchPoints": touch_points},
            timeout=timeout,
        )

    async def tap(self, selector_or_x, y: float | None = None) -> None:
        """Touch tap. Accepts a selector or (x, y) coordinates.

        Uses Input.dispatchTouchEvent — triggers touchstart/touchend listeners
        that dispatchMouseEvent won't reach. Use for mobile Chrome via ADB.
        """
        if y is not None:
            x, y = float(selector_or_x), y
        else:
            x, y = await self._element_center(selector_or_x)

        tp = [{"x": x, "y": y}]
        await self._touch("touchStart", tp)
        await self._touch("touchEnd", [])

    async def swipe(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        *,
        steps: int = 10,
        duration_ms: int = 300,
    ) -> None:
        """Touch swipe from (x1,y1) to (x2,y2).

        Dispatches touchStart → touchMove × steps → touchEnd.
        For scrolling on mobile Chrome via ADB.
        """
        await self._touch("touchStart", [{"x": x1, "y": y1}])
        delay = duration_ms / steps / 1000
        for i in range(1, steps + 1):
            frac = i / steps
            cx = x1 + (x2 - x1) * frac
            cy = y1 + (y2 - y1) * frac
            await self._touch("touchMove", [{"x": cx, "y": cy}])
            await asyncio.sleep(delay)
        await self._touch("touchEnd", [])

    async def tree(self) -> list[str]:
        """Compact accessibility tree as text lines.

        Standalone read — no settle, no observation bundle.
        """
        from .observe import build_tree

        return await build_tree(self)

    async def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        body: "dict | str | None" = None,
        headers: "dict[str, str] | None" = None,
    ) -> dict:
        """In-page JS fetch with Python-ergonomic args.

        Returns {status: int, ok: bool, body: Any}.
        Body is auto-parsed as JSON when content-type is json. Content-Type
        defaults to application/json for a dict body, application/x-www-form-
        urlencoded for a string body — pass `headers={"Content-Type": ...}`
        to override (e.g. for a raw JSON string or plain text body).
        """
        body_js = "undefined"
        if body is not None:
            if isinstance(body, dict):
                body_js = json.dumps(json.dumps(body))  # JSON-encode the string
            else:
                body_js = json.dumps(str(body))

        h: dict[str, str] = {}
        if body is not None:
            h["Content-Type"] = (
                "application/json"
                if isinstance(body, dict)
                else "application/x-www-form-urlencoded"
            )
        if headers:
            # Caller headers win outright, including overriding the default
            # Content-Type above (matched case-insensitively per HTTP spec).
            ct_key = next((k for k in h if k.lower() == "content-type"), None)
            if ct_key and any(k.lower() == "content-type" for k in headers):
                del h[ct_key]
            h.update(headers)
        headers_js = json.dumps(h) if h else "undefined"

        code = f"""
(async function() {{
  const opts = {{
    method: {json.dumps(method)},
    body: {body_js},
    headers: {headers_js},
  }};
  const r = await fetch({json.dumps(url)}, opts);
  const ct = r.headers.get('content-type') || '';
  const text = await r.text();
  let body = text;
  if (ct.includes('json') && text) {{
    try {{ body = JSON.parse(text); }} catch(e) {{}}
  }}
  return {{status: r.status, ok: r.ok, body: body}};
}})()
"""
        return await self.js(code, await_promise=True)

    async def wait_for(self, selector: str, *, timeout: float = 5.0) -> None:
        """Wait for an element matching *selector* to appear in the DOM.

        Uses the same selector syntax as click/type_text (CSS, text=, role=,
        label=, :has-text).  Polls every 0.1s up to *timeout* seconds.
        Raises RuntimeError if the element never appears.
        """
        await self._wait_for_node(selector, timeout=timeout)

    async def wait_for_idle(self, *, timeout: float = 5.0, quiet: float = 0.5) -> int:
        """Wait for network idle. Returns settle time in ms."""
        from .observe import settle

        return await settle([self], timeout=timeout, quiet=quiet)

    async def _wait_ready(self, timeout: float = 10) -> None:
        """Wait for the ready signal after navigation/reload, then network idle."""
        ready = self._ready or "document.readyState === 'complete'"
        await self._await_ready_signal(ready, timeout)
        await self.wait_for_idle(timeout=2.0, quiet=0.3)

    async def reload(self) -> None:
        """Reload the page, then wait for the ready signal."""
        await self._exec("Page.reload")
        await self._wait_ready()

    async def navigate(self, url: str) -> None:
        """Navigate to URL, then wait for the ready signal."""
        await self._exec("Page.navigate", {"url": url})
        await self._wait_ready()

    async def screenshot(
        self,
        *,
        full_page: bool = False,
        path: str | None = None,
    ) -> dict:
        """Capture a PNG screenshot, resized to the vision API token grid.

        Captures full-res from CDP (no clip.scale — that races the compositor),
        then resizes via Pillow off the event loop (in a thread executor, so
        the resize's CPU cost doesn't stall the kernel's shared asyncio loop).
        """
        import time

        from ..tasks import RUNTIME_DIR, _ensure_spill_dir

        params: dict = {"format": "png"}
        if full_page:
            params["captureBeyondViewport"] = True

        metrics = await self._exec("Page.getLayoutMetrics", {})
        vp = metrics.get("cssVisualViewport", {})
        src_w = int(vp.get("clientWidth", 0))
        src_h = int(vp.get("clientHeight", 0))

        result = await self._exec("Page.captureScreenshot", params)
        img_bytes = base64.b64decode(result.get("data", ""))

        tgt_w, tgt_h = (
            _model_dims(src_w, src_h) if src_w > 0 and src_h > 0 else (src_w, src_h)
        )
        if tgt_w < src_w or tgt_h < src_h:
            try:
                img_bytes = await asyncio.get_running_loop().run_in_executor(
                    None, _resize_png, img_bytes, tgt_w, tgt_h
                )
            except Exception:
                # Unparseable/exotic PNG variant — report the untouched image
                # accurately rather than resizing metadata that doesn't match
                # the bytes actually written.
                tgt_w, tgt_h = src_w, src_h
        scale = (
            min(tgt_w / src_w, tgt_h / src_h)
            if src_w > 0 and src_h > 0 and (tgt_w < src_w or tgt_h < src_h)
            else 1.0
        )

        if path:
            out = pathlib.Path(path)
        else:
            _ensure_spill_dir()
            tid = self.target_id.replace(":", "-")
            out = RUNTIME_DIR / f"screenshot-{tid}-{int(time.time())}.png"
        await asyncio.get_running_loop().run_in_executor(
            None, out.write_bytes, img_bytes
        )
        return {
            "path": str(out),
            "source": {"width": src_w, "height": src_h},
            "model": {"width": tgt_w, "height": tgt_h},
            "scale": round(scale, 4),
            "bytes": len(img_bytes),
        }

    # ------------------------------------------------------------------
    # Query methods (sync DuckDB)
    # ------------------------------------------------------------------

    def _filtered_query(
        self, source: str, conditions: list[str], bind_params: list[Any], tail: str
    ) -> list:
        """SELECT * FROM `source` with an optional WHERE built from conditions."""
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        sql = f"SELECT * FROM {source} {where} {tail}"
        return self._session.query(sql, bind_params if bind_params else None)

    @staticmethod
    def _like_pattern(url: str) -> str:
        """Convert a `*`-glob URL filter to a SQL LIKE pattern."""
        pattern = url.replace("*", "%")
        if not pattern.startswith("%"):
            pattern = "%" + pattern
        if not pattern.endswith("%"):
            pattern = pattern + "%"
        return pattern

    def network(
        self,
        *,
        url: str | None = None,
        method: str | None = None,
        status: int | None = None,
        type: str | None = None,
        since: float | None = None,
        include_assets: bool = False,
    ) -> list[Row]:
        """Query the HAR summary view with optional filters."""
        conditions: list[str] = []
        bind_params: list[Any] = []

        if url:
            conditions.append("url LIKE ?")
            bind_params.append(self._like_pattern(url))
        if method:
            conditions.append("method = ?")
            bind_params.append(method.upper())
        if status is not None:
            conditions.append("status = ?")
            bind_params.append(status)
        if type:
            conditions.append("type = ?")
            bind_params.append(type)
        if since is not None:
            conditions.append("CAST(last_activity AS DOUBLE) >= ?")
            bind_params.append(since)
        if not include_assets:
            conditions.append("is_asset = false")

        rows = self._filtered_query(
            "har_summary", conditions, bind_params, "ORDER BY id DESC LIMIT 500"
        )
        return Rows(_row_from_har(r, self._session) for r in rows)

    def console(
        self,
        *,
        level: str | None = None,
        source: str | None = None,
        since: float | None = None,
    ) -> list[Row]:
        """Query the console_entries view with optional filters."""
        conditions: list[str] = []
        bind_params: list[Any] = []

        if level:
            conditions.append("level = ?")
            bind_params.append(level)
        if source:
            conditions.append("source = ?")
            bind_params.append(source)
        if since is not None:
            conditions.append("CAST(timestamp AS DOUBLE) >= ?")
            bind_params.append(since)

        rows = self._filtered_query(
            "console_entries", conditions, bind_params, "LIMIT 200"
        )
        return Rows(_row_from_console(r, self._session) for r in rows)

    # ------------------------------------------------------------------
    # Controls protocol
    # ------------------------------------------------------------------

    async def controls(self) -> dict | None:
        """Snapshot controls from window.controls.describeAll(). Returns None if absent."""
        result = await self._exec(
            "Runtime.evaluate",
            {
                "expression": "window.controls?.describeAll()",
                "returnByValue": True,
                "awaitPromise": False,
            },
        )
        value = result.get("result", {}).get("value")
        return value if isinstance(value, dict) else None

    async def invoke(self, control: str, action: str, args: dict | None = None) -> dict:
        """Invoke a control action via window.controls.invoke(). Returns InvokeResult."""
        args_js = json.dumps(args) if args else "{}"
        code = f"window.controls.invoke({json.dumps(control)}, {json.dumps(action)}, {args_js})"
        result = await self._exec(
            "Runtime.evaluate",
            {"expression": code, "returnByValue": True, "awaitPromise": True},
        )
        value = result.get("result", {}).get("value")
        if value is None:
            desc = result.get("result", {}).get("description", "")
            raise RuntimeError(f"invoke failed: {desc}")
        return value

    def control_observations(self) -> list[dict]:
        """Parsed __controls__ observations from console.debug messages."""
        rows = self._session.query(
            f"SELECT text FROM console_entries WHERE level = 'debug' AND text LIKE '{_CONTROLS_PREFIX}%' ORDER BY id DESC LIMIT 100"
        )
        results = []
        for row in rows:
            raw = row[0]
            if not isinstance(raw, str):
                continue
            prefix = f"{_CONTROLS_PREFIX} "
            if raw.startswith(prefix):
                raw = raw[len(prefix) :]
            try:
                results.append(json.loads(raw))
            except (json.JSONDecodeError, TypeError):
                continue
        return results

    def sse(
        self,
        *,
        url: str | None = None,
        event_name: str | None = None,
        since: float | None = None,
    ) -> list[Row]:
        """Query SSE (EventSource) messages received on this tab."""
        conditions: list[str] = []
        bind_params: list[Any] = []

        if url:
            conditions.append(
                "request_id IN (SELECT request_id FROM har_summary WHERE url LIKE ?)"
            )
            bind_params.append(self._like_pattern(url))
        if event_name:
            conditions.append("event_name = ?")
            bind_params.append(event_name)
        if since is not None:
            conditions.append("CAST(timestamp AS DOUBLE) >= ?")
            bind_params.append(since)

        rows = self._filtered_query(
            "sse_entries", conditions, bind_params, "ORDER BY rowid DESC LIMIT 500"
        )
        return Rows(_row_from_sse(r, self._session) for r in rows)

    def lifecycle(
        self,
        *,
        name: str | None = None,
        since: float | None = None,
    ) -> list[Row]:
        """Query Page.lifecycleEvent events for this tab.

        Event names (from Chromium source):
          init, DOMContentLoaded, load, firstPaint, firstContentfulPaint,
          firstImagePaint, firstMeaningfulPaintCandidate, firstMeaningfulPaint,
          networkAlmostIdle, networkIdle, InteractiveTime, commit (catch-up only)
        """
        conditions: list[str] = []
        bind_params: list[Any] = []

        if name:
            conditions.append("name = ?")
            bind_params.append(name)
        if since is not None:
            conditions.append("CAST(timestamp AS DOUBLE) >= ?")
            bind_params.append(since)

        rows = self._filtered_query(
            "lifecycle_entries",
            conditions,
            bind_params,
            "ORDER BY rowid DESC LIMIT 500",
        )
        return Rows(_row_from_lifecycle(r, self._session) for r in rows)

    def clear(self) -> None:
        """Clear all captured events (network + console + SSE + lifecycle) for this tab."""
        self._session.clear_events()

    def body(self, request_id: str | int) -> dict:
        """Fetch the response body for a request_id."""
        return self._session.fetch_body(str(request_id))

    def request(self, request_id: str | int) -> dict:
        """Return the full HAR entry for a request_id as a structured dict.

        Returns request/response headers, postData, auth scheme, timing —
        everything except the response body (use .body() for that).
        """
        rows = self._session.query(
            "SELECT * FROM har_entries WHERE request_id = ? ORDER BY id DESC LIMIT 1",
            [str(request_id)],
        )
        if not rows:
            raise RuntimeError(f"No request found for id: {request_id}")
        return _dict_from_har_entry(rows[0])

    async def cookies(self) -> list[dict]:
        """Return all cookies for this tab via CDP."""
        result = await self._exec("Network.getCookies")
        return result.get("cookies", [])

    async def cdp(self, method: str, **params: Any) -> dict:
        """Raw CDP passthrough."""
        return await self._exec(method, params if params else None)

    def __repr__(self) -> str:
        return f"<Tab {self.target_id} {self.url!r}>"
