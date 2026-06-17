"""MCP JSON-RPC tool schemas + dispatch.

Shared by the kernel; the bridge is a dumb byte-pipe and never touches this.
"""

import __main__
import asyncio
import inspect
import json
import threading
from typing import Protocol

from .help import build_instructions as _build_instructions
from .tasks import spill_text as _spill_text

PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "exec",
        "description": (
            "Run Python in shared __main__. Returns inline within timeout; "
            "otherwise {task_id, done:false} with channel push on completion. "
            "Use defer() for background work that should outlive the response."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "timeout": {"type": "number", "default": 2.0},
            },
            "required": ["code"],
        },
    },
    {
        "name": "get_task",
        "description": (
            "Fetch current status and a head+tail preview of a task's output. "
            "Use Read on the returned `spill_path` for full content."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "cancel",
        "description": (
            "Attempt to cancel a running task. Returns whether cancellation "
            "was accepted. Cannot preempt tight sync loops (`while True: pass`) "
            "— only await-yielding code is cancellable."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "browser_watch",
        "description": (
            "Watch Chrome tabs matching a URL glob pattern (e.g. '*github.com*'). "
            "Currently-matching tabs attach immediately; future matching tabs auto-attach. "
            "Watched tabs are lightweight (events only, no body capture). "
            "Use browser.get() for a tab with full body capture, or opt in per tab with tab.capture_bodies = True. "
            "Gists call this in connect() to establish persistent tab access."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"pattern": {"type": "string"}},
            "required": ["pattern"],
        },
    },
    {
        "name": "browser_detach",
        "description": "Detach tabs matching a URL glob pattern, or all tabs if no pattern given.",
        "inputSchema": {
            "type": "object",
            "properties": {"pattern": {"type": "string"}},
            "required": [],
        },
    },
    {
        "name": "browser_tabs",
        "description": "List currently attached browser tabs with their target_id, url, and title.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "browser_pages",
        "description": "List all Chrome targets (attached or not), including their type and URL.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "browser_js",
        "description": (
            "Evaluate JavaScript in a browser tab. Top-level await works "
            "(REPL semantics, like the DevTools console); promise results are awaited."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Chrome target_id from browser_tabs",
                },
                "code": {
                    "type": "string",
                    "description": "JavaScript expression to evaluate",
                },
                "await_promise": {
                    "type": "boolean",
                    "description": "Set false to return without awaiting a promise result",
                },
            },
            "required": ["target", "code"],
        },
    },
    {
        "name": "browser_network",
        "description": (
            "Query captured network requests. Returns compact list. "
            "Use browser_request for headers/postData."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Chrome target_id from browser_tabs",
                },
                "url": {"type": "string", "description": "URL substring filter"},
                "method": {
                    "type": "string",
                    "description": "HTTP method filter (GET, POST, ...)",
                },
                "status": {"type": "integer", "description": "HTTP status code filter"},
                "type": {"type": "string", "description": "Resource type filter"},
                "include_assets": {"type": "boolean", "default": False},
            },
            "required": ["target"],
        },
    },
    {
        "name": "browser_request",
        "description": (
            "Inspect a captured request by request_id. Returns full HAR entry "
            "with request/response headers, postData, auth scheme, timing — "
            "everything except the response body (use browser_body for that)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Chrome target_id from browser_tabs",
                },
                "request_id": {"type": "string"},
            },
            "required": ["target", "request_id"],
        },
    },
    {
        "name": "browser_body",
        "description": "Fetch the response body for a request by request_id. Works on any attached tab (uses Network.getResponseBody on demand; pre-captured in DuckDB on get/open tabs).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Chrome target_id from browser_tabs",
                },
                "request_id": {"type": "string"},
            },
            "required": ["target", "request_id"],
        },
    },
    {
        "name": "browser_navigate",
        "description": (
            "Navigate a tab to a URL. Returns observation (tree + network + console delta). "
            "Blocked on iframe targets (would destroy embedded app session) — use click/fetch instead. "
            "Pass force=true to override."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Chrome target_id from browser_tabs",
                },
                "url": {"type": "string"},
                "force": {
                    "type": "boolean",
                    "default": False,
                    "description": "Override iframe navigation block",
                },
            },
            "required": ["target", "url"],
        },
    },
    {
        "name": "browser_open",
        "description": (
            "Open new tab and navigate. "
            "Returns observation with target: header for the new tab ID."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "browser_key",
        "description": (
            "Send a key press (Enter, Escape, Tab, ArrowDown, etc). "
            "Returns accessibility tree + network/console delta."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Chrome target_id from browser_tabs",
                },
                "key": {
                    "type": "string",
                    "description": "Key name: Enter, Escape, Tab, ArrowDown, etc.",
                },
            },
            "required": ["target", "key"],
        },
    },
    {
        "name": "browser_tree",
        "description": (
            "Get the page's accessibility tree as compact text. "
            "Crosses iframe boundaries for attached child targets."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Chrome target_id from browser_tabs",
                },
            },
            "required": ["target"],
        },
    },
    {
        "name": "browser_fetch",
        "description": (
            "Execute a fetch() in the page's context (inherits cookies/session). "
            "Returns {status, ok, body}."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Chrome target_id from browser_tabs",
                },
                "url": {"type": "string"},
                "method": {"type": "string", "default": "GET"},
                "body": {
                    "description": "Request body (dict for JSON, string for raw)",
                },
                "headers": {
                    "type": "object",
                    "description": "Additional headers",
                },
            },
            "required": ["target", "url"],
        },
    },
    {
        "name": "browser_click",
        "description": (
            "Click element. Auto-waits 2s. "
            "Returns observation (tree + network + console delta after settle)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Chrome target_id from browser_tabs",
                },
                "selector": {
                    "type": "string",
                    "description": "CSS, text=Label, role=button[name='OK'], label=Name, or tag:has-text('...')",
                },
            },
            "required": ["target", "selector"],
        },
    },
    {
        "name": "browser_type",
        "description": (
            "Clear field and type text. Auto-waits 2s. "
            "Returns observation (tree + network + console delta after settle)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Chrome target_id from browser_tabs",
                },
                "selector": {"type": "string"},
                "text": {"type": "string"},
                "press_enter": {"type": "boolean", "default": False},
            },
            "required": ["target", "selector", "text"],
        },
    },
    {
        "name": "browser_console",
        "description": "Query captured console messages for a tab (console.log, errors, exceptions).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Chrome target_id from browser_tabs",
                },
                "level": {
                    "type": "string",
                    "description": "Filter by level: log, info, warning, error",
                },
                "source": {"type": "string"},
            },
            "required": ["target"],
        },
    },
    {
        "name": "browser_screenshot",
        "description": "Capture a JPEG screenshot of a tab, pre-sized for the vision API. Saves to disk, returns path. Use Read to view.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Chrome target_id from browser_tabs",
                },
                "full_page": {"type": "boolean", "default": False},
            },
            "required": ["target"],
        },
    },
    {
        "name": "browser_cdp",
        "description": "Raw CDP passthrough. Execute any CDP method on a tab.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Chrome target_id from browser_tabs",
                },
                "method": {
                    "type": "string",
                    "description": "CDP method, e.g. 'Page.navigate'",
                },
                "params": {"type": "object", "description": "CDP params dict"},
            },
            "required": ["target", "method"],
        },
    },
    {
        "name": "browser_clear",
        "description": "Clear captured network and console events. Specify target for one tab, or omit to clear all.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Chrome target_id from browser_tabs (omit to clear all)",
                },
            },
            "required": [],
        },
    },
]

