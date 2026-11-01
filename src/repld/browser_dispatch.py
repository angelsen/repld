"""browser_* MCP tool-call handlers.

Split out of protocol.py: this is the browser-plumbing half of the
Dispatcher (routes browser_* tool calls to Tab/Browser methods, wraps
mutations with the observe pipeline). MCP protocol routing (tools/list,
resources, exec/get_task/cancel, gist tools) stays in protocol.py, which
mixes BrowserDispatchMixin into its Dispatcher class.
"""

import asyncio
import json
import threading
from typing import ClassVar

import __main__

from . import tasks
from .kernel_context import KernelContext

# ipc.Session for the tool call currently dispatching on this IPC reader
# thread, if any. `_run_async` hands it to `tasks._tool_caller` so the
# tab's action window can name the caller.  Thread-local because
# `_browser_tool` runs on the reader thread, one per connection; a module
# global would let two sessions' calls race each other.
_dispatch_session: threading.local = threading.local()

# Wall-clock ceiling on one loop round-trip made from the IPC thread.
# Bounds every browser tool and every async gist tool (see `_run_async`).
_ASYNC_CALL_TIMEOUT: float = 30.0

# Settle deadlines, scaled to how much work the mutation can plausibly kick
# off. All three are *ceilings*, not waits: `observe.settle` returns as soon as
# the network has been quiet for its `quiet` window, so a bigger number costs
# nothing on a page that goes idle promptly and only buys headroom on one that
# doesn't. A navigation or a freshly opened tab loads a document and everything
# beneath it; a click, keypress or typed field usually triggers an XHR or two;
# `invoke()` calls an app's own control and is the same shape as a click.
#
# `_SETTLE_NAVIGATION_S` is the largest of the three, which makes it the real
# upper bound on how long a request may legitimately sit in a session's
# `_inflight` map. `cdp._INFLIGHT_MAX_AGE` is chosen to clear it, so raising
# this means revisiting that.
_SETTLE_NAVIGATION_S: float = 8.0
_SETTLE_INTERACTION_S: float = 5.0
_SETTLE_DEFAULT_S: float = 3.0

# Grace period after the last keystroke of `type_text`, before the observation
# settles. Typing dispatches key events and returns; an app that debounces its
# input (search-as-you-type being the standard case) has not issued its request
# yet at that moment, so settle would see a quiet network and return before the
# request it is meant to capture ever entered flight. Long enough for a typical
# debounce, short enough not to pad every `type_text` noticeably.
_POST_TYPE_DEBOUNCE_S: float = 0.3


# Noise control, not redaction. repld's contract is that the agent works with
# the user's real sessions, so nothing is hidden: `full=true` (or
# `tab.request()` in exec) returns everything. The cap only trims the cookie
# jar and bearer tokens that otherwise dominate every request dump.
_HEADER_VALUE_CAP: int = 120
_COOKIE_VALUE_CAP: int = 24


def _cap(value: object, limit: int) -> object:
    """Truncate a string beyond `limit`, preserving non-strings unchanged."""
    if not isinstance(value, str) or len(value) <= limit:
        return value
    return f"{value[:limit]}...(+{len(value) - limit} chars, full=true for the rest)"


def _cap_side(part: dict) -> None:
    """Cap header values and cookie values in one request or response dict."""
    headers = part.get("headers")
    if isinstance(headers, dict):
        for k, v in headers.items():
            if k.lower() in ("cookie", "set-cookie") and isinstance(v, str):
                headers[k] = "; ".join(
                    f"{n}={_cap(val, _COOKIE_VALUE_CAP)}" if val else pair
                    for pair in v.split("; ")
                    for n, _, val in (pair.partition("="),)
                )
            else:
                headers[k] = _cap(v, _HEADER_VALUE_CAP)
    cookies = part.get("cookies")
    if isinstance(cookies, list):
        for c in cookies:
            if isinstance(c, dict) and "value" in c:
                c["value"] = _cap(c["value"], _COOKIE_VALUE_CAP)


