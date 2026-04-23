"""Tab facade + Row dataclass.

Tab wraps CDPSession with a user-friendly API for JS eval, DOM interaction,
network queries, and console queries. Row is the dataclass returned by
network() and console().
"""

import asyncio
import base64
import json
from dataclasses import dataclass
from typing import Any

from .cdp import CDPSession

__all__ = ["Tab", "Row", "Rows", "BrowserJSError"]


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


@dataclass
class Row:
    """A row from a HAR or console query."""

    # HAR fields (network rows)
    id: int = 0
    request_id: str = ""
    redirect_index: int = 0
    protocol: str = ""
    method: str = ""
    status: int = 0
    url: str = ""
    type: str = ""
    size: int = 0
    time_ms: int | None = None
    state: str = ""
    pause_stage: str | None = None
    paused_id: int | None = None
    frames_sent: int | None = None
    frames_received: int | None = None
    started_datetime: str | None = None
    last_activity: float | None = None
    target: str = ""
    body_status: str | None = None
    mime_family: str = ""
    is_asset: bool = False
    initiator_type: str | None = None
    initiator_url: str | None = None

    # Console fields
    level: str = ""
    source: str = ""
    text: str = ""
    stack_url: str | None = None
    stack_line: str | None = None
    stack_function: str | None = None
    timestamp: str | None = None

    # Back-reference for .body()
    _session: "CDPSession | None" = None

    def body(self) -> dict:
        """Fetch the response body for this request."""
        if self._session is None:
            return {"error": "no session"}
        return self._session.fetch_body(self.request_id)

    def __repr__(self) -> str:
        if self.method and self.url:
            size_str = f"{self.size / 1024:.1f}KB" if self.size else "0B"
            time_str = f"{self.time_ms}ms" if self.time_ms is not None else "?"
            rid = f" rid={self.request_id}" if self.request_id else ""
            return f"<Request {self.method} {self.url} -> {self.status} ({time_str}, {size_str}){rid}>"
        if self.level:
            return f"<Console {self.level}: {self.text[:60]}>"
        return f"<Row id={self.id}>"


class Rows(list):
    """List subclass with one-entry-per-line repr for grep-friendly spill files."""

    def __repr__(self) -> str:
        if not self:
            return "[]"
        return "\n".join(repr(r) for r in self)


def _row_from_har(cols: tuple, session: CDPSession) -> Row:
    """Build a Row from a har_summary query result tuple."""
    # har_summary columns: id, request_id, redirect_index, protocol, method, status,
    #   url, type, size, time_ms, state, pause_stage, paused_id, frames_sent,
    #   frames_received, started_datetime, last_activity, target, body_status,
    #   mime_family, is_asset, initiator_type, initiator_url
    return Row(
        id=cols[0] or 0,
        request_id=cols[1] or "",
        redirect_index=cols[2] or 0,
        protocol=cols[3] or "",
        method=cols[4] or "",
        status=cols[5] or 0,
        url=cols[6] or "",
        type=cols[7] or "",
        size=cols[8] or 0,
        time_ms=cols[9],
        state=cols[10] or "",
        pause_stage=cols[11],
        paused_id=cols[12],
        frames_sent=cols[13],
        frames_received=cols[14],
        started_datetime=cols[15],
        last_activity=cols[16],
        target=cols[17] or "",
        body_status=cols[18],
        mime_family=cols[19] or "",
        is_asset=bool(cols[20]),
        initiator_type=cols[21],
        initiator_url=cols[22],
        _session=session,
    )


def _row_from_console(cols: tuple, session: CDPSession) -> Row:
    """Build a Row from a console_entries query result tuple."""
    # console_entries columns: id, level, source, text, stack_url, stack_line,
    #   stack_function, timestamp, target
    return Row(
        id=cols[0] or 0,
        level=cols[1] or "",
        source=cols[2] or "",
        text=cols[3] or "",
        stack_url=cols[4],
        stack_line=cols[5],
        stack_function=cols[6],
        timestamp=cols[7],
        target=cols[8] or "",
        _session=session,
    )


def _parse_json(val: Any) -> Any:
    """Parse a JSON string into a dict/list, or return None."""
    if val is None:
        return None
    if isinstance(val, (dict, list)):
        return val
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError):
        return None