_DOC_RESOURCES = [
    {
        "uri": "repld://docs/guide",
        "name": "repld-guide",
        "description": "Working guide: execution model, gist patterns, conventions. Read before writing gists.",
        "mimeType": "text/plain",
    },
    {
        "uri": "repld://docs/browser",
        "name": "repld-browser",
        "description": "Browser API reference, internals (capture, settle, selectors, session recovery), and workflow patterns.",
        "mimeType": "text/plain",
    },
    {
        "uri": "repld://docs/playbook",
        "name": "repld-playbook",
        "description": "Workflow methodology: prototype interactive → extract gists → wire triggers → production. Read before designing automation.",
        "mimeType": "text/plain",
    },
    {
        "uri": "repld://docs/production",
        "name": "repld-production",
        "description": "Graduation guide: move gists to FastMCP or FastAPI with the two-layer pattern, .env secrets, and concrete wiring examples.",
        "mimeType": "text/plain",
    },
]

_BROWSER_RESOURCES = [
    {
        "uri": "repld://browser/tabs",
        "name": "browser-tabs",
        "description": "Currently attached browser tabs with target IDs, URLs, and titles.",
        "mimeType": "text/plain",
    },
    {
        "uri": "repld://browser/network",
        "name": "browser-network",
        "description": "Network requests captured across all attached browser tabs.",
        "mimeType": "text/plain",
    },
    {
        "uri": "repld://browser/console",
        "name": "browser-console",
        "description": "Console messages captured across all attached browser tabs.",
        "mimeType": "text/plain",
    },
]


