"""Tab facade — user-friendly API over CDPSession.

Wraps CDPSession with JS eval, DOM interaction, and CDP passthrough.
Row/Rows types and the DuckDB-backed query methods (network/console/sse/
lifecycle/body/request) live in tab_query.py; selector translation lives in
selector.py and resolution in inject.py (the vendored engine).
"""

import asyncio
import base64
import json
import pathlib
from dataclasses import dataclass
from typing import Any

from .. import bg
from ..channel import push_kind
from .cdp import CDPSession
from .pin import (
    BINDING_NAME,
    _handle_binding,
    _next_label_color,
    _PIN_JS,
    label_script,
)
from .png import _model_dims, _resize_png
from . import inject
from . import selector as selector_mod
from .tab_query import TabQueryMixin
from .target import make_target

__all__ = ["Tab", "BrowserJSError", "Receipt"]

# Pin/pill heartbeat cadence. The JS-side pill self-removes if it hasn't
# heard from Python in _HEARTBEAT_STALE_MS — kept in lockstep with the
# Python-side give-up point (interval * max misses) via the same constants,
# substituted into _PIN_JS at injection time (see tab.py:_inject_pin).
_HEARTBEAT_INTERVAL_S = 5
_HEARTBEAT_MAX_MISSES = 3
_HEARTBEAT_STALE_MS = _HEARTBEAT_INTERVAL_S * _HEARTBEAT_MAX_MISSES * 1000

# Tab.key()'s named-key handling. Every other supported name (Enter, Escape,
# Tab, ArrowDown, ...) already equals its own KeyboardEvent.code and needs no
# `text` — Space is the one name whose real .key (" ") and .code ("Space")
# diverge from each other and from the friendly name callers actually type.
_KEY_ALIASES = {"Space": " "}
_KEY_CODE = {" ": "Space"}
_KEY_TEXT = {"Enter": "\r", " ": " "}

# Grace period at the end of `_reattach`, after the ready signal is satisfied.
# The re-attach re-enables this session's CDP domains through
# `CDPSession._enable_domains`, which sends every `*.enable` with `send_nowait`
# — deliberately, to keep attach fast, but it means they are in flight and
# unacknowledged when the ready signal fires. Returning at that moment hands
# back a Tab whose Network/Runtime/Log events Chrome may not be emitting yet,
# so the first thing the caller does after a reattach is the thing least likely
# to be observed. Empirical, and the cheapest place to absorb it.
_POST_REATTACH_SETTLE_S = 0.3


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


@dataclass(frozen=True, slots=True)
class Receipt:
    """What an input action actually did — returned by click/tap/type_text/
    select_option so a misdirected action is visible in the same call.

    `line` is the rendered one-liner (also what str()/repr() show, so a cell
    or observation prints it directly); `warning` is True when the action
    landed somewhere unrelated to the resolved target.
    """

    line: str
    warning: bool = False

    def __str__(self) -> str:
        return self.line

    def __repr__(self) -> str:
        return self.line


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


