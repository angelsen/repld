"""MCP JSON-RPC tool schemas + dispatch.

Shared by the kernel; the bridge is a dumb byte-pipe and never touches this.
"""

import __main__
import asyncio
import base64
import json
import threading
from typing import Protocol

from .help import INSTRUCTIONS as _INSTRUCTIONS
from .tasks import spill_text as _spill_text

PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "exec",
        "description": (
            "Execute Python in the running kernel. Returns inline if it "
            "finishes within `timeout` seconds; otherwise returns "
            "{task_id, done:false} and the completion arrives as a channel "
            "notification."
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
        "name": "browser_attach",
        "description": (
            "Attach to Chrome tabs matching a URL glob pattern (e.g. '*github.com*'). "
            "Currently-matching tabs attach immediately; future matching tabs auto-attach."
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
            "Evaluate JavaScript in a browser tab. "
            "`target` is the Chrome target_id (from browser_tabs). "
            "Top-level await is auto-detected."
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
            "Query captured network requests for a tab. "
            "Returns a list of request entries (method, url, status, size, time_ms, state)."
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
        "name": "browser_click",
        "description": "Click an element in a tab by CSS selector, dispatching trusted mouse events.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Chrome target_id from browser_tabs",
                },
                "selector": {"type": "string"},
            },
            "required": ["target", "selector"],
        },
    },
    {
        "name": "browser_type",
        "description": "Type text into an element in a tab by CSS selector.",
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
        "description": "Capture a PNG screenshot of a tab. Returns base64-encoded PNG.",
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

RESOURCES = [
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
        server_version: str = "0.0.1",
    ):
        self.ctx = ctx
        self.server_name = server_name
        self.server_version = server_version

    def handle(self, req: dict, session) -> dict | None:
        method = req.get("method")
        rid = req.get("id")
        if method == "initialize":
            return self._initialize(rid)
        if method == "notifications/initialized":
            session.set_initialized()
            return None
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
        if method == "tools/call":
            return self._tools_call(rid, req.get("params", {}))
        if method == "resources/list":
            return {"jsonrpc": "2.0", "id": rid, "result": {"resources": RESOURCES}}
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
                "instructions": _INSTRUCTIONS,
            },
        }

    def _tools_call(self, rid, params: dict) -> dict:
        name = params.get("name")
        args = params.get("arguments", {}) or {}
        if name == "exec":
            return self._exec(rid, args)
        if name == "get_task":
            return self._get_task(rid, args)
        if name == "cancel":
            return self._cancel(rid, args)
        if name and name.startswith("browser_"):
            return self._browser_tool(rid, name, args)
        return _error(rid, -32602, f"unknown tool: {name}")

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
    # Browser tool dispatch
    # ------------------------------------------------------------------

    def _browser_tool(self, rid, name: str, args: dict) -> dict:
        """Dispatch a browser_* tool call."""
        try:
            result = self._browser_dispatch(name, args)
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
        """Route to individual browser tool handler. Returns JSON-serializable result."""
        browser = self._get_browser()

        if name == "browser_attach":
            return {"result": self._run_async(browser.attach(args["pattern"]))}

        if name == "browser_detach":
            return {"result": self._run_async(browser.detach(args.get("pattern")))}

        if name == "browser_tabs":
            tabs = browser.tabs
            return [
                {"target": t.target_id, "url": t.url, "title": t.title} for t in tabs
            ]

        if name == "browser_pages":
            return self._run_async(browser.pages())

        if name == "browser_js":
            tab = browser.find(args["target"])
            ap = args.get("await_promise", "auto")
            result = self._run_async(tab.js(args["code"], await_promise=ap))
            return {"result": result}

        if name == "browser_network":
            tab = browser.find(args["target"])
            rows = tab.network(
                url=args.get("url"),
                method=args.get("method"),
                status=args.get("status"),
                type=args.get("type"),
                include_assets=bool(args.get("include_assets", False)),
            )
            return [repr(r) for r in rows]

        if name == "browser_request":
            tab = browser.find(args["target"])
            return tab.request(args["request_id"])

        if name == "browser_body":
            tab = browser.find(args["target"])
            return tab.body(args["request_id"])

        if name == "browser_click":
            tab = browser.find(args["target"])
            self._run_async(tab.click(args["selector"]))
            return {"result": "ok"}

        if name == "browser_type":
            tab = browser.find(args["target"])
            self._run_async(
                tab.type_text(
                    args["selector"],
                    args["text"],
                    press_enter=bool(args.get("press_enter", False)),
                )
            )
            return {"result": "ok"}

        if name == "browser_console":
            tab = browser.find(args["target"])
            rows = tab.console(
                level=args.get("level"),
                source=args.get("source"),
            )
            return [repr(r) for r in rows]

        if name == "browser_screenshot":
            tab = browser.find(args["target"])

            png_bytes = self._run_async(
                tab.screenshot(full_page=bool(args.get("full_page", False)))
            )
            return {"base64_png": base64.b64encode(png_bytes).decode()}

        if name == "browser_cdp":
            tab = browser.find(args["target"])
            params = args.get("params") or {}
            return self._run_async(tab.cdp(args["method"], **params))

        if name == "browser_clear":
            return {"result": browser.clear(args.get("target"))}

        raise ValueError(f"Unknown browser tool: {name}")

    # ------------------------------------------------------------------
    # Resource dispatch
    # ------------------------------------------------------------------

    def _read_resource(self, rid, params: dict) -> dict:
        uri = params.get("uri", "")
        try:
            if uri == "repld://browser/tabs":
                text = self._resource_tabs()
            elif uri == "repld://browser/network":
                text = self._resource_network()
            elif uri == "repld://browser/console":
                text = self._resource_console()
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


def _error(rid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}
