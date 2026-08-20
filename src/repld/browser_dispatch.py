"""browser_* MCP tool-call handlers.

Split out of protocol.py: this is the browser-plumbing half of the
Dispatcher (routes browser_* tool calls to Tab/Browser methods, wraps
mutations with the observe pipeline). MCP protocol routing (tools/list,
resources, exec/get_task/cancel, gist tools) stays in protocol.py, which
mixes BrowserDispatchMixin into its Dispatcher class.
"""

import __main__
import asyncio
import json

from .kernel_context import KernelContext

# Wall-clock ceiling on one loop round-trip made from the IPC thread. Bounds
# every browser tool and every async gist tool — see `_run_async`.
_ASYNC_CALL_TIMEOUT = 30.0

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
# `_inflight` map — `cdp._INFLIGHT_MAX_AGE` is chosen to clear it, so raising
# this means revisiting that.
_SETTLE_NAVIGATION_S = 8.0
_SETTLE_INTERACTION_S = 5.0
_SETTLE_DEFAULT_S = 3.0

# Grace period after the last keystroke of `type_text`, before the observation
# settles. Typing dispatches key events and returns; an app that debounces its
# input (search-as-you-type being the standard case) has not issued its request
# yet at that moment, so settle would see a quiet network and return before the
# request it is meant to capture ever entered flight. Empirical — long enough
# for a typical debounce, short enough not to pad every type_text noticeably.
_POST_TYPE_DEBOUNCE_S = 0.3


# Noise control, not redaction — repld's contract is that the agent works with
# the user's real sessions, so nothing is hidden: `full=true` (or tab.request()
# in exec) returns everything. The cap only trims the cookie jar and bearer
# tokens that otherwise dominate every request dump.
_HEADER_VALUE_CAP = 120
_COOKIE_VALUE_CAP = 24


def _cap(value, limit: int):
    if not isinstance(value, str) or len(value) <= limit:
        return value
    return f"{value[:limit]}…(+{len(value) - limit} chars — full=true for the rest)"


def _compact_credentials(entry: dict) -> dict:
    out = json.loads(json.dumps(entry, default=str))  # deep copy, wire-shaped
    for side in ("request", "response"):
        part = out.get(side)
        if not isinstance(part, dict):
            continue
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
    return out


async def route_detach(browser, target, port) -> str | None:
    """Shared target/port detach routing (MCP tool + dashboard RPC).

    Returns None when neither target nor port is given — the no-arg
    fallbacks differ by design (MCP: detach tabs, keep the WebSocket;
    dashboard: full disconnect) and stay at the call sites.
    """
    if target:
        b = browser.browser_for(target)
        return await b.detach_target(target)
    if port is not None:
        return await browser.disconnect(port)
    return None


