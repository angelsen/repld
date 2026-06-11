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

from .cdp import CDPSession
from .row import Row, Rows, _dict_from_har_entry, _row_from_console, _row_from_har
from .selector import resolve as _resolve_selector

__all__ = ["Tab", "BrowserJSError"]

# ---------------------------------------------------------------------------
# Pill JS/CSS blob — injected via Runtime.evaluate on tab.pin()
# ---------------------------------------------------------------------------
_PIN_JS = r"""
(function() {
  if (window.__repld_pill) {
    // Already injected — idempotent, just ensure update function is live
    return;
  }

  // ---- CSS ----
  var style = document.createElement('style');
  style.id = '__repld_style';
  style.textContent = `
    #__repld_pill {
      position: fixed;
      bottom: 18px;
      left: 50%;
      transform: translateX(-50%);
      z-index: 2147483647;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 13px;
      pointer-events: auto;
    }
    #__repld_pill * { box-sizing: border-box; }
    #__repld_pill_bar {
      display: flex;
      align-items: center;
      gap: 7px;
      background: rgba(20,20,28,0.92);
      backdrop-filter: blur(12px);
      -webkit-backdrop-filter: blur(12px);
      border: 1px solid rgba(255,255,255,0.10);
      border-radius: 999px;
      padding: 5px 14px 5px 10px;
      cursor: pointer;
      user-select: none;
      box-shadow: 0 4px 24px rgba(0,0,0,0.45);
      transition: background 0.15s;
    }
    #__repld_pill_bar:hover { background: rgba(30,30,42,0.97); }
    #__repld_dot {
      width: 9px; height: 9px;
      border-radius: 50%;
      background: #22c55e;
      flex-shrink: 0;
      transition: background 0.2s;
    }
    #__repld_dot.amber {
      background: #f59e0b;
      animation: __repld_pulse 1s ease-in-out infinite;
    }
    @keyframes __repld_pulse {
      0%, 100% { opacity: 1; }
      50% { opacity: 0.4; }
    }
    #__repld_label {
      color: rgba(255,255,255,0.88);
      white-space: nowrap;
    }
    #__repld_panel {
      display: none;
      margin-top: 6px;
      background: rgba(20,20,28,0.96);
      backdrop-filter: blur(12px);
      -webkit-backdrop-filter: blur(12px);
      border: 1px solid rgba(255,255,255,0.10);
      border-radius: 14px;
      padding: 14px 16px;
      min-width: 280px;
      max-width: 380px;
      box-shadow: 0 8px 40px rgba(0,0,0,0.6);
      color: rgba(255,255,255,0.80);
    }
    #__repld_panel.open { display: block; }
    .__repld_row {
      display: flex;
      gap: 8px;
      margin-bottom: 6px;
      font-size: 12px;
    }
    .__repld_row_label {
      color: rgba(255,255,255,0.42);
      min-width: 60px;
      flex-shrink: 0;
    }
    .__repld_row_value { color: rgba(255,255,255,0.82); word-break: break-all; }
    #__repld_gate_area {
      margin-top: 10px;
      padding-top: 10px;
      border-top: 1px solid rgba(255,255,255,0.08);
    }
    #__repld_gate_prompt {
      font-size: 13px;
      color: rgba(255,255,255,0.90);
      margin-bottom: 10px;
      line-height: 1.4;
    }
    #__repld_gate_buttons {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }
    .__repld_btn {
      padding: 5px 14px;
      border-radius: 7px;
      border: 1px solid rgba(255,255,255,0.18);
      background: rgba(255,255,255,0.08);
      color: rgba(255,255,255,0.90);
      cursor: pointer;
      font-size: 12px;
      transition: background 0.12s;
    }
    .__repld_btn:hover { background: rgba(255,255,255,0.16); }
    .__repld_btn.primary {
      background: #3b82f6;
      border-color: #3b82f6;
      color: #fff;
    }
    .__repld_btn.primary:hover { background: #2563eb; }
    #__repld_pending_count {
      font-size: 11px;
      color: rgba(255,255,255,0.38);
      margin-top: 8px;
    }
  `;
  document.head.appendChild(style);

  // ---- DOM (createElement only — no innerHTML, Trusted Types safe) ----
  function _el(tag, attrs, children) {
    var e = document.createElement(tag);
    if (attrs) Object.keys(attrs).forEach(function(k) {
      if (k === 'text') e.textContent = attrs[k];
      else if (k === 'style') e.style.cssText = attrs[k];
      else e[k] = attrs[k];
    });
    if (children) children.forEach(function(c) { e.appendChild(c); });
    return e;
  }

  var pill = _el('div', {id: '__repld_pill'}, [
    _el('div', {id: '__repld_pill_bar'}, [
      _el('div', {id: '__repld_dot'}),
      _el('span', {id: '__repld_label', text: 'repld'})
    ]),
    _el('div', {id: '__repld_panel'}, [
      _el('div', {className: '__repld_row'}, [
        _el('span', {className: '__repld_row_label', text: 'status'}),
        _el('span', {className: '__repld_row_value', id: '__repld_status', text: 'connected'})
      ]),
      _el('div', {className: '__repld_row'}, [
        _el('span', {className: '__repld_row_label', text: 'host'}),
        _el('span', {className: '__repld_row_value', id: '__repld_host'})
      ]),
      _el('div', {className: '__repld_row', id: '__repld_reason_row', style: 'display:none'}, [
        _el('span', {className: '__repld_row_label', text: 'reason'}),
        _el('span', {className: '__repld_row_value', id: '__repld_reason'})
      ]),
      _el('div', {id: '__repld_gate_area', style: 'display:none'}, [
        _el('div', {id: '__repld_gate_prompt'}),
        _el('div', {id: '__repld_gate_buttons'}),
        _el('div', {id: '__repld_pending_count'})
      ])
    ])
  ]);
  document.body.appendChild(pill);

  // Set host
  document.getElementById('__repld_host').textContent = location.hostname;

  // Toggle panel on pill bar click
  var pillBar = document.getElementById('__repld_pill_bar');
  var panel = document.getElementById('__repld_panel');
  pillBar.addEventListener('click', function() {
    panel.classList.toggle('open');
  });

  // ---- Gate queue ----
  var _gate_queue = [];
  var _active_gate = null;

  function _render_gate() {
    var area = document.getElementById('__repld_gate_area');
    var promptEl = document.getElementById('__repld_gate_prompt');
    var buttonsEl = document.getElementById('__repld_gate_buttons');
    var pendingEl = document.getElementById('__repld_pending_count');
    var dot = document.getElementById('__repld_dot');
    var label = document.getElementById('__repld_label');
    var statusEl = document.getElementById('__repld_status');

    if (!_active_gate) {
      area.style.display = 'none';
      dot.className = '';
      label.textContent = 'repld';
      statusEl.textContent = 'connected';
      pendingEl.textContent = '';
      return;
    }

    area.style.display = 'block';
    panel.classList.add('open');
    dot.className = 'amber';
    label.textContent = 'repld';
    statusEl.textContent = 'awaiting input';
    promptEl.textContent = _active_gate.prompt;

    // Build buttons
    while (buttonsEl.firstChild) buttonsEl.removeChild(buttonsEl.firstChild);
    _active_gate.buttons.forEach(function(btn) {
      var el = document.createElement('button');
      el.className = '__repld_btn' + (btn.style === 'primary' ? ' primary' : '');
      el.textContent = btn.label;
      el.addEventListener('click', function() {
        var gid = _active_gate.gate_id;
        var val = btn.value;
        _active_gate = null;
        if (_gate_queue.length > 0) {
          _active_gate = _gate_queue.shift();
        }
        _render_gate();
        window.__repld_resolve(JSON.stringify({gate_id: gid, value: val}));
      });
      buttonsEl.appendChild(el);
    });

    var remaining = _gate_queue.length;
    pendingEl.textContent = remaining > 0 ? remaining + ' more pending' : '';
  }

  // ---- Public API ----
  window.__repld_pill = true;

  window.__repld_update = function(opts) {
    if (opts.reason !== undefined) {
      var reasonRow = document.getElementById('__repld_reason_row');
      var reasonEl = document.getElementById('__repld_reason');
      if (opts.reason) {
        reasonEl.textContent = opts.reason;
        reasonRow.style.display = 'flex';
      } else {
        reasonRow.style.display = 'none';
      }
    }
  };

  window.__repld_gate = function(gate_id, prompt, buttons) {
    var entry = {gate_id: gate_id, prompt: prompt, buttons: buttons};
    if (!_active_gate) {
      _active_gate = entry;
    } else {
      _gate_queue.push(entry);
    }
    _render_gate();
  };

  window.__repld_remove = function() {
    if (window.__repld_hb_timer) clearInterval(window.__repld_hb_timer);
    window.removeEventListener('beforeunload', window.__repld_beforeunload);
    var el = document.getElementById('__repld_pill');
    if (el) el.remove();
    var st = document.getElementById('__repld_style');
    if (st) st.remove();
    window.__repld_pill = false;
    window.__repld_hb = undefined;
    window.__repld_hb_timer = undefined;
    window.__repld_beforeunload = undefined;
    window.__repld_update = undefined;
    window.__repld_gate = undefined;
    window.__repld_remove = undefined;
  };

  // ---- Heartbeat (liveness) ----
  window.__repld_hb = Date.now();
  window.__repld_hb_timer = setInterval(function() {
    if (Date.now() - window.__repld_hb > 15000) {
      if (window.__repld_remove) window.__repld_remove();
    }
  }, 5000);

  // ---- beforeunload guard ----
  window.__repld_beforeunload = function(e) {
    e.preventDefault();
    e.returnValue = 'repld is using this tab. Leave anyway?';
    return e.returnValue;
  };
  window.addEventListener('beforeunload', window.__repld_beforeunload);
})();
"""


