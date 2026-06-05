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
            "Evaluate JavaScript in a browser tab. Top-level await is auto-detected."
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
                    "description": "Force promise awaiting (default: auto-detect)",
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
        "description": "Fetch the response body for a captured request by request_id.",
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
                "target": {"type": "string"},
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
                "target": {"type": "string"},
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
                "target": {"type": "string"},
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
                "target": {"type": "string"},
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
        "description": "Capture a PNG screenshot of a tab. Saves to disk, returns path. Use Read to view.",
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
        "description": "Working guide: execution model, browser API, gist patterns, conventions. Read before writing gists.",
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
            return {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {"resourceTemplates": []},
            }
        if method == "resources/read":
            return self._read_resource(rid, req.get("params", {}))
        if rid is None:
            return None
        return _error(rid, -32601, f"method not found: {method}")

    def _initialize(self, rid) -> dict:
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
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
        }

    def _tools_list(self, rid) -> dict:
        from . import gists

        all_tools = list(TOOLS) + gists.scan_tools()
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": all_tools}}

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
            return {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {
                    "content": [{"type": "text", "text": text}],
                    "isError": bool(snap["exception"]),
                    "_meta": {
                        "task_id": task_id,
                        "done": True,
                        "spilled": snap["spilled"],
                        "spill_path": snap["spill_path"],
                    },
                },
            }
        self.ctx.mark_nudged(task_id)
        preview = snap["text"].rstrip()
        msg = f"[task {task_id} still running after {timeout}s; completion will arrive via channel]"
        if preview:
            msg += "\n" + preview
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "content": [{"type": "text", "text": msg}],
                "_meta": {"task_id": task_id, "done": False},
            },
        }

    def _get_task(self, rid, args: dict) -> dict:
        snap = self.ctx.snapshot(args["task_id"])
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "content": [{"type": "text", "text": json.dumps(snap, indent=2)}],
                "_meta": snap,
            },
        }

    def _cancel(self, rid, args: dict) -> dict:
        tid = args["task_id"]
        accepted = self.ctx.cancel_task(tid)
        status = "accepted" if accepted else "no-op"
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "content": [{"type": "text", "text": f"cancel task={tid}: {status}"}],
                "_meta": {"task_id": tid, "cancelled": accepted},
            },
        }

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
            return {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {
                    "content": [{"type": "text", "text": result}],
                },
            }
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
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {"content": [{"type": "text", "text": "\n".join(parts) or text}]},
        }

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

    def _browser_dispatch(self, name: str, args: dict):
        """Route to individual browser tool handler.

        Returns JSON-serializable result, OR a plain str for observation text.
        """
        from .browser.observe import compose_tree, pre_observe, post_observe

        browser = self._get_browser()

        if name == "browser_watch":
            return {"result": self._run_async(browser.watch(args["pattern"]))}

        if name == "browser_detach":
            return {"result": self._run_async(browser.detach(args.get("pattern")))}

        if name == "browser_tabs":
            return browser.format_tabs_nested()

        if name == "browser_pages":
            return self._run_async(browser.pages())

        if name == "browser_js":
            tab = self._run_async(browser.get(args["target"]))
            ap = args.get("await_promise", "auto")
            result = self._run_async(tab.js(args["code"], await_promise=ap))
            return {"result": result}

        if name == "browser_network":
            tab = self._run_async(browser.get(args["target"]))
            rows = tab.network(
                url=args.get("url"),
                method=args.get("method"),
                status=args.get("status"),
                type=args.get("type"),
                include_assets=bool(args.get("include_assets", False)),
            )
            return [repr(r) for r in rows]

        if name == "browser_request":
            tab = self._run_async(browser.get(args["target"]))
            return tab.request(args["request_id"])

        if name == "browser_body":
            tab = self._run_async(browser.get(args["target"]))
            return tab.body(args["request_id"])

        if name == "browser_navigate":
            tab = self._run_async(browser.get(args["target"]))
            if tab.type == "iframe" and not args.get("force"):
                from .browser import make_target

                parent_short = (
                    make_target(tab._port, tab.parent_frame_id)
                    if tab.parent_frame_id
                    else "unknown"
                )
                return {
                    "error": (
                        f"Cannot navigate iframe target {tab.target_id} — "
                        f"this would destroy the embedded app session. "
                        f"Use click/fetch on the iframe for in-app navigation, "
                        f"or navigate the parent ({parent_short}). "
                        f"Pass force=true to override."
                    )
                }
            pre = self._run_async(pre_observe(tab, browser._session))
            self._run_async(tab.navigate(args["url"]))
            return self._run_async(
                post_observe(tab, browser._session, pre, timeout=8.0)
            )

        if name == "browser_open":
            tab = self._run_async(browser.open(args["url"]))
            from .browser.observe import PreObservation

            # New tab — all activity is the delta, so snapshot at 0.
            # Calling pre_observe here would race: by the time it queries
            # MAX(id), early navigation events may already be in DuckDB.
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

        if name == "browser_key":
            tab = self._run_async(browser.get(args["target"]))
            key = args["key"]
            pre = self._run_async(pre_observe(tab, browser._session))
            self._run_async(
                tab.cdp("Input.dispatchKeyEvent", type="keyDown", key=key, code=key)
            )
            self._run_async(
                tab.cdp("Input.dispatchKeyEvent", type="keyUp", key=key, code=key)
            )
            return self._run_async(
                post_observe(tab, browser._session, pre, timeout=5.0)
            )

        if name == "browser_tree":
            tab = self._run_async(browser.get(args["target"]))
            lines, _ = self._run_async(compose_tree(tab, browser._session))
            return "\n".join(lines)

        if name == "browser_fetch":
            tab = self._run_async(browser.get(args["target"]))
            return self._run_async(
                tab.fetch(
                    args["url"],
                    method=args.get("method", "GET"),
                    body=args.get("body"),
                    headers=args.get("headers"),
                )
            )

        if name == "browser_click":
            tab = self._run_async(browser.get(args["target"]))
            pre = self._run_async(pre_observe(tab, browser._session))
            self._run_async(tab.click(args["selector"]))
            return self._run_async(
                post_observe(tab, browser._session, pre, timeout=5.0)
            )

        if name == "browser_type":
            tab = self._run_async(browser.get(args["target"]))
            pre = self._run_async(pre_observe(tab, browser._session))
            self._run_async(
                tab.type_text(
                    args["selector"],
                    args["text"],
                    press_enter=bool(args.get("press_enter", False)),
                )
            )
            # Debounce: wait 300ms after last keystroke before settle check
            self._run_async(asyncio.sleep(0.3))
            return self._run_async(
                post_observe(tab, browser._session, pre, timeout=5.0)
            )

        if name == "browser_console":
            tab = self._run_async(browser.get(args["target"]))
            rows = tab.console(
                level=args.get("level"),
                source=args.get("source"),
            )
            return [repr(r) for r in rows]

        if name == "browser_screenshot":
            tab = self._run_async(browser.get(args["target"]))
            path = self._run_async(
                tab.screenshot(full_page=bool(args.get("full_page", False)))
            )
            return f"Screenshot saved to {path}\nUse Read to view it."

        if name == "browser_cdp":
            tab = self._run_async(browser.get(args["target"]))
            params = args.get("params") or {}
            return self._run_async(tab.cdp(args["method"], **params))

        if name == "browser_clear":
            return {"result": browser.clear(args.get("target"))}

        raise ValueError(f"Unknown browser tool: {name}")

    # ------------------------------------------------------------------
    # Resource dispatch
    # ------------------------------------------------------------------

    def _resources_list(self, rid) -> dict:
        from . import gists

        resources = list(_DOC_RESOURCES) + list(_BROWSER_RESOURCES)
        for name, doc in gists.scan():
            resources.append(
                {
                    "uri": f"repld://gists/{name}",
                    "name": name,
                    "description": doc,
                    "mimeType": "text/plain",
                }
            )
        return {"jsonrpc": "2.0", "id": rid, "result": {"resources": resources}}

    def _read_resource(self, rid, params: dict) -> dict:
        uri = params.get("uri", "")
        try:
            if uri == "repld://docs/guide":
                from .help import GUIDE

                text = GUIDE
            elif uri == "repld://browser/tabs":
                text = self._resource_tabs()
            elif uri == "repld://browser/network":
                text = self._resource_network()
            elif uri == "repld://browser/console":
                text = self._resource_console()
            elif uri.startswith("repld://gists/"):
                name = uri.removeprefix("repld://gists/")
                text = self._resource_gist(name)
            else:
                return _error(rid, -32602, f"unknown resource: {uri}")
            sp = _spill_text(text, label=uri.split("/")[-1])
            content = sp["text"].rstrip() if sp["text"] else text
            if sp["truncated"]:
                content += f"\n[full output: {sp['spill_path']}]"
            return {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {
                    "contents": [
                        {"uri": uri, "mimeType": "text/plain", "text": content}
                    ]
                },
            }
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


def _error(rid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}