class BrowserDispatchMixin:
    """Browser tool-call handlers, mixed into protocol.Dispatcher.

    Relies on `self.ctx` (KernelContext, for `.loop`) set by
    Dispatcher.__init__.
    """

    ctx: KernelContext

    def _browser_tool(self, rid, name: str, args: dict) -> dict:
        """Dispatch a browser_* tool call."""
        from .protocol import _error

        try:
            result = self._browser_dispatch(name, args)
            if isinstance(result, str):
                # Observation text — pass directly to spill pipeline
                return self._spill_response(rid, result, label=name)
            text = json.dumps(result, default=str, indent=2)
            return self._spill_response(rid, text, label=name)
        except Exception as exc:
            return _error(rid, -32000, f"{name}: {exc}")

    def _spill_response(self, rid, text: str, label: str = "output") -> dict:
        """Build a tool/resource response using the unified spill pipeline."""
        from .protocol import _format_spill, _response
        from .tasks import spill_text as _spill_text

        sp = _spill_text(text, label=label)
        return _response(
            rid, {"content": [{"type": "text", "text": _format_spill(sp, text)}]}
        )

    def _get_browser(self):
        """Retrieve the browser object from __main__; raise if not available."""
        browser = __main__.__dict__.get("browser")
        if browser is None:
            raise RuntimeError(
                "browser builtin not available — kernel not running or browser extra not installed"
            )
        return browser

    def _run_async(self, coro):
        """Run a coroutine on the repld asyncio loop from the IPC thread.

        The deadline is named in the exception because nothing else would say
        it: `concurrent.futures.TimeoutError` *is* `TimeoutError` and carries
        an empty message, so both callers' `f"{name}: {exc}"` used to answer
        with the tool name, a colon, and nothing — for user-controlled work
        (`browser_js` awaiting a promise, `browser_fetch`, `browser_cdp`, any
        async gist tool) that can legitimately outrun 30s.
        """
        fut = asyncio.run_coroutine_threadsafe(coro, self.ctx.loop)
        try:
            return fut.result(timeout=_ASYNC_CALL_TIMEOUT)
        except TimeoutError:
            # Same class either way, so `fut.done()` — not the exception — is
            # what says whose deadline this was. Settled means the coroutine
            # raised a TimeoutError of its own, or landed in the race between
            # our deadline expiring and this check; taking the result answers
            # both correctly instead of masking them with the message below.
            if fut.done():
                return fut.result()
            raise TimeoutError(
                f"still running on the kernel loop after {_ASYNC_CALL_TIMEOUT:.0f}s "
                "— abandoned, not cancelled, so it may still complete. Work that "
                "takes this long belongs in `exec`, which defers past its timeout "
                "and reports back on channel."
            ) from None

    def _run_sync_on_loop(self, fn, *args):
        """Run a *synchronous* browser call on the kernel loop.

        Handlers run on the IPC reader thread, and most reach the browser
        through `_run_async`, so they land on the loop by construction. The two
        sync entry points below (`format_tabs_nested`, `clear`) did not, and
        both walk the session/browser maps the loop mutates on every tab that
        opens or closes; `clear` also resets `_event_count` and empties
        `_inflight`, which settle reads from the loop. The snapshots inside
        `browser/browser.py` and `browser/pool.py` keep the *reads* from
        crashing wherever they are
        called from, but a write to loop-owned state belongs on the loop, so
        these go there too.

        Not for the DuckDB query methods — `tab.network()`, `console()`,
        `body()` and friends are deliberately off-loop on a per-call cursor,
        which is the whole reason a Refresh in the dashboard doesn't stall the
        kernel.
        """

        async def _call():
            return fn(*args)

        return self._run_async(_call())

    def _get_tab(self, browser, args):
        return self._run_async(browser.get(args["target"]))

    def _browser_dispatch(self, name: str, args: dict):
        """Route to individual browser tool handler.

        Returns JSON-serializable result, OR a plain str for observation text.
        """
        handler = self._BROWSER_DISPATCH.get(name)
        if handler is None:
            raise ValueError(f"Unknown browser tool: {name}")
        return handler(self, self._get_browser(), args)

    # ------------------------------------------------------------------
    # Browser handlers — browser-level (no tab)
    # ------------------------------------------------------------------

    def _bh_watch(self, browser, args):
        return self._run_async(browser.watch(args["pattern"]))

    def _bh_detach(self, browser, args):
        result = self._run_async(
            route_detach(browser, args.get("target"), args.get("port"))
        )
        if result is None:
            result = self._run_async(browser.detach(args.get("pattern")))
        return result

    def _bh_tabs(self, browser, args):
        return self._run_sync_on_loop(browser.format_tabs_nested)

    def _bh_pages(self, browser, args):
        return self._run_async(browser.pages())

    def _bh_clear(self, browser, args):
        return self._run_sync_on_loop(browser.clear, args.get("target"))

    def _bh_controls(self, browser, args):
        tab = self._get_tab(browser, args)
        result = self._run_async(tab.controls())
        if result is None:
            return {"controls": None, "message": "No window.controls on this tab"}
        return result

    def _bh_invoke(self, browser, args):
        tab = self._get_tab(browser, args)
        invoke_args = args.get("args")
        captured: dict = {}

        def mutate():
            captured["result"] = self._run_async(
                tab.invoke(args["control"], args["action"], invoke_args)
            )

        observation = self._observed_mutation(
            browser, tab, mutate, timeout=_SETTLE_DEFAULT_S
        )
        # `tab.invoke` returns the control's InvokeResult and this handler used
        # to drop it on the floor for the observation text alone — so the one
        # thing the tool is named for never reached the agent, and the only
        # surface left was the app's own `console.debug("__controls__", …)`,
        # which is opt-in instrumentation rather than the invoke result. Both
        # halves are wanted: the return value *and* what it did to the page.
        # Nested rather than spread, like `_bh_js`: the payload comes from
        # `window.controls.invoke()`, so its shape is the app's to decide.
        return {"result": captured.get("result"), "observation": observation}

    # ------------------------------------------------------------------
    # Browser handlers — tab read-only
    # ------------------------------------------------------------------

    def _bh_js(self, browser, args):
        tab = self._get_tab(browser, args)
        ap = args.get("await_promise", True)
        # Wrapped (unlike watch/detach/clear's fixed prose messages): the JS
        # result is dynamically typed (str/int/bool/dict/list/None) and
        # _browser_tool's isinstance(result, str) check would otherwise treat
        # a string-valued JS result as pre-formatted text and pass it through
        # unencoded instead of JSON-encoding it.
        return {"result": self._run_async(tab.js(args["code"], await_promise=ap))}

    def _bh_network(self, browser, args):
        tab = self._get_tab(browser, args)
        rows = tab.network(
            url=args.get("url"),
            method=args.get("method"),
            status=args.get("status"),
            type=args.get("type"),
            include_assets=bool(args.get("include_assets", False)),
        )
        return [repr(r) for r in rows]

    def _bh_request(self, browser, args):
        tab = self._get_tab(browser, args)
        entry = tab.request(args["request_id"])
        if args.get("full"):
            return entry
        return _compact_credentials(entry)

    def _bh_body(self, browser, args):
        tab = self._get_tab(browser, args)
        return tab.body(args["request_id"])

    def _bh_fetch(self, browser, args):
        tab = self._get_tab(browser, args)
        return self._run_async(
            tab.fetch(
                args["url"],
                method=args.get("method", "GET"),
                body=args.get("body"),
                headers=args.get("headers"),
            )
        )

    def _bh_console(self, browser, args):
        tab = self._get_tab(browser, args)
        rows = tab.console(
            level=args.get("level"),
            source=args.get("source"),
        )
        return [repr(r) for r in rows]

    def _bh_screenshot(self, browser, args):
        tab = self._get_tab(browser, args)
        info = self._run_async(
            tab.screenshot(full_page=bool(args.get("full_page", False)))
        )
        src = info["source"]
        mdl = info["model"]
        lines = [
            f"Screenshot saved to {info['path']}",
            f"Captured: {src['width']}x{src['height']}  →  Resized: {mdl['width']}x{mdl['height']} ({info['bytes'] // 1024}KB PNG)",
            "Use Read to view it.",
        ]
        if info["scale"] < 1.0:
            lines.append(
                f"Coordinates: multiply by {1 / info['scale']:.2f} to map back to page pixels."
            )
        return "\n".join(lines)

    def _bh_cdp(self, browser, args):
        tab = self._get_tab(browser, args)
        params = args.get("params") or {}
        return self._run_async(tab.cdp(args["method"], **params))

    def _session_for(self, browser, tab):
        """Get the BrowserSession that owns this tab (multi-browser aware)."""
        return browser.browser_for(tab.target_id)._session

    def _bh_tree(self, browser, args):
        from .browser.observe import compose_aria_tree, compose_tree

        tab = self._get_tab(browser, args)
        session = self._session_for(browser, tab)
        if args.get("mode") == "ax":
            lines, _ = self._run_async(compose_tree(tab, session))
        else:
            lines, _ = self._run_async(compose_aria_tree(tab, session))
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Browser handlers — tab mutations (with observe)
    # ------------------------------------------------------------------

    def _observed_mutation(self, browser, tab, mutate, *, timeout: float):
        """Run pre_observe → mutate() → post_observe around a tab mutation."""
        from .browser.observe import post_observe, pre_observe

        session = self._session_for(browser, tab)
        pre = self._run_async(pre_observe(tab, session))
        mutate()
        return self._run_async(post_observe(tab, session, pre, timeout=timeout))

    def _bh_navigate(self, browser, args):
        tab = self._get_tab(browser, args)
        if tab.type == "iframe" and not args.get("force"):
            from .browser.target import make_target

            parent_short = (
                make_target(tab._port, tab.parent_frame_id)
                if tab.parent_frame_id
                else "unknown"
            )
            raise ValueError(
                f"Cannot navigate iframe target {tab.target_id} — "
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
        from .browser.observe import PreObservation, post_observe

        tab = self._run_async(browser.open(args["url"]))
        if args.get("viewport"):
            w, _, h = str(args["viewport"]).lower().partition("x")
            self._run_async(tab.set_viewport(int(w), int(h)))
        session = self._session_for(browser, tab)
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
        tab = self._get_tab(browser, args)
        if args.get("keys"):
            mutate = lambda: self._run_async(tab.keys(list(args["keys"])))  # noqa: E731
        elif args.get("key"):
            mutate = lambda: self._run_async(tab.key(args["key"]))  # noqa: E731
        else:
            raise ValueError("browser_key needs 'key' or 'keys'")
        return self._observed_mutation(
            browser, tab, mutate, timeout=_SETTLE_INTERACTION_S
        )

    def _bh_click(self, browser, args):
        tab = self._get_tab(browser, args)
        captured: dict = {}

        def mutate():
            captured["receipt"] = self._run_async(tab.click(args["selector"]))

        obs = self._observed_mutation(
            browser, tab, mutate, timeout=_SETTLE_INTERACTION_S
        )
        return f"{captured['receipt']}\n\n{obs}"

    def _bh_type(self, browser, args):
        tab = self._get_tab(browser, args)
        captured: dict = {}

        def mutate():
            captured["receipt"] = self._run_async(
                tab.type_text(
                    args["selector"],
                    args["text"],
                    press_enter=bool(args.get("press_enter", False)),
                )
            )
            self._run_async(asyncio.sleep(_POST_TYPE_DEBOUNCE_S))

        obs = self._observed_mutation(
            browser, tab, mutate, timeout=_SETTLE_INTERACTION_S
        )
        return f"{captured['receipt']}\n\n{obs}"

    def _bh_select(self, browser, args):
        tab = self._get_tab(browser, args)
        captured: dict = {}

        def mutate():
            captured["receipt"] = self._run_async(
                tab.select_option(args["selector"], args["option"])
            )

        obs = self._observed_mutation(
            browser, tab, mutate, timeout=_SETTLE_INTERACTION_S
        )
        return f"{captured['receipt']}\n\n{obs}"

    _BROWSER_DISPATCH = {
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
        "browser_controls": _bh_controls,
        "browser_invoke": _bh_invoke,
    }