def _compact_credentials(entry: dict) -> dict:
    """Return a deep copy of a HAR entry with long credentials truncated."""
    out: dict = json.loads(json.dumps(entry, default=str))
    for side in ("request", "response"):
        part = out.get(side)
        if isinstance(part, dict):
            _cap_side(part)
    return out


async def route_detach(browser, target, port) -> str | None:
    """Shared target/port detach routing (MCP tool + dashboard RPC).

    Returns None when neither target nor port is given. The no-arg
    fallbacks differ by design (MCP: detach tabs, keep the WebSocket;
    dashboard: full disconnect) and stay at the call sites.
    """
    if target:
        b = browser.browser_for(target)
        return await b.detach_target(target)
    if port is not None:
        return await browser.disconnect(port)
    return None


def _raise_on_unresolved_dialog(tab, pre) -> None:
    """Fail the call if a mutation triggered a rejected dialog or an unresolved chooser.

    Belt-and-suspenders with Tab's own action methods, which already raise
    for either on their own session. This still matters for one fired on a
    *different* target (a cross-origin iframe) during the same mutation,
    which a raw `tab.click()` has no way to see.
    """
    from .browser.cdp import unresolved_dialog_error, unresolved_filechooser_error

    for t in (tab, *pre.iframe_children):
        exc = unresolved_dialog_error(t._session._dialog_log)
        if exc is not None:
            raise exc
        exc = unresolved_filechooser_error(t._session._filechooser_log)
        if exc is not None:
            raise exc