def _dict_from_har_entry(cols: tuple) -> dict:
    """Build a structured dict from a har_entries query result tuple.

    har_entries columns (0-indexed):
      0  id, 1  request_id, 2  redirect_index, 3  protocol, 4  method,
      5  url, 6  status, 7  status_text, 8  type, 9  size, 10 time_ms,
      11 state, 12 pause_stage, 13 paused_id, 14 request_headers,
      15 post_data, 16 response_headers, 17 mime_type, 18 timing,
      19 error_text, 20 request_cookies, 21 frames_sent, 22 frames_received,
      23 ws_total_bytes, 24 started_datetime, 25 last_activity, 26 target,
      27 body_status, 28 initiator_type, 29 initiator_url,
      30 initiator_function, 31 initiator_line, 32 loader_id, 33 frame_id,
      34 auth_scheme, 35 auth_cookies, 36 csrf_token_header, 37 mime_family,
      38 is_asset, 39 curl_command
    """
    d: dict[str, Any] = {
        "request": {
            "method": cols[4] or "",
            "url": cols[5] or "",
        },
        "response": {
            "status": cols[6] or 0,
        },
        "state": cols[11] or "",
        "type": cols[8] or "",
        "size": cols[9] or 0,
        "time_ms": cols[10],
    }

    # Request details
    req_headers = _parse_json(cols[14])
    if req_headers:
        d["request"]["headers"] = req_headers
    if cols[15]:
        d["request"]["postData"] = cols[15]

    # Response details
    if cols[7]:
        d["response"]["statusText"] = cols[7]
    resp_headers = _parse_json(cols[16])
    if resp_headers:
        d["response"]["headers"] = resp_headers
    if cols[17]:
        d["response"]["mimeType"] = cols[17]

    # Timing
    timing = _parse_json(cols[18])
    if timing:
        d["timing"] = timing

    # Error
    if cols[19]:
        d["error_text"] = cols[19]

    # Auth
    if cols[34]:
        d["auth_scheme"] = cols[34]
    if cols[36]:
        d["csrf_token_header"] = cols[36]

    # Initiator
    init_type = cols[28]
    if init_type:
        initiator: dict[str, Any] = {"type": init_type}
        if cols[29]:
            initiator["url"] = cols[29]
        if cols[30]:
            initiator["function"] = cols[30]
        if cols[31]:
            initiator["line"] = cols[31]
        d["initiator"] = initiator

    return d