class Tab(TabQueryMixin):
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
            bg.spawn(
                self._session.enable_fetch(),
                name=f"repld-fetch-enable-{self._chrome_target_id[:8]}",
                loop=loop,
            )
        else:
            bg.spawn(
                self._session.disable_fetch(),
                name=f"repld-fetch-disable-{self._chrome_target_id[:8]}",
                loop=loop,
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
        bg.spawn(self._set_label(value), name="repld-label-set", loop=loop)

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

        result = await session.execute(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": label_script(text, color), "runImmediately": True},
        )
        session._label_script_id = result.get("identifier")
        session._label_text = text
        session._label_color = color

    # ------------------------------------------------------------------
    # Pin API
    # ------------------------------------------------------------------

    async def pin(self, reason: str = "", guard_unload: bool = True) -> None:
        """Inject pill + heartbeat, and (unless `guard_unload=False`) a
        beforeunload guard. Idempotent.

        `guard_unload` defaults True — pin()'s original, always-on
        behavior — for every existing caller protecting a long-lived
        interactive session from accidental tab close. Set it False for a
        tab you're actively driving against a live-reloading dev server:
        the guard's `beforeunload` handler fires on ANY unload, same-origin
        included, so it blocks a framework's own HMR full-page reload
        (Vite, etc.) behind a native "Leave site?" confirm dialog that a
        CDP driver can't dismiss on its own — indistinguishable from a
        hung tab from the caller's side (found live: a pinned tab wedged
        mid dev-loop, `Page.navigate`/`Runtime.evaluate` both timing out,
        while the exact same page was instant unpinned).
        Only settable on the first `pin()` call — a reason-only re-pin
        (already pinned, no state to change) doesn't revisit it.
        """
        session = self._session
        if not session._pinned:
            session._pinned = True  # claim before the awaits below yield
            try:
                session._pin_origin = await self.js("location.origin")
                session._pin_reason = reason
                session._pin_guard_unload = guard_unload
                await self._inject_pin()
                session._heartbeat_task = asyncio.create_task(
                    self._heartbeat_loop(), name="repld-pill-heartbeat"
                )
            except Exception:
                session._pinned = False
                raise
        elif reason:
            session._pin_reason = reason
            await self.js(f"__repld_update({{reason: {json.dumps(reason)}}})")

    async def _inject_pin(self) -> None:
        """Set up the binding + inject the pill JS + re-apply the reason label."""
        session = self._session
        await self._setup_binding()
        js = (
            _PIN_JS.replace("%STALE_MS%", str(_HEARTBEAT_STALE_MS))
            .replace("%CHECK_MS%", str(_HEARTBEAT_INTERVAL_S * 1000))
            .replace("%GUARD_UNLOAD%", "true" if session._pin_guard_unload else "false")
        )
        await self.js(js)
        if session._pin_reason:
            await self.js(
                f"__repld_update({{reason: {json.dumps(session._pin_reason)}}})"
            )

    async def unpin(self) -> None:
        """Remove pill + beforeunload + heartbeat.

        The state clears in a `finally` for the same reason `pin()` rolls back
        in an `except`: the DOM half can fail on its own. Removing the pill is
        a `Runtime.evaluate` on a tab that may already be gone, in which case
        `_exec` raises "target ... no longer exists" — and by then the
        heartbeat, the one other thing that would eventually have cleared
        `_pinned`, has already been cancelled. Leaving the flag set stranded
        the session pinned forever: `pin()` afterwards sees `_pinned` and takes
        the reason-only branch, so it never re-injects and never restarts the
        heartbeat, and nothing else ever writes False.
        """
        session = self._session
        if not session._pinned:
            return
        if session._heartbeat_task is not None:
            session._heartbeat_task.cancel()
            session._heartbeat_task = None
        try:
            await self.js("window.__repld_remove && window.__repld_remove()")
        finally:
            session._pinned = False
            session._pin_reason = ""
            session._pin_origin = ""
            session._pin_guard_unload = True

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
            push_kind(
                f"pinned tab navigated away from {origin}",
                "pin_lost",
                target=self.target_id,
            )
            break
        session._pinned = False
        session._heartbeat_task = None
        session._pin_reason = ""
        session._pin_origin = ""
        session._pin_guard_unload = True

    async def _setup_binding(self) -> None:
        """Register the gate-callback CDP binding on this session.

        Setting `_binding_handler` is also what tells `_reattach_core` this
        session owes a re-registration after a reconnect — see there.
        """
        await self._exec("Runtime.addBinding", {"name": BINDING_NAME})
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
        input, so this one never reaches the tab. It is answered wherever an
        ordinary `ask()` is: the kernel's pane, or `repld gate answer` when
        there isn't one."""
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
        label state); the pin pill re-injects via the heartbeat loop, and the
        session-scoped registrations that died with the old session — the gate
        binding and the label's addScriptToEvaluateOnNewDocument — are restored
        by `_reattach_core`, so both paths into a reattach get them rather than
        only this one.  Waits for the ready signal (selector or JS expression)
        if set, otherwise waits for document.readyState === "complete".
        """
        browser_session = self._session.browser_session
        if browser_session is None:
            raise RuntimeError("Cannot re-attach: no BrowserSession reference")

        await browser_session.reattach_session(self._session)

        await self._await_ready_signal(
            self._ready or "document.readyState === 'complete'"
        )
        await asyncio.sleep(_POST_REATTACH_SETTLE_S)

    async def _await_ready_signal(self, ready: str, timeout: float = 10) -> None:
        """Wait for a ready signal — selector or JS expression, by shape.

        Classification comes from `selector.looks_like_selector`, not from a
        second opinion held here. This used to test
        `startswith((".", "#", "[", "data-"))`, which disagreed with the
        selector module — the owner of the question — about every bare tag and
        custom element. `ready="main"` and `ready="my-app"` took the JS branch,
        evaluated as bare identifiers, came back as a ReferenceError whose
        `result.value` is simply absent, and polled silently for the full
        timeout while `ready="#app"` worked. The selector branch polls
        `inject.selector_exists` — existence only, never strictness: a ready
        signal that matched three nodes has still fired.
        """
        is_selector = selector_mod.looks_like_selector(ready)
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if is_selector:
                if await inject.selector_exists(self, ready):
                    return
            else:
                result = await self._session.execute(
                    "Runtime.evaluate",
                    {"expression": ready, "returnByValue": True},
                )
                # A raising expression is reported, not polled. Nothing
                # inspected `exceptionDetails` before, so a typo'd signal was
                # indistinguishable from one that hadn't fired yet: the same
                # full-timeout wait, then a message saying the page never
                # became ready. A ReferenceError on the first evaluation will
                # still be one on the hundredth.
                if "exceptionDetails" in result:
                    desc = result["exceptionDetails"].get("text", "evaluation failed")
                    raise RuntimeError(f"Ready signal {ready!r} raised: {desc}")
                if result.get("result", {}).get("value"):
                    return
            await asyncio.sleep(0.1)
        verb = "not found" if is_selector else "not satisfied"
        raise RuntimeError(f"Ready signal {verb} after re-attach: {ready}")

    async def _exec(
        self, method: str, params: dict | None = None, timeout: float = 30
    ) -> dict:
        """Execute a CDP command with session-gone recovery.

        On HMR reload or navigation, the Chrome session ID changes but the
        target ID stays the same. Detects "session not found", re-attaches,
        waits for the ready signal, and retries once.

        If the re-attach itself fails, the *target* was destroyed outright,
        not just its session — e.g. a cross-origin navigation Chrome handled
        with a process swap, or the tab/window was closed. _reattach() still
        addresses the old target ID (per its own docstring's "usually
        survives" caveat), so that failure is Target.attachToTarget's raw
        CDP error, e.g. "No target with given id found" — not obviously
        actionable on its own. Re-raised here as a clear, distinct error
        pointing at browser_tabs, since this Tab handle can't recover itself.
        """
        try:
            return await self._session.execute(method, params, timeout)
        except RuntimeError as exc:
            if not self._is_session_gone(exc):
                raise
            try:
                await self._reattach()
            except Exception as reattach_exc:
                raise RuntimeError(
                    f"target {self.target_id} no longer exists (destroyed, not "
                    "just its session) -- call browser_tabs to find its "
                    "replacement, if any"
                ) from reattach_exc
            return await self._session.execute(method, params, timeout)

    async def _ensure_front(self) -> None:
        """Raise this tab if Chrome has it backgrounded, before dispatching input.

        `click`/`tap`/`type_text`/`key` all ride `Input.dispatch*Event` —
        the renderer's real input pipeline, which Chrome silently drops for
        a tab whose window is occluded or backgrounded
        (`document.visibilityState === "hidden"`): the CDP command still
        acks, no exception is raised, and no handler on the page ever
        fires. Resolving a selector doesn't hit this — `DOM.getContentQuads`
        and `Runtime.evaluate` both work fine on a hidden tab, which is why
        the failure looks like nothing happened rather than an error. Costs
        one `Runtime.evaluate` round trip on every call and `Page.bringToFront`
        only on the degraded path — the multi-window watch workflow this
        kernel exists for means N-1 tabs are hidden at any given time, so
        this can't be opt-in.
        """
        result = await self._exec(
            "Runtime.evaluate",
            {"expression": "document.visibilityState", "returnByValue": True},
        )
        if result.get("result", {}).get("value") == "hidden":
            await self._exec("Page.bringToFront")

    async def front(self) -> None:
        """Bring this tab's window to the front (Page.bringToFront).

        click()/tap()/type_text()/key() already do this automatically when
        the tab is backgrounded — call this directly to raise a tab for its
        own sake (e.g. before a screenshot a human is watching for), or to
        avoid paying the visibility check on every call in a script that
        knows it's about to do a lot of input against one tab.
        """
        await self._exec("Page.bringToFront")

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

    @staticmethod
    def _quad_center(quads: list) -> tuple[float, float]:
        """Center point from DOM.getContentQuads result."""
        quad = quads[0]
        xs = [quad[i] for i in range(0, 8, 2)]
        ys = [quad[i] for i in range(1, 8, 2)]
        return sum(xs) / 4, sum(ys) / 4

    async def _element_center_of(
        self, el: inject.ResolvedElement, selector: str
    ) -> tuple[float, float]:
        """Center point of a resolved element handle.

        DOM.getContentQuads returns no quads for a zero-area element; the
        bounding-rect fallback keeps the single-hidden-match case (an
        off-screen real control behind a styled proxy) addressable, matching
        the resolve policy that a lone invisible match is still the target.
        """
        quads = await self._exec("DOM.getContentQuads", {"objectId": el.object_id})
        qs = quads.get("quads") or []
        if qs:
            return self._quad_center(qs)
        result = await inject.call_engine(
            self,
            "function(el) { const r = el.getBoundingClientRect();"
            " return { x: r.left + r.width / 2, y: r.top + r.height / 2 }; }",
            [{"objectId": el.object_id}],
        )
        pos = result.get("result", {}).get("value")
        if pos is None:
            raise RuntimeError(f"Element not found: {selector}")
        return pos["x"], pos["y"]

    async def _receipt_for(
        self, el: inject.ResolvedElement, x: float, y: float, *, verb: str
    ) -> Receipt:
        data = await inject.hit_receipt(self, el, x, y)
        t = data.get("target") or {}
        h = data.get("hit")
        tdesc = " — ".join(p for p in (t.get("preview"), t.get("sel")) if p)
        if data.get("related", True) or h is None:
            line = f"{verb}: {tdesc} ({x:.0f},{y:.0f})"
            if el.note:
                line += f" [{el.note}]"
            return Receipt(line=line)
        hdesc = " — ".join(p for p in (h.get("preview"), h.get("sel")) if p)
        return Receipt(
            line=f"warning: {verb} at ({x:.0f},{y:.0f}) hits {hdesc}, not {tdesc}",
            warning=True,
        )

    async def _click_element(
        self,
        el: inject.ResolvedElement,
        *,
        selector: str,
        button: str = "left",
        click_count: int = 1,
    ) -> Receipt:
        """Coordinate resolution + hit receipt + mouse dispatch for a resolved
        element. The receipt is taken *before* dispatch — the click's own
        handlers may re-render the DOM, so "what will this land on" is the
        honest answer; the dispatch still happens either way (observe and
        report, never refuse)."""
        x, y = await self._element_center_of(el, selector)
        receipt = await self._receipt_for(el, x, y, verb="clicked")
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
        return receipt

    async def click(
        self,
        selector: str,
        *,
        button: str = "left",
        click_count: int = 1,
    ) -> Receipt:
        """Click an element. Auto-waits up to 2s, strictly resolved.

        Selector: CSS, text=Label, role=button[name='OK'], label=Name,
        tag:has-text('...'), or aria-ref=e12 from browser_tree. A selector
        matching more than one element (with no single visible winner) raises
        with a candidate digest instead of guessing. Waits for the element to
        be visible, enabled and stable, and returns a Receipt naming what the
        click actually hit.
        """
        await self._ensure_front()
        el = await inject.resolve_element(self, selector)
        await inject.check_actionable(
            self, el, ("visible", "enabled", "stable"), selector=selector
        )
        return await self._click_element(
            el, selector=selector, button=button, click_count=click_count
        )

    async def type_text(
        self,
        selector: str,
        text: str,
        *,
        delay_ms: int = 0,
        press_enter: bool = False,
    ) -> Receipt:
        """Clear field and type text. Auto-waits up to 2s, strictly resolved.

        Selects all existing content, types over it via key events, then
        *verifies the value landed*. If the keystrokes changed nothing — the
        signature of a framework-controlled input swallowing them — it falls
        back to setting the value through the native prototype setter plus
        bubbled input/change events, and re-verifies. The Receipt says which
        path the text took.
        """
        await self._ensure_front()
        el = await inject.resolve_element(self, selector)
        await inject.check_actionable(
            self, el, ("visible", "enabled", "editable", "stable"), selector=selector
        )
        await self._exec("DOM.focus", {"objectId": el.object_id})
        await inject.call_engine(
            self,
            "function(el) { if (el.select) el.select();"
            " else el.ownerDocument.execCommand('selectAll'); }",
            [{"objectId": el.object_id}],
        )

        before = await inject.read_value(self, el)
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

        after = await inject.read_value(self, el)
        if after == text:
            note = "value verified"
        elif after == before:
            # Keystrokes changed nothing — a controlled input reverted them.
            # `after != before but != text` is deliberately NOT this branch:
            # a masking/formatting field transformed the input legitimately,
            # and overwriting it with the raw text would undo the widget.
            await inject.set_value_native(self, el, text)
            final = await inject.read_value(self, el)
            note = (
                "value verified via native-setter fallback"
                if final == text
                else f"value not verified (got {final!r})"
            )
        else:
            note = f"value transformed to {after!r}"

        preview, sel = await inject.describe_element(self, el)
        tdesc = " — ".join(p for p in (preview, sel) if p)
        receipt = Receipt(line=f"typed into {tdesc} ({note})")

        if press_enter:
            await self.key("Enter")
        return receipt

    async def select_option(self, selector: str, option: str) -> Receipt:
        """Select *option* in a dropdown — native <select> or custom listbox.

        Native <select>: finds the option by label (else value) and sets it
        through the prototype setter + input/change events. Custom widgets
        (react-select-style): clicks the field, waits for a role=option
        matching *option* (exact, then substring), clicks it. A miss lists
        the visible options' accessible names.
        """
        await self._ensure_front()
        el = await inject.resolve_element(self, selector)
        await inject.check_actionable(
            self, el, ("visible", "enabled", "stable"), selector=selector
        )
        tag_r = await inject.call_engine(
            self, "function(el) { return el.tagName; }", [{"objectId": el.object_id}]
        )
        preview, sel = await inject.describe_element(self, el)
        tdesc = " — ".join(p for p in (preview, sel) if p)

        if tag_r.get("result", {}).get("value") == "SELECT":
            v = await inject.select_native(self, el, option)
            if not v.get("ok"):
                raise RuntimeError(
                    f"option {option!r} not found in {tdesc}"
                    f" — options: {v.get('options')}"
                )
            return Receipt(line=f'selected "{option}" in {tdesc}')

        await self._click_element(el, selector=selector)
        opt_label = f"option {option!r}"
        opt_selectors = [
            f"role=option[name={selector_mod._text_body(option, exact=True)}]",
            f"role=option[name*={selector_mod._text_body(option, exact=True)}]",
        ]
        try:
            opt_el = await inject.resolve_engine_selectors(
                self, opt_selectors, opt_label
            )
        except inject.AmbiguousSelectorError:
            raise
        except RuntimeError as exc:
            names = await inject.list_option_names(self)
            if names:
                raise RuntimeError(
                    f"{opt_label} not found after opening {tdesc}"
                    f" — visible options: {names}"
                ) from exc
            raise RuntimeError(
                f"{opt_label} not found — no listbox opened after clicking {tdesc}"
            ) from exc
        await inject.check_actionable(
            self, opt_el, ("visible", "stable"), selector=opt_label
        )
        await self._click_element(opt_el, selector=opt_label)

        # Best-effort: the field (or its re-rendered value) should now carry
        # the option text. try/except because the click may have replaced the
        # field element outright, which detaches our handle — not a failure.
        verified = False
        try:
            v = await inject.call_engine(
                self,
                "function(el, opt) { const t = (el.value || '') + ' ' +"
                " (el.textContent || ''); return t.includes(opt); }",
                [{"objectId": el.object_id}, {"value": option}],
            )
            verified = bool(v.get("result", {}).get("value"))
        except Exception:
            pass
        suffix = "" if verified else " (selection not verified)"
        return Receipt(line=f'selected "{option}" in {tdesc}{suffix}')

    async def key(self, key: str) -> None:
        """Dispatch a keyDown+keyUp pair for a named key (e.g. "Enter", "Escape", "Space").

        `key` is a KeyboardEvent.key value ("Space" is accepted as a friendly
        alias for its real value, " ").
        """
        await self._ensure_front()
        key = _KEY_ALIASES.get(key, key)
        code = _KEY_CODE.get(key, key)
        text = _KEY_TEXT.get(key)
        for event_type in ("keyDown", "keyUp"):
            params: dict[str, str] = {"type": event_type, "key": key, "code": code}
            # Chromium's native button/checkbox/form activation for Enter and
            # Space only fires when the keyDown event also carries the
            # produced character in `text` — verified live: without it, the
            # DOM keydown/keyup still dispatch but no click ever follows,
            # indistinguishable from the key doing nothing.
            if event_type == "keyDown" and text:
                params["text"] = text
            await self._exec("Input.dispatchKeyEvent", params)

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

    async def tap(self, selector_or_x, y: float | None = None) -> Receipt:
        """Touch tap. Accepts a selector or (x, y) coordinates.

        Uses Input.dispatchTouchEvent — triggers touchstart/touchend listeners
        that dispatchMouseEvent won't reach. Use for mobile Chrome via ADB.
        The selector form resolves strictly, waits for visible/stable, and
        returns a Receipt like click(); the raw-coordinate form has no target
        element to check, so its receipt is just the point.
        """
        await self._ensure_front()
        if y is not None:
            x, y = float(selector_or_x), y
            receipt = Receipt(line=f"tapped ({x:.0f},{y:.0f})")
        else:
            selector = selector_or_x
            el = await inject.resolve_element(self, selector)
            await inject.check_actionable(
                self, el, ("visible", "stable"), selector=selector
            )
            x, y = await self._element_center_of(el, selector)
            receipt = await self._receipt_for(el, x, y, verb="tapped")

        tp = [{"x": x, "y": y}]
        await self._touch("touchStart", tp)
        await self._touch("touchEnd", [])
        return receipt

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
        await self._ensure_front()
        await self._touch("touchStart", [{"x": x1, "y": y1}])
        delay = duration_ms / steps / 1000
        for i in range(1, steps + 1):
            frac = i / steps
            cx = x1 + (x2 - x1) * frac
            cy = y1 + (y2 - y1) * frac
            await self._touch("touchMove", [{"x": cx, "y": cy}])
            await asyncio.sleep(delay)
        await self._touch("touchEnd", [])

    async def scroll(
        self,
        selector: str,
        dy: float = 0,
        dx: float = 0,
        *,
        steps: int = 10,
        duration_ms: int = 300,
    ) -> None:
        """Touch-scroll a container by (dx, dy) pixels. Auto-waits up to 2s.

        Sugar over swipe() — resolves selector to its center, then swipes in
        the opposite direction (scrollBy semantics: positive dy scrolls down,
        positive dx scrolls right). For scrolling on mobile Chrome via ADB.
        """
        el = await inject.resolve_element(self, selector)
        cx, cy = await self._element_center_of(el, selector)
        await self.swipe(cx, cy, cx - dx, cy - dy, steps=steps, duration_ms=duration_ms)

    async def tree(self, mode: str = "aria") -> list[str]:
        """Accessibility snapshot as text lines. Standalone read — no settle.

        mode="aria" (default): Playwright ariaSnapshot with [ref=…] handles,
        usable as aria-ref=<ref> selectors in click/type until the next
        snapshot or navigation. mode="ax": the raw CDP accessibility tree
        (pierces same-process iframes, no refs).
        """
        from .observe import build_aria_tree, build_tree

        if mode == "ax":
            return await build_tree(self)
        return await build_aria_tree(self)

    async def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        body: "dict | str | None" = None,
        headers: "dict[str, str] | None" = None,
    ) -> dict:
        """In-page JS fetch with Python-ergonomic args.

        Returns {status: int, ok: bool, body: Any, base64Encoded: bool}.
        Body is auto-parsed as JSON when content-type is json (text responses
        only). Binary responses (invalid UTF-8) are base64-encoded and
        returned with base64Encoded=True, matching the {body, base64Encoded}
        convention used by tab.body() / Fetch.getResponseBody. Content-Type
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
  const buf = await r.arrayBuffer();
  const bytes = new Uint8Array(buf);
  let body, base64Encoded = false;
  try {{
    body = new TextDecoder('utf-8', {{fatal: true}}).decode(bytes);
    if (ct.includes('json') && body) {{
      try {{ body = JSON.parse(body); }} catch(e) {{}}
    }}
  }} catch(e) {{
    // Invalid UTF-8 — binary payload. toBase64() is Chrome 140+, which is
    // repld's floor; the btoa fallback it replaced had to chunk by hand,
    // because String.fromCharCode spread over a whole buffer blows the call
    // stack somewhere north of 64K arguments.
    body = bytes.toBase64();
    base64Encoded = true;
  }}
  return {{status: r.status, ok: r.ok, body: body, base64Encoded: base64Encoded}};
}})()
"""
        return await self.js(code, await_promise=True)

    async def wait_for(self, selector: str, *, timeout: float = 5.0) -> None:
        """Wait for an element matching *selector* to appear in the DOM.

        Uses the same selector syntax as click/type_text (CSS, text=, role=,
        label=, :has-text). Existence only — no strictness or visibility, so
        it can watch for elements a later click would still have to
        disambiguate. Polls every 0.1s up to *timeout* seconds. Raises
        RuntimeError if the element never appears.
        """
        deadline = asyncio.get_running_loop().time() + timeout
        while not await inject.selector_exists(self, selector):
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError(f"Element not found: {selector}")
            await asyncio.sleep(0.1)

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

    async def close(self) -> None:
        """Close this tab (Target.closeTarget). Session cleanup follows from
        the resulting Target.targetDestroyed event, same as a user closing it."""
        await self._exec("Target.closeTarget", {"targetId": self._chrome_target_id})

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
        import os
        import time

        from ..paths import RUNTIME_DIR, ensure_runtime_dir
        from ..state import open_private

        params: dict = {"format": "png"}
        if full_page:
            params["captureBeyondViewport"] = True

        metrics = await self._exec("Page.getLayoutMetrics", {})
        # Measure whatever CDP actually captured. With captureBeyondViewport
        # the PNG is the full content box, so sizing the resize off the visual
        # viewport squashed a tall full-page shot down to viewport height —
        # and reported a `scale` that mapped back to nothing.
        box = metrics.get("cssContentSize" if full_page else "cssVisualViewport", {})
        src_w = int(box.get("width") or box.get("clientWidth") or 0)
        src_h = int(box.get("height") or box.get("clientHeight") or 0)

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
            ensure_runtime_dir()
            tid = self.target_id.replace(":", "-")
            # `{pid}-` prefix like task spills: it is what lets a later kernel
            # boot sweep this file once this process is gone.
            out = RUNTIME_DIR / f"{os.getpid()}-screenshot-{tid}-{int(time.time())}.png"

        def write(data: bytes) -> None:
            if path:
                # Caller picked the location — respect their umask.
                out.write_bytes(data)
                return
            # 0600: a screenshot can show authenticated page content.
            with open_private(out, "wb") as f:
                f.write(data)

        await asyncio.get_running_loop().run_in_executor(None, write, img_bytes)
        return {
            "path": str(out),
            "source": {"width": src_w, "height": src_h},
            "model": {"width": tgt_w, "height": tgt_h},
            "scale": round(scale, 4),
            "bytes": len(img_bytes),
        }

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

    async def cookies(self) -> list[dict]:
        """Return all cookies for this tab via CDP."""
        result = await self._exec("Network.getCookies")
        return result.get("cookies", [])

    async def cdp(self, method: str, **params: Any) -> dict:
        """Raw CDP passthrough."""
        return await self._exec(method, params if params else None)

    def __repr__(self) -> str:
        return f"<Tab {self.target_id} {self.url!r}>"
