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
        """Run a coroutine on the repld asyncio loop from the IPC thread."""
        fut = asyncio.run_coroutine_threadsafe(coro, self.ctx.loop)
        return fut.result(timeout=30)

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
        return browser.format_tabs_nested()

    def _bh_pages(self, browser, args):
        return self._run_async(browser.pages())

    def _bh_clear(self, browser, args):
        return browser.clear(args.get("target"))

    def _bh_controls(self, browser, args):
        tab = self._get_tab(browser, args)
        result = self._run_async(tab.controls())
        if result is None:
            return {"controls": None, "message": "No window.controls on this tab"}
        return result

    def _bh_invoke(self, browser, args):
        tab = self._get_tab(browser, args)
        invoke_args = args.get("args")

        def mutate():
            self._run_async(tab.invoke(args["control"], args["action"], invoke_args))

        return self._observed_mutation(browser, tab, mutate, timeout=3.0)

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
        return tab.request(args["request_id"])

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
        if hasattr(browser, "browser_for"):
            return browser.browser_for(tab.target_id)._session
        # Fallback for a plain Browser bound to __main__.browser (no pool).
        return browser._session

    def _bh_tree(self, browser, args):
        from .browser.observe import compose_tree

        tab = self._get_tab(browser, args)
        session = self._session_for(browser, tab)
        lines, _ = self._run_async(compose_tree(tab, session))
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
            from .browser import make_target

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
            timeout=8.0,
        )

    def _bh_open(self, browser, args):
        from .browser.observe import PreObservation, post_observe

        tab = self._run_async(browser.open(args["url"]))
        session = self._session_for(browser, tab)
        key = tab.target_id
        pre = PreObservation(
            iframe_children=[],
            har_snapshots={key: 0},
            console_snapshots={key: 0},
        )
        return self._run_async(
            post_observe(
                tab,
                session,
                pre,
                timeout=8.0,
                extra_header=f"target: {tab.target_id}",
            )
        )

    def _bh_key(self, browser, args):
        tab = self._get_tab(browser, args)
        return self._observed_mutation(
            browser,
            tab,
            lambda: self._run_async(tab.key(args["key"])),
            timeout=5.0,
        )

    def _bh_click(self, browser, args):
        tab = self._get_tab(browser, args)
        return self._observed_mutation(
            browser,
            tab,
            lambda: self._run_async(tab.click(args["selector"])),
            timeout=5.0,
        )

    def _bh_type(self, browser, args):
        tab = self._get_tab(browser, args)

        def mutate():
            self._run_async(
                tab.type_text(
                    args["selector"],
                    args["text"],
                    press_enter=bool(args.get("press_enter", False)),
                )
            )
            self._run_async(asyncio.sleep(0.3))

        return self._observed_mutation(browser, tab, mutate, timeout=5.0)

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
        "browser_controls": _bh_controls,
        "browser_invoke": _bh_invoke,
    }