async def _handle_binding(session, params: dict) -> None:
    """Handle __repld_resolve callback from pill UI."""
    payload_str = params.get("payload", "{}")
    try:
        payload = json.loads(payload_str)
    except (json.JSONDecodeError, TypeError):
        return
    gate_id = payload.get("gate_id")
    value = payload.get("value")
    if gate_id:
        from ..gates import resolve_gate

        resolve_gate(gate_id, value)


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
        target_id: str,
        port: int = 9222,
        ready: str | None = None,
    ) -> None:
        self._session = session
        self._chrome_target_id = target_id
        self._port = port
        self._ready = ready
        self._pinned: bool = False
        self._pin_reason: str = ""
        self._pin_origin: str = ""
        self._heartbeat_task: asyncio.Task[None] | None = None

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
        self._session.capture_bodies = value

    # ------------------------------------------------------------------
    # Pin API
    # ------------------------------------------------------------------

    async def pin(self, reason: str = "") -> None:
        """Inject pill + beforeunload guard + heartbeat. Idempotent."""
        if not self._pinned:
            self._pin_origin = await self.js("location.origin")
            await self._setup_binding()
            await self.js(_PIN_JS)
            self._pinned = True
            self._pin_reason = reason
            self._heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(), name="repld-pill-heartbeat"
            )
        if reason:
            self._pin_reason = reason
            await self.js(f"__repld_update({{reason: {json.dumps(reason)}}})")

    async def unpin(self) -> None:
        """Remove pill + beforeunload + heartbeat."""
        if self._pinned:
            if self._heartbeat_task is not None:
                self._heartbeat_task.cancel()
                self._heartbeat_task = None
            await self.js("window.__repld_remove && window.__repld_remove()")
            self._pinned = False
            self._pin_reason = ""
            self._pin_origin = ""

    async def _heartbeat_loop(self) -> None:
        """Beat every 5s. Re-inject on same-origin reload; unpin on cross-origin."""
        origin = self._pin_origin
        check = (
            "window.__repld_pill"
            " ? (window.__repld_hb = Date.now(), 'ok')"
            " : location.origin === %s ? 'reload' : 'gone'"
        ) % json.dumps(origin)
        misses = 0
        while True:
            await asyncio.sleep(5)
            try:
                status = await self.js(check)
                misses = 0
            except Exception:
                misses += 1
                if misses >= 3:
                    break
                continue
            if status == "ok":
                continue
            if status == "reload":
                try:
                    await self._setup_binding()
                    await self.js(_PIN_JS)
                    if self._pin_reason:
                        await self.js(
                            f"__repld_update({{reason: {json.dumps(self._pin_reason)}}})"
                        )
                except Exception:
                    misses += 1
                    if misses >= 3:
                        break
                continue
            # Cross-origin — pin contract broken.
            from ..kernel import push_channel

            push_channel(
                f"pinned tab navigated away from {origin}",
                {"kind": "pin_lost", "target": self.target_id},
            )
            break
        self._pinned = False
        self._heartbeat_task = None
        self._pin_reason = ""
        self._pin_origin = ""

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
        """Gate routed to terminal (no pill UI for text input)."""
        from ..gates import ask as _ask

        return await _ask(prompt, **kw)

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
        Waits for the ready signal (CSS selector or JS expression) if set,
        otherwise waits for document.readyState === "complete".
        """
        send_fn = self._session._send
        browser_session = getattr(send_fn, "__self__", None)
        if browser_session is None:
            raise RuntimeError("Cannot re-attach: no BrowserSession reference")

        old_sid = self._session._session_id
        browser_session._sessions.pop(old_sid, None)

        cdp = await browser_session.attach(self._chrome_target_id)
        if cdp is None:
            raise RuntimeError(f"Re-attach failed for {self._chrome_target_id}")
        self._session = cdp

        ready = self._ready or "document.readyState === 'complete'"
        if ready.startswith((".", "#", "[", "data-")):
            await self._wait_ready_selector(ready)
        else:
            await self._wait_ready_js(ready)
        await asyncio.sleep(0.3)

    async def _wait_ready_selector(self, selector: str, timeout: float = 10) -> None:
        doc = await self._session.execute("DOM.getDocument")
        root_id = doc["root"]["nodeId"]
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            result = await self._session.execute(
                "DOM.querySelector", {"nodeId": root_id, "selector": selector}
            )
            if result.get("nodeId", 0) != 0:
                return
            await asyncio.sleep(0.1)
        raise RuntimeError(f"Ready signal not found after re-attach: {selector}")

    async def _wait_ready_js(self, expr: str, timeout: float = 10) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            result = await self._session.execute(
                "Runtime.evaluate",
                {"expression": expr, "returnByValue": True},
            )
            if result.get("result", {}).get("value"):
                return
            await asyncio.sleep(0.1)
        raise RuntimeError(f"Ready signal not satisfied after re-attach: {expr}")

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
        await_promise: str | bool = "auto",
        user_gesture: bool = True,
    ) -> Any:
        """Evaluate JavaScript expression in the page context.

        Args:
            expr: JavaScript expression to evaluate.
            await_promise: Whether to await Promises. "auto" retries if result is a Promise.
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
                "returnByValue": True,
                "userGesture": user_gesture,
                "awaitPromise": await_promise is True,
            },
        )

        # Check for exception
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            exc_obj = details.get("exception", {})
            text = exc_obj.get("description") or details.get("text", "JavaScript error")
            stack = exc_obj.get("description", "")
            url = details.get("url", "")
            line = details.get("lineNumber", 0)
            raise BrowserJSError(text, stack, url, line)

        rv = result.get("result", {})

        # Auto-await: if result is a Promise, re-evaluate with awaitPromise=True
        if await_promise == "auto" and rv.get("subtype") == "promise":
            return await self.js(expr, await_promise=True, user_gesture=user_gesture)

        return rv.get("value")

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
            for event_type in ("keyDown", "keyUp"):
                await self._exec(
                    "Input.dispatchKeyEvent",
                    {"type": event_type, "key": "Enter", "code": "Enter"},
                )

    async def _touch(
        self, type: str, touch_points: list[dict], timeout: float = 3
    ) -> None:
        """Dispatch a touch event with a timeout. Raises TimeoutError if the page's
        touch handler blocks (e.g. preventDefault on complex apps like Messenger)."""
        await self._session.execute(
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
        Body is auto-parsed as JSON when content-type is json.
        """
        body_js = "undefined"
        if body is not None:
            if isinstance(body, dict):
                body_js = json.dumps(json.dumps(body))  # JSON-encode the string
            else:
                body_js = json.dumps(str(body))

        h: dict[str, str] = {}
        if body is not None and isinstance(body, dict):
            h["Content-Type"] = "application/json"
        if headers:
            h.update(headers)  # caller's headers win (including Content-Type override)
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
        ready = self._ready
        if ready is None:
            return
        if ready.startswith((".", "#", "[", "data-")):
            await self._wait_ready_selector(ready, timeout)
        else:
            await self._wait_ready_js(ready, timeout)
        await self.wait_for_idle(timeout=2.0, quiet=0.3)

    async def reload(self) -> None:
        """Reload the page, then wait for the ready signal."""
        await self._session.execute("Page.reload")
        await self._wait_ready()

    async def navigate(self, url: str) -> None:
        """Navigate to URL, then wait for the ready signal."""
        await self._session.execute("Page.navigate", {"url": url})
        await self._wait_ready()

    async def screenshot(
        self, *, full_page: bool = False, path: str | None = None
    ) -> pathlib.Path:
        """Capture a PNG screenshot, save to disk, return the path.

        path: explicit save location. Default: $XDG_RUNTIME_DIR/repld/screenshot-{target}-{ts}.png
        """
        import time

        from ..tasks import SPILL_DIR, _ensure_spill_dir

        params: dict = {"format": "png"}
        if full_page:
            params["captureBeyondViewport"] = True
        result = await self._session.execute("Page.captureScreenshot", params)
        png_bytes = base64.b64decode(result.get("data", ""))
        if path:
            out = pathlib.Path(path)
        else:
            _ensure_spill_dir()
            tid = self.target_id.replace(":", "-")
            out = SPILL_DIR / f"screenshot-{tid}-{int(time.time())}.png"
        out.write_bytes(png_bytes)
        return out

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
            like_pattern = url.replace("*", "%")
            if not like_pattern.startswith("%"):
                like_pattern = "%" + like_pattern
            if not like_pattern.endswith("%"):
                like_pattern = like_pattern + "%"
            conditions.append("url LIKE ?")
            bind_params.append(like_pattern)
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

    def clear(self) -> None:
        """Clear all captured events (network + console) for this tab."""
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
        result = await self._session.execute("Network.getCookies")
        return result.get("cookies", [])

    async def cdp(self, method: str, **params: Any) -> dict:
        """Raw CDP passthrough."""
        return await self._session.execute(method, params if params else None)

    def __repr__(self) -> str:
        return f"<Tab {self.target_id} {self.url!r}>"