class KernelContext(Protocol):
    loop: asyncio.AbstractEventLoop

    def start_task(self, src: str) -> tuple[str, threading.Event]: ...
    def snapshot(self, task_id: str) -> dict: ...
    def mark_nudged(self, task_id: str) -> None: ...
    def cancel_task(self, task_id: str) -> bool: ...


class Dispatcher:
    def __init__(
        self,
        ctx: KernelContext,
        *,
        server_name: str = "repld",
    ):
        from . import __version__

        self.ctx = ctx
        self.server_name = server_name
        self.server_version = __version__

    def handle(self, req: dict, session) -> dict | None:
        method = req.get("method")
        rid = req.get("id")
        if method == "initialize":
            return self._initialize(rid)
        if method == "notifications/initialized":
            session.set_initialized()
            return None
        if method == "tools/list":
            return self._tools_list(rid)
        if method == "tools/call":
            return self._tools_call(rid, req.get("params", {}))
        if method == "resources/list":
            return self._resources_list(rid)
        if method == "resources/templates/list":
            return _response(rid, {"resourceTemplates": []})
        if method == "resources/read":
            return self._read_resource(rid, req.get("params", {}))
        if rid is None:
            return None
        return _error(rid, -32601, f"method not found: {method}")

    def _initialize(self, rid) -> dict:
        return _response(
            rid,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {},
                    "resources": {},
                    "experimental": {
                        "claude/channel": {},
                        "claude/channel/permission": {},
                    },
                },
                "serverInfo": {
                    "name": self.server_name,
                    "version": self.server_version,
                },
                "instructions": _build_instructions(),
            },
        )

    def _tools_list(self, rid) -> dict:
        from . import gists

        all_tools = list(TOOLS) + gists.scan_tools()
        return _response(rid, {"tools": all_tools})

    def _tools_call(self, rid, params: dict) -> dict:
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "exec":
            return self._exec(rid, args)
        if name == "get_task":
            return self._get_task(rid, args)
        if name == "cancel":
            return self._cancel(rid, args)
        if name and name.startswith("browser_"):
            return self._browser_tool(rid, name, args)
        if not name:
            return _error(rid, -32602, "missing tool name")
        return self._gist_tool(rid, name, args)

    def _exec(self, rid, args: dict) -> dict:
        src = args.get("code", "")
        timeout = float(args.get("timeout", 2.0))
        task_id, done_event = self.ctx.start_task(src)
        finished = done_event.wait(timeout=timeout)
        snap = self.ctx.snapshot(task_id)
        if finished:
            parts = []
            if snap["text"]:
                parts.append(snap["text"].rstrip())
            if snap["truncated"]:
                parts.append(f"[full output: {snap['spill_path']}]")
            text = "\n".join(parts) or "(no output)"
            return _response(
                rid,
                {
                    "content": [{"type": "text", "text": text}],
                    "isError": bool(snap["exception"]),
                    "_meta": {
                        "task_id": task_id,
                        "done": True,
                        "spilled": snap["spilled"],
                        "spill_path": snap["spill_path"],
                    },
                },
            )
        self.ctx.mark_nudged(task_id)
        preview = snap["text"].rstrip()
        msg = f"[task {task_id} still running after {timeout}s; completion will arrive via channel]"
        if preview:
            msg += "\n" + preview
        return _response(
            rid,
            {
                "content": [{"type": "text", "text": msg}],
                "_meta": {"task_id": task_id, "done": False},
            },
        )

    def _get_task(self, rid, args: dict) -> dict:
        snap = self.ctx.snapshot(args["task_id"])
        return _response(
            rid,
            {
                "content": [{"type": "text", "text": json.dumps(snap, indent=2)}],
                "_meta": snap,
            },
        )

    def _cancel(self, rid, args: dict) -> dict:
        tid = args["task_id"]
        accepted = self.ctx.cancel_task(tid)
        status = "accepted" if accepted else "no-op"
        return _response(
            rid,
            {
                "content": [{"type": "text", "text": f"cancel task={tid}: {status}"}],
                "_meta": {"task_id": tid, "cancelled": accepted},
            },
        )

    # ------------------------------------------------------------------
    # Gist tool dispatch
    # ------------------------------------------------------------------

    def _gist_tool(self, rid, name: str, args: dict) -> dict:
        """Dispatch to a gist-registered tool handler.

        Handlers return str or JSON-serializable data.  No spill pipeline —
        the handler controls output size.
        """
        from . import gists

        try:
            handler = gists.resolve_tool(name)
        except AttributeError as exc:
            return _error(rid, -32602, str(exc))
        if handler is None:
            return _error(rid, -32602, f"unknown tool: {name}")
        try:
            result = handler(args)
            if inspect.iscoroutine(result):
                result = self._run_async(result)
            if not isinstance(result, str):
                result = json.dumps(result, indent=2)
            return _response(rid, {"content": [{"type": "text", "text": result}]})
        except Exception as exc:
            return _error(rid, -32000, f"{name}: {exc}")

    # ------------------------------------------------------------------
    # Browser tool dispatch
    # ------------------------------------------------------------------

    def _browser_tool(self, rid, name: str, args: dict) -> dict:
        """Dispatch a browser_* tool call."""
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
        sp = _spill_text(text, label=label)
        parts = []
        if sp["text"]:
            parts.append(sp["text"].rstrip())
        if sp["truncated"]:
            parts.append(f"[full output: {sp['spill_path']}]")
        return _response(
            rid, {"content": [{"type": "text", "text": "\n".join(parts) or text}]}
        )

    def _get_browser(self):
        """Retrieve the browser object from __main__; raise if not available."""
        browser = __main__.__dict__.get("browser")
        if browser is None:
            raise RuntimeError(
                "browser builtin not available — kernel not running or browser extra not installed"
            )
        return browser

    def _run_async(self, coro, timeout: float = 30):
        """Run a coroutine on the repld asyncio loop from the IPC thread."""
        try:
            loop = self.ctx.loop  # type: ignore[attr-defined]
        except AttributeError:
            raise RuntimeError("KernelContext does not expose .loop")
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        return fut.result(timeout=timeout)

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
        return {"result": self._run_async(browser.watch(args["pattern"]))}

    def _bh_detach(self, browser, args):
        return {"result": self._run_async(browser.detach(args.get("pattern")))}

    def _bh_tabs(self, browser, args):
        return browser.format_tabs_nested()

    def _bh_pages(self, browser, args):
        return self._run_async(browser.pages())

    def _bh_clear(self, browser, args):
        return {"result": browser.clear(args.get("target"))}

    # ------------------------------------------------------------------
    # Browser handlers — tab read-only
    # ------------------------------------------------------------------

    def _bh_js(self, browser, args):
        tab = self._get_tab(browser, args)
        ap = args.get("await_promise", "auto")
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
            f"Captured: {src['width']}x{src['height']} ({info['bytes'] // 1024}KB JPEG)  →  Model sees: {mdl['width']}x{mdl['height']} (API resizes server-side)",
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

    def _bh_tree(self, browser, args):
        from .browser.observe import compose_tree

        tab = self._get_tab(browser, args)
        lines, _ = self._run_async(compose_tree(tab, browser._session))
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Browser handlers — tab mutations (with observe)
    # ------------------------------------------------------------------

    def _observed_mutation(self, browser, tab, mutate, *, timeout: float):
        """Run pre_observe → mutate() → post_observe around a tab mutation."""
        from .browser.observe import post_observe, pre_observe

        pre = self._run_async(pre_observe(tab, browser._session))
        mutate()
        return self._run_async(
            post_observe(tab, browser._session, pre, timeout=timeout)
        )

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
        # New tab — all activity is the delta, so snapshot at 0.
        key = tab.target_id
        pre = PreObservation(
            iframe_children=[],
            har_snapshots={key: 0},
            console_snapshots={key: 0},
        )
        return self._run_async(
            post_observe(
                tab,
                browser._session,
                pre,
                timeout=8.0,
                extra_header=f"target: {tab.target_id}",
            )
        )

    def _bh_key(self, browser, args):
        tab = self._get_tab(browser, args)
        key = args["key"]

        def mutate():
            self._run_async(
                tab.cdp("Input.dispatchKeyEvent", type="keyDown", key=key, code=key)
            )
            self._run_async(
                tab.cdp("Input.dispatchKeyEvent", type="keyUp", key=key, code=key)
            )

        return self._observed_mutation(browser, tab, mutate, timeout=5.0)

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
    }

    # ------------------------------------------------------------------
    # Resource dispatch
    # ------------------------------------------------------------------

    def _resources_list(self, rid) -> dict:
        from . import gists

        resources = list(_DOC_RESOURCES) + list(_BROWSER_RESOURCES)
        resources.append(
            {
                "uri": "repld://gists/_registry",
                "name": "gist-registry",
                "description": "Every gist seen across projects; link one in with `repld gist add`.",
                "mimeType": "text/plain",
            }
        )
        for name, doc in gists.scan():
            resources.append(
                {
                    "uri": f"repld://gists/{name}",
                    "name": name,
                    "description": doc,
                    "mimeType": "text/plain",
                }
            )
        return _response(rid, {"resources": resources})

    def _read_resource(self, rid, params: dict) -> dict:
        uri = params.get("uri", "")
        try:
            if uri == "repld://docs/guide":
                from .help import GUIDE

                text = GUIDE
            elif uri == "repld://docs/browser":
                from .help import BROWSER_GUIDE

                text = BROWSER_GUIDE
            elif uri == "repld://docs/playbook":
                from .help import PLAYBOOK

                text = PLAYBOOK
            elif uri == "repld://docs/production":
                from .help import PRODUCTION

                text = PRODUCTION
            elif uri == "repld://browser/tabs":
                text = self._resource_tabs()
            elif uri == "repld://browser/network":
                text = self._resource_network()
            elif uri == "repld://browser/console":
                text = self._resource_console()
            elif uri == "repld://gists/_registry":
                from . import gists

                text = gists.registry_summary()
            elif uri.startswith("repld://gists/"):
                name = uri.removeprefix("repld://gists/")
                text = self._resource_gist(name)
            else:
                return _error(rid, -32602, f"unknown resource: {uri}")
            sp = _spill_text(text, label=uri.split("/")[-1])
            content = sp["text"].rstrip() if sp["text"] else text
            if sp["truncated"]:
                content += f"\n[full output: {sp['spill_path']}]"
            return _response(
                rid,
                {"contents": [{"uri": uri, "mimeType": "text/plain", "text": content}]},
            )
        except Exception as exc:
            return _error(rid, -32000, f"resource read: {exc}")

    def _resource_tabs(self) -> str:
        browser = self._get_browser()
        tabs = browser.tabs
        if not tabs:
            return "(no tabs attached)"
        lines: list[str] = []
        for t in tabs:
            lines.append(f"{t.target_id}  {t.url}  {t.title}")
        return "\n".join(lines)

    def _resource_network(self) -> str:
        browser = self._get_browser()
        lines: list[str] = []
        for tab in browser.tabs:
            rows = tab.network()
            for r in rows:
                lines.append(repr(r))
        return "\n".join(lines) if lines else "(no network events captured)"

    def _resource_console(self) -> str:
        browser = self._get_browser()
        lines: list[str] = []
        for tab in browser.tabs:
            rows = tab.console()
            for r in rows:
                lines.append(repr(r))
        return "\n".join(lines) if lines else "(no console events captured)"

    def _resource_gist(self, name: str) -> str:
        from . import gists

        return gists.introspect(name)


def _response(rid, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _error(rid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}