class BrowserDispatchMixin:
    """Browser tool-call handlers, mixed into protocol.Dispatcher.

    Relies on `self.ctx` (`KernelContext`, for `.loop`) set by
    `Dispatcher.__init__`.
    """

    ctx: KernelContext

    def _browser_tool(self, rid, name: str, args: dict, session=None) -> dict:
        """Dispatch one browser_* tool call, spilling large output."""
        from .protocol import _tool_error

        _dispatch_session.current = session
        try:
            result = self._browser_dispatch(name, args)
            if isinstance(result, str):
                # Observation text: pass directly to spill pipeline
                return self._build_spill_response(rid, result, label=name)
            text = json.dumps(result, default=str, indent=2)
            return self._build_spill_response(rid, text, label=name)
        except Exception as exc:
            return _tool_error(rid, f"{name}: {exc}")
        finally:
            _dispatch_session.current = None

    def _build_spill_response(self, rid, text: str, label: str = "output") -> dict:
        """Build a tool/resource response using the unified spill pipeline."""
        from .protocol import _format_spill, _response
        from .tasks import spill_text as _spill_text

        sp = _spill_text(text, label=label)
        return _response(
            rid, {"content": [{"type": "text", "text": _format_spill(sp, text)}]}
        )

    def _get_browser(self):
        """Return the browser object from `__main__`, raising if absent."""
        browser = __main__.__dict__.get("browser")
        if browser is None:
            raise RuntimeError(
                "browser builtin not available: kernel not running or browser extra not installed"
            )
        return browser

    def _run_async(self, coro):
        """Run a coroutine on the repld asyncio loop from the IPC thread.

        The deadline is named in the exception because nothing else would say
        it: `concurrent.futures.TimeoutError` *is* `TimeoutError` and carries
        an empty message, so both callers' `f"{name}: {exc}"` would answer
        with the tool name, a colon, and nothing. User-controlled work
        (`browser_js` awaiting a promise, `browser_fetch`, `browser_cdp`, any
        async gist tool) can legitimately outrun 30 s.
        """
        caller = getattr(_dispatch_session, "current", None)

        async def _scoped():
            """Bind the IPC session as `_tool_caller` for action-window routing."""
            tasks._tool_caller.set(caller)
            return await coro

        fut = asyncio.run_coroutine_threadsafe(_scoped(), self.ctx.loop)
        try:
            return fut.result(timeout=_ASYNC_CALL_TIMEOUT)
        except TimeoutError:
            # Same class either way, so `fut.done()` (not the exception) is
            # what says whose deadline this was. Settled means the coroutine
            # raised a TimeoutError of its own, or landed in the race between
            # our deadline expiring and this check; taking the result answers
            # both correctly instead of masking them with the message below.
            if fut.done():
                return fut.result()
            raise TimeoutError(
                f"still running on the kernel loop after {_ASYNC_CALL_TIMEOUT:.0f}s "
                "(abandoned, not cancelled, so it may still complete). Work that "
                "takes this long belongs in `exec`, which defers past its timeout "
                "and reports back on channel."
            ) from None

    def _run_sync_on_loop(self, fn, *args):
        """Run a *synchronous* browser call on the kernel loop.

        Handlers run on the IPC reader thread, and most reach the browser
        through `_run_async`, so they land on the loop by construction. `clear`
        did not: it resets `_event_count` and empties `_inflight`, which settle
        reads from the loop, and a write to loop-owned state belongs on the
        loop.

        Not for the DuckDB query methods. `tab.network()`, `console()`,
        `body()` and friends are deliberately off-loop on a per-call cursor,
        which is the whole reason a Refresh in the dashboard doesn't stall the
        kernel.
        """

        async def _call():
            """Wrap a sync call so `_run_async` can schedule it."""
            return fn(*args)

        return self._run_async(_call())

    def _resolve_tab(self, browser, args):
        """Resolve the `target` argument to a live `Tab`."""
        return self._run_async(browser.get(args["target"]))

    def _browser_dispatch(self, name: str, args: dict):
        """Route a tool name to its handler and invoke it.

        Returns a JSON-serializable result, or a plain str for observation text.
        """
        handler = self._BROWSER_DISPATCH.get(name)
        if handler is None:
            raise ValueError(f"Unknown browser tool: {name}")
        return handler(self, self._get_browser(), args)

    # ------------------------------------------------------------------
    # Browser handlers: browser-level (no tab)
    # ------------------------------------------------------------------

    def _bh_watch(self, browser, args):
        """Attach to tabs matching a URL pattern."""
        return self._run_async(browser.watch(args["pattern"]))

    def _bh_detach(self, browser, args):
        """Detach a target, port, or pattern-matched tabs."""
        result = self._run_async(
            route_detach(browser, args.get("target"), args.get("port"))
        )
        if result is None:
            result = self._run_async(browser.detach(args.get("pattern")))
        return result

    def _bh_tabs(self, browser, args):
        """Return the nested tab listing across all connected browsers."""
        return self._run_async(browser.format_tabs_nested())

    def _bh_pages(self, browser, args):
        """Return flat page metadata for all attached targets."""
        return self._run_async(browser.pages())

    def _bh_clear(self, browser, args):
        """Reset event counters and in-flight state for a target (on the loop)."""
        return self._run_sync_on_loop(browser.clear, args.get("target"))

    def _bh_controls(self, browser, args):
        """Return `window.controls` for a tab, or a null sentinel."""
        tab = self._resolve_tab(browser, args)
        result = self._run_async(tab.controls())
        if result is None:
            return {"controls": None, "message": "No window.controls on this tab"}
        return result

    def _bh_dismiss_dialog(self, browser, args):
        """Accept or dismiss a pending JavaScript dialog."""
        tab = self._resolve_tab(browser, args)
        return self._run_async(
            tab.dismiss_dialog(bool(args.get("accept", True)), args.get("prompt_text"))
        )

    def _bh_take(self, browser, args):
        """Claim exclusive ownership of a tab for this session."""
        return self._run_async(self._resolve_tab(browser, args).take())

    def _bh_release(self, browser, args):
        """Release a taken tab back to shared mode."""
        return self._run_async(self._resolve_tab(browser, args).release())

    def _bh_expect_file_chooser(self, browser, args):
        """Pre-arm the file chooser intercept with the given paths."""
        tab = self._resolve_tab(browser, args)
        return self._run_async(tab.expect_file_chooser(list(args["paths"])))

    def _bh_expect_auth(self, browser, args):
        """Pre-arm the HTTP auth challenge handler with credentials."""
        tab = self._resolve_tab(browser, args)
        return self._run_async(tab.expect_auth(args["username"], args["password"]))

    def _bh_grant_permissions(self, browser, args):
        """Grant browser permissions (e.g. clipboard) to the tab's origin."""
        tab = self._resolve_tab(browser, args)
        return self._run_async(
            tab.grant_permissions(list(args["permissions"]), args.get("origin"))
        )

    def _bh_invoke(self, browser, args):
        """Invoke a `window.controls` action and observe the resulting mutation.

        Returns both the control's result and the observation text: the
        return value is the one thing the tool is named for, and the app's
        own `console.debug("__controls__", ...)` is opt-in instrumentation,
        not a substitute for it. Nested rather than spread, like `_bh_js`:
        the payload comes from `window.controls.invoke()`, so its shape is
        the app's to decide.
        """
        tab = self._resolve_tab(browser, args)
        invoke_args = args.get("args")
        captured: dict = {}

        def mutate():
            captured["result"] = self._run_async(
                tab.invoke(args["control"], args["action"], invoke_args)
            )

        observation = self._observed_mutation(
            browser, tab, mutate, timeout=_SETTLE_DEFAULT_S
        )
        return {"result": captured.get("result"), "observation": observation}

    # ------------------------------------------------------------------
    # Browser handlers: tab read-only
    # ------------------------------------------------------------------

    def _bh_js(self, browser, args):
        """Evaluate JavaScript in the tab and return the result.

        Wrapped (unlike watch/detach/clear's fixed prose messages): the JS
        result is dynamically typed (str/int/bool/dict/list/None) and
        `_browser_tool`'s `isinstance(result, str)` check would otherwise
        treat a string-valued JS result as pre-formatted text and pass it
        through unencoded instead of JSON-encoding it.
        """
        tab = self._resolve_tab(browser, args)
        ap = args.get("await_promise", True)
        return {"result": self._run_async(tab.js(args["code"], await_promise=ap))}

    def _bh_network(self, browser, args):
        """Query the tab's captured network log with optional filters."""
        tab = self._resolve_tab(browser, args)
        rows = tab.network(
            url=args.get("url"),
            method=args.get("method"),
            status=args.get("status"),
            type=args.get("type"),
            include_assets=bool(args.get("include_assets", False)),
        )
        return [repr(r) for r in rows]

    def _bh_request(self, browser, args):
        """Return a single HAR entry, credential-capped unless `full=true`."""
        tab = self._resolve_tab(browser, args)
        entry = tab.request(args["request_id"])
        if args.get("full"):
            return entry
        return _compact_credentials(entry)

    def _bh_body(self, browser, args):
        """Return the response body for a captured request."""
        tab = self._resolve_tab(browser, args)
        return tab.body(args["request_id"])

    def _bh_fetch(self, browser, args):
        """Execute an HTTP request through the tab's page context."""
        tab = self._resolve_tab(browser, args)
        return self._run_async(
            tab.fetch(
                args["url"],
                method=args.get("method", "GET"),
                body=args.get("body"),
                headers=args.get("headers"),
            )
        )

    def _bh_console(self, browser, args):
        """Query the tab's console log with optional level and source filters."""
        tab = self._resolve_tab(browser, args)
        rows = tab.console(
            level=args.get("level"),
            source=args.get("source"),
        )
        return [repr(r) for r in rows]

    def _bh_screenshot(self, browser, args):
        """Capture a PNG screenshot of the tab and return its path."""
        tab = self._resolve_tab(browser, args)
        info = self._run_async(
            tab.screenshot(
                full_page=bool(args.get("full_page", False)),
                force=bool(args.get("force", False)),
            )
        )
        return "\n".join(
            [
                f"Screenshot saved to {info['path']}",
                f"{info['width']}x{info['height']} ({info['bytes'] // 1024}KB PNG)",
                "Use Read to view it.",
            ]
        )

    def _bh_cdp(self, browser, args):
        """Send a raw CDP command to the tab's session."""
        tab = self._resolve_tab(browser, args)
        params = args.get("params") or {}
        return self._run_async(tab.cdp(args["method"], **params))

    def _resolve_session(self, browser, tab):
        """Return the `BrowserSession` that owns this tab (multi-browser aware)."""
        return browser.browser_for(tab.target_id)._session

    def _bh_tree(self, browser, args):
        """Build the accessibility tree for a tab, composing across iframes.

        An `at=` coordinate hit-tests a point instead and returns a small
        subtree rooted there. No iframe composition in that case: an `at=`
        hit on an iframe redirects the caller to a fresh call against that
        target's own session instead (see `Tab.tree`'s docstring).
        """
        from .browser import captcha
        from .browser.observe import compose_aria_tree, compose_tree

        tab = self._resolve_tab(browser, args)
        if args.get("at") is not None:
            return "\n".join(
                self._run_async(tab.tree(at=args["at"], in_crop=args.get("in_crop")))
            )
        session = self._resolve_session(browser, tab)
        if args.get("mode") == "ax":
            lines, _, _ = self._run_async(compose_tree(tab, session))
        else:
            lines, _ = self._run_async(compose_aria_tree(tab, session))
        try:
            found = self._run_async(captcha.detect(tab))
        except Exception:
            found = []
        lines = list(lines) + [captcha.describe(c) for c in found]
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Browser handlers: tab mutations (with observe)
    # ------------------------------------------------------------------

    def _observed_mutation(self, browser, tab, mutate, *, timeout: float) -> str:
        """Run `pre_observe`, then `mutate()`, then `post_observe` around a tab mutation."""
        from .browser.observe import post_observe, pre_observe

        session = self._resolve_session(browser, tab)
        pre = self._run_async(pre_observe(tab, session))
        mutate()
        text = self._run_async(post_observe(tab, session, pre, timeout=timeout))
        _raise_on_unresolved_dialog(tab, pre)
        return text

    def _receipt_mutation(
        self, browser, tab, coro_factory, *, after_s: float = 0.0
    ) -> str:
        """Observed mutation for a receipt-returning Tab call.

        The receipt line goes above the observation. `after_s` sleeps inside
        mutate(), so the settle window opens only after the debounce.
        """
        captured: dict = {}

        def mutate():
            """Run the Tab coroutine and optionally sleep for debounce."""
            captured["receipt"] = self._run_async(coro_factory())
            if after_s:
                self._run_async(asyncio.sleep(after_s))

        obs = self._observed_mutation(
            browser, tab, mutate, timeout=_SETTLE_INTERACTION_S
        )
        return f"{captured['receipt']}\n\n{obs}"

    def _bh_navigate(self, browser, args):
        """Navigate a tab to a URL, refusing iframes unless `force=true`.

        Navigating an iframe target would destroy the embedded app session.
        The error names the parent target so the caller can navigate there
        instead.
        """
        tab = self._resolve_tab(browser, args)
        if tab.type == "iframe" and not args.get("force"):
            from .browser.target import make_target

            parent_short = (
                make_target(tab._port, tab.parent_frame_id)
                if tab.parent_frame_id
                else "unknown"
            )
            raise ValueError(
                f"Cannot navigate iframe target {tab.target_id}: "
                f"this would destroy the embedded app session. "
                f"Use click/fetch on the iframe for in-app navigation, "
                f"or navigate the parent ({parent_short}). "
                f"Pass force=true to override."
            )
        return self._observed_mutation(
            browser,
            tab,
            lambda: self._run_async(tab.navigate(args["url"])),
            timeout=_SETTLE_NAVIGATION_S,
        )

    def _bh_open(self, browser, args):
        """Open a new tab at a URL and observe the initial page load."""
        from .browser.observe import PreObservation, post_observe

        tab = self._run_async(browser.open(args["url"]))
        if args.get("viewport"):
            w, _, h = str(args["viewport"]).lower().partition("x")
            self._run_async(tab.set_viewport(int(w), int(h)))
        session = self._resolve_session(browser, tab)
        key = tab.target_id
        pre = PreObservation(iframe_children=[], snapshots={key: 0})
        return self._run_async(
            post_observe(
                tab,
                session,
                pre,
                timeout=_SETTLE_NAVIGATION_S,
                extra_header=f"target: {tab.target_id}",
            )
        )

    def _bh_key(self, browser, args):
        """Dispatch a single key or a sequence of keys to the tab."""
        tab = self._resolve_tab(browser, args)
        if args.get("keys"):
            mutate = lambda: self._run_async(tab.keys(list(args["keys"])))
        elif args.get("key"):
            mutate = lambda: self._run_async(tab.key(args["key"]))
        else:
            raise ValueError("browser_key needs 'key' or 'keys'")
        return self._observed_mutation(
            browser, tab, mutate, timeout=_SETTLE_INTERACTION_S
        )

    def _bh_click(self, browser, args):
        """Click a selector and observe the mutation."""
        tab = self._resolve_tab(browser, args)
        return self._receipt_mutation(browser, tab, lambda: tab.click(args["selector"]))

    def _bh_type(self, browser, args):
        """Type text into a selector, with a post-type debounce for settle."""
        tab = self._resolve_tab(browser, args)
        return self._receipt_mutation(
            browser,
            tab,
            lambda: tab.type_text(
                args["selector"],
                args["text"],
                press_enter=bool(args.get("press_enter", False)),
            ),
            after_s=_POST_TYPE_DEBOUNCE_S,
        )

    def _bh_hover(self, browser, args):
        """Hover over a selector and observe the mutation."""
        tab = self._resolve_tab(browser, args)
        return self._receipt_mutation(browser, tab, lambda: tab.hover(args["selector"]))

    def _bh_drag(self, browser, args):
        """Drag from one point or selector to another."""
        tab = self._resolve_tab(browser, args)
        return self._receipt_mutation(
            browser,
            tab,
            lambda: tab.drag(
                args["from"],
                args["to"],
                steps=int(args.get("steps", 12)),
                duration_ms=int(args.get("duration_ms", 400)),
                dwell_ms=int(args.get("dwell_ms", 150)),
            ),
        )

    def _bh_select(self, browser, args):
        """Select an option from a dropdown (native or custom listbox)."""
        tab = self._resolve_tab(browser, args)
        return self._receipt_mutation(
            browser, tab, lambda: tab.select_option(args["selector"], args["option"])
        )

    def _bh_set_files(self, browser, args):
        """Set file paths on a pending file chooser intercept."""
        tab = self._resolve_tab(browser, args)
        return self._receipt_mutation(
            browser, tab, lambda: tab.set_files(list(args["paths"]))
        )

    _BROWSER_DISPATCH: ClassVar[dict] = {
        "browser_watch": _bh_watch,
        "browser_detach": _bh_detach,
        "browser_tabs": _bh_tabs,
        "browser_pages": _bh_pages,
        "browser_clear": _bh_clear,
        "browser_js": _bh_js,
        "browser_network": _bh_network,
        "browser_request": _bh_request,
        "browser_body": _bh_body,
        "browser_fetch": _bh_fetch,
        "browser_console": _bh_console,
        "browser_screenshot": _bh_screenshot,
        "browser_cdp": _bh_cdp,
        "browser_tree": _bh_tree,
        "browser_navigate": _bh_navigate,
        "browser_open": _bh_open,
        "browser_key": _bh_key,
        "browser_click": _bh_click,
        "browser_type": _bh_type,
        "browser_select": _bh_select,
        "browser_hover": _bh_hover,
        "browser_drag": _bh_drag,
        "browser_controls": _bh_controls,
        "browser_invoke": _bh_invoke,
        "browser_dismiss_dialog": _bh_dismiss_dialog,
        "browser_take": _bh_take,
        "browser_release": _bh_release,
        "browser_set_files": _bh_set_files,
        "browser_expect_file_chooser": _bh_expect_file_chooser,
        "browser_expect_auth": _bh_expect_auth,
        "browser_grant_permissions": _bh_grant_permissions,
    }