# Shared role → CSS selector mapping (used by _resolve_selector for both
# role= and :has-text() patterns).
_ROLE_CSS: dict[str, str] = {
    "button": 'button, [role="button"], input[type="button"], input[type="submit"]',
    "link": 'a[href], [role="link"]',
    "textbox": 'input:not([type]), input[type="text"], input[type="email"], input[type="search"], input[type="url"], input[type="password"], textarea, [role="textbox"]',
    "checkbox": 'input[type="checkbox"], [role="checkbox"]',
    "radio": 'input[type="radio"], [role="radio"]',
    "heading": 'h1, h2, h3, h4, h5, h6, [role="heading"]',
    "listitem": 'li, [role="listitem"]',
    "tab": '[role="tab"]',
    "tabpanel": '[role="tabpanel"]',
    "option": 'option, [role="option"]',
    "combobox": 'select, [role="combobox"]',
}


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
    ) -> None:
        self._session = session
        self._chrome_target_id = target_id
        self._port = port

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
    def capture_bodies(self) -> bool:
        return self._session.capture_bodies

    @capture_bodies.setter
    def capture_bodies(self, value: bool) -> None:
        self._session.capture_bodies = value

    # ------------------------------------------------------------------
    # JS interaction
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
        result = await self._session.execute(
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

    @staticmethod
    def _resolve_selector(selector: str) -> str:
        """Convert Playwright-style selectors to a JS expression returning an element.

        Supported patterns:
          text=Submit                       → text content match
          button:has-text('OK')            → CSS base + text filter
          role=button[name="Save"]         → ARIA role + accessible name
          label=Username                   → input by associated label
          .css-selector                    → document.querySelector(...)
        """
        import json as _json
        import re

        # text=... → exact text content or aria-label match (prefer smallest element)
        if selector.startswith("text="):
            text = selector[5:]
            return (
                f"(function() {{"
                f" const text = {_json.dumps(text)};"
                f" const all = Array.from(document.querySelectorAll('*'));"
                f" const exact = all.filter(el => el.offsetWidth > 0 && ("
                f"   el.textContent.trim() === text || el.getAttribute('aria-label') === text));"
                f" return exact.sort((a,b) => a.textContent.length - b.textContent.length)[0] || null;"
                f"}})()"
            )

        # role=button[name="Save"] → ARIA role + accessible name
        # Supports = (exact), *= (contains), ^= (starts-with)
        m = re.match(r'^role=(\w+)(?:\[name([*^]?=)["\']?(.+?)["\']?\])?$', selector)
        if m:
            role, op, name = m.group(1), m.group(2), m.group(3)
            css = _ROLE_CSS.get(role, f'[role="{role}"]')
            if name:
                n = _json.dumps(name)
                if op == "*=":
                    cmp = (
                        f"el.textContent.trim().includes({n})"
                        f" || (el.getAttribute('aria-label') || '').includes({n})"
                        f" || (el.getAttribute('title') || '').includes({n})"
                    )
                elif op == "^=":
                    cmp = (
                        f"el.textContent.trim().startsWith({n})"
                        f" || (el.getAttribute('aria-label') || '').startsWith({n})"
                        f" || (el.getAttribute('title') || '').startsWith({n})"
                    )
                else:
                    cmp = (
                        f"el.textContent.trim() === {n}"
                        f" || el.getAttribute('aria-label') === {n}"
                        f" || el.getAttribute('title') === {n}"
                        f" || el.value === {n}"
                        f" || (el.labels && Array.from(el.labels).some(l => l.textContent.trim() === {n}))"
                    )
                return (
                    f"Array.from(document.querySelectorAll({_json.dumps(css)}))"
                    f".find(el => {cmp})"
                )
            return f"document.querySelector({_json.dumps(css)})"

        # label=Username → input by associated label text
        if selector.startswith("label="):
            label_text = selector[6:]
            return (
                f"(function() {{"
                f" const lbl = Array.from(document.querySelectorAll('label'))"
                f"   .find(l => l.textContent.trim() === {_json.dumps(label_text)});"
                f" if (!lbl) return null;"
                f" if (lbl.htmlFor) return document.getElementById(lbl.htmlFor);"
                f" return lbl.querySelector('input, textarea, select');"
                f"}})()"
            )

        # :has-text('...') → split into CSS base + JS text filter
        # Expands known role names (button → includes [role="button"])
        m = re.match(r"^(.+?):has-text\(['\"](.+?)['\"]\)$", selector)
        if m:
            css_base, text = m.group(1), m.group(2)
            css_expanded = _ROLE_CSS.get(css_base, css_base)
            return (
                f"Array.from(document.querySelectorAll({_json.dumps(css_expanded)}))"
                f".find(el => el.textContent.trim().includes({_json.dumps(text)})"
                f" || (el.getAttribute('aria-label') || '').includes({_json.dumps(text)}))"
            )

        # Plain CSS selector
        return f"document.querySelector({_json.dumps(selector)})"

    async def _find_element(self, selector: str, timeout: float = 2.0) -> str:
        """Resolve selector to element with auto-wait. Returns the JS find expression.

        Retries for up to `timeout` seconds before raising RuntimeError.
        """
        find_expr = self._resolve_selector(selector)
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            result = await self._session.execute(
                "Runtime.evaluate",
                {
                    "expression": f"!!({find_expr})",
                    "returnByValue": True,
                },
            )
            if result.get("result", {}).get("value"):
                return find_expr
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError(f"Element not found: {selector}")
            await asyncio.sleep(0.1)

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
        find_expr = await self._find_element(selector)
        coords = await self._session.execute(
            "Runtime.evaluate",
            {
                "expression": f"""
(function() {{
    const el = {find_expr};
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
        x, y = pos["x"], pos["y"]

        for event_type in ("mousePressed", "mouseReleased"):
            await self._session.execute(
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
        find_expr = await self._find_element(selector)

        # Focus + select all existing content so first keystroke replaces it
        await self._session.execute(
            "Runtime.evaluate",
            {
                "expression": (
                    f"(function() {{ const el = {find_expr};"
                    f" if (el) {{ el.focus(); if (el.select) el.select(); }} }})()"
                ),
                "returnByValue": True,
            },
        )

        # Type new text via key events
        for char in text:
            for event_type in ("keyDown", "keyUp"):
                await self._session.execute(
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
                await self._session.execute(
                    "Input.dispatchKeyEvent",
                    {"type": event_type, "key": "Enter", "code": "Enter"},
                )

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
        import json as _json

        body_js = "undefined"
        if body is not None:
            if isinstance(body, dict):
                body_js = _json.dumps(_json.dumps(body))  # JSON-encode the string
            else:
                body_js = _json.dumps(str(body))

        h: dict[str, str] = {}
        if body is not None and isinstance(body, dict):
            h["Content-Type"] = "application/json"
        if headers:
            h.update(headers)  # caller's headers win (including Content-Type override)
        headers_js = _json.dumps(h) if h else "undefined"

        code = f"""
(async function() {{
  const opts = {{
    method: {_json.dumps(method)},
    body: {body_js},
    headers: {headers_js},
  }};
  const r = await fetch({_json.dumps(url)}, opts);
  const ct = r.headers.get('content-type') || '';
  let body;
  if (ct.includes('json')) {{
    try {{ body = await r.json(); }} catch(e) {{ body = await r.text(); }}
  }} else {{
    body = await r.text();
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
        await self._find_element(selector, timeout=timeout)

    async def reload(self) -> None:
        """Reload the page via Page.reload CDP command."""
        await self._session.execute("Page.reload")

    async def navigate(self, url: str) -> None:
        """Page.navigate CDP command. Caller handles settle separately."""
        await self._session.execute("Page.navigate", {"url": url})

    async def screenshot(self, *, full_page: bool = False) -> bytes:
        """Capture a screenshot; returns PNG bytes."""
        params: dict = {"format": "png"}
        if full_page:
            params["captureBeyondViewport"] = True
        result = await self._session.execute("Page.captureScreenshot", params)
        data = result.get("data", "")
        return base64.b64decode(data)

    # ------------------------------------------------------------------
    # Query methods (sync DuckDB)
    # ------------------------------------------------------------------

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

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        sql = f"SELECT * FROM har_summary {where} ORDER BY id DESC LIMIT 500"

        rows = self._session.query(sql, bind_params if bind_params else None)
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

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        sql = f"SELECT * FROM console_entries {where} LIMIT 200"

        rows = self._session.query(sql, bind_params if bind_params else None)
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
