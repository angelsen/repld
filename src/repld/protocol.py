"""MCP JSON-RPC tool schemas + dispatch.

Shared by the kernel; the bridge is a dumb byte-pipe and never touches this.
"""

import __main__
import inspect
import json

from .browser_dispatch import BrowserDispatchMixin
from .help import build_instructions as _build_instructions
from .kernel_context import KernelContext
from .tasks import spill_marker, spill_text as _spill_text

PROTOCOL_VERSION = "2024-11-05"

_TARGET_DESC = "Chrome target_id from browser_tabs"

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
        "description": (
            "Detach or disconnect browser targets. Pass 'target' to detach one "
            "tab (unpins it first), 'port' to disconnect an entire Chrome "
            "instance (unpins all its tabs, closes the WebSocket), 'pattern' "
            "to detach by URL glob, or no args to detach everything."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "target": {
                    "type": "string",
                    "description": "Short target ID (e.g. '9222:a1b2c3') — detach one tab",
                },
                "port": {
                    "type": "integer",
                    "description": "Chrome debug port — disconnect the entire browser",
                },
            },
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
                    "description": _TARGET_DESC,
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
                    "description": _TARGET_DESC,
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
                    "description": _TARGET_DESC,
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
                    "description": _TARGET_DESC,
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
                    "description": _TARGET_DESC,
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
            "Returns observation (tree + network + console delta after settle)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": _TARGET_DESC,
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
                    "description": _TARGET_DESC,
                },
            },
            "required": ["target"],
        },
    },
    {
        "name": "browser_fetch",
        "description": (
            "Execute a fetch() in the page's context (inherits cookies/session). "
            "Returns {status, ok, body, base64Encoded}. Binary responses "
            "(invalid UTF-8) are base64-encoded with base64Encoded=true. "
            "Content-Type defaults to application/json for a dict body, "
            "application/x-www-form-urlencoded for a string body — pass "
            "headers to override (e.g. for raw JSON text or plain text)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": _TARGET_DESC,
                },
                "url": {"type": "string"},
                "method": {"type": "string", "default": "GET"},
                "body": {
                    "type": ["object", "string"],
                    "description": (
                        "Request body: dict is JSON-encoded (Content-Type: "
                        "application/json), string is sent as-is (Content-Type: "
                        "application/x-www-form-urlencoded unless overridden)"
                    ),
                },
                "headers": {
                    "type": "object",
                    "description": "Additional headers (overrides the default Content-Type)",
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
                    "description": _TARGET_DESC,
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
                    "description": _TARGET_DESC,
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
                    "description": _TARGET_DESC,
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
        "description": "Capture a PNG screenshot of a tab, resized to the vision API token grid. Returns path + coordinate mapping. Use Read to view. For crisp text, first resize the viewport: browser_cdp(target, method='Emulation.setDeviceMetricsOverride', params={width: 1440, height: 900, deviceScaleFactor: 1, mobile: false}). For mobile: {width: 390, height: 844, deviceScaleFactor: 1, mobile: true}. Reapplying the override on an already-emulated tab can leave viewport metrics inconsistent (clientWidth != innerWidth) — prefer a fresh tab per distinct size, and verify document.documentElement.clientWidth === window.innerWidth before trusting the capture.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": _TARGET_DESC,
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
                    "description": _TARGET_DESC,
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
    {
        "name": "browser_controls",
        "description": (
            "Discover controls exposed by window.controls on a tab. "
            "Returns schema: actions with param types, properties with values, state. "
            "Apps using the controls protocol register named controls (auth, thread, etc.) "
            "with typed actions the agent can invoke."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": _TARGET_DESC,
                },
            },
            "required": ["target"],
        },
    },
    {
        "name": "browser_invoke",
        "description": (
            "Invoke a control action on a tab. Returns {returned, stateBefore, stateAfter, duration}. "
            "Runs the full observation pipeline (settle + tree + network + console delta) after the action."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": _TARGET_DESC,
                },
                "control": {
                    "type": "string",
                    "description": "Control name (e.g. 'auth', 'thread')",
                },
                "action": {
                    "type": "string",
                    "description": "Action name (e.g. 'login', 'goto')",
                },
                "args": {"type": "object", "description": "Action parameters"},
            },
            "required": ["target", "control", "action"],
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
    {
        "uri": "repld://browser/controls",
        "name": "browser-controls",
        "description": "Controls exposed by window.controls on attached tabs — actions with param schemas, properties, state.",
        "mimeType": "application/json",
    },
]

# resources/read returns full text — resources are on-demand pulls, unlike
# exec output. The cap only guards the unbounded producers (browser network/
# console dumps); everything above it falls back to the spill preview.
_RESOURCE_MAX_BYTES = 64 * 1024
_RESOURCE_MIMETYPES = {
    r["uri"]: r["mimeType"] for r in _DOC_RESOURCES + _BROWSER_RESOURCES
}


class Dispatcher(BrowserDispatchMixin):
    def __init__(self, ctx: KernelContext):
        from . import __version__

        self.ctx = ctx
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
                    "name": "repld",
                    "version": self.server_version,
                },
                "instructions": _build_instructions(),
            },
        )

    def _tools_list(self, rid) -> dict:
        from . import gists

        has_browser = _has_browser()
        tools = [
            t for t in TOOLS if has_browser or not t["name"].startswith("browser_")
        ]
        return _response(rid, {"tools": tools + gists.scan_tools()})

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
        assert snap is not None  # task_id was just created by start_task
        if finished:
            text = _format_spill(snap, "(no output)")
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
        tid = args.get("task_id")
        if not tid:
            return _error(rid, -32602, "missing task_id")
        snap = self.ctx.snapshot(tid)
        if snap is None:
            return _error(rid, -32602, f"unknown task_id: {tid}")
        return _response(
            rid,
            {
                "content": [{"type": "text", "text": json.dumps(snap, indent=2)}],
                "_meta": snap,
            },
        )

    def _cancel(self, rid, args: dict) -> dict:
        tid = args.get("task_id")
        if not tid:
            return _error(rid, -32602, "missing task_id")
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
            resolved = gists.resolve_tool(name)
        except AttributeError as exc:
            return _error(rid, -32602, str(exc))
        if resolved is None:
            return _error(rid, -32602, f"unknown tool: {name}")
        handler, old_style = resolved
        try:
            result = handler(args) if old_style else handler(**args)
            if inspect.iscoroutine(result):
                result = self._run_async(result)
            if not isinstance(result, str):
                result = json.dumps(result, indent=2)
            return _response(rid, {"content": [{"type": "text", "text": result}]})
        except Exception as exc:
            return _error(rid, -32000, f"{name}: {exc}")

    # ------------------------------------------------------------------
    # Resource dispatch
    # ------------------------------------------------------------------

    def _resources_list(self, rid) -> dict:
        from . import gists

        resources = list(_DOC_RESOURCES) + (
            list(_BROWSER_RESOURCES) if _has_browser() else []
        )
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

    # Static docs: URI → help.py attribute name (imported lazily at read time)
    _DOC_ATTR_MAP = {
        "repld://docs/guide": "GUIDE",
        "repld://docs/browser": "BROWSER_GUIDE",
        "repld://docs/playbook": "PLAYBOOK",
        "repld://docs/production": "PRODUCTION",
    }

    def _read_resource(self, rid, params: dict) -> dict:
        uri = params.get("uri", "")
        try:
            reader = self._RESOURCE_DISPATCH.get(uri)
            if uri in self._DOC_ATTR_MAP:
                from . import help as _help

                text = getattr(_help, self._DOC_ATTR_MAP[uri])
            elif reader is not None:
                text = reader(self)
            elif uri.startswith("repld://gists/"):
                name = uri.removeprefix("repld://gists/")
                text = self._resource_gist(name)
            else:
                return _error(rid, -32602, f"unknown resource: {uri}")
            if len(text) <= _RESOURCE_MAX_BYTES:
                content = text
                mime = _RESOURCE_MIMETYPES.get(uri, "text/plain")
            else:
                sp = _spill_text(text, label=uri.split("/")[-1])
                # Preview + [full output: …] marker isn't valid JSON anymore.
                content, mime = _format_spill(sp, text), "text/plain"
            return _response(
                rid,
                {"contents": [{"uri": uri, "mimeType": mime, "text": content}]},
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

    def _collect_rows(self, method: str, empty: str) -> str:
        """Concatenate repr'd rows of tab.<method>() across all attached tabs."""
        browser = self._get_browser()
        lines = [repr(r) for tab in browser.tabs for r in getattr(tab, method)()]
        return "\n".join(lines) if lines else empty

    def _resource_network(self) -> str:
        return self._collect_rows("network", "(no network events captured)")

    def _resource_console(self) -> str:
        return self._collect_rows("console", "(no console events captured)")

    def _resource_controls(self) -> str:
        browser = self._get_browser()
        result: dict = {}
        for tab in browser.tabs:
            controls = self._run_async(tab.controls())
            if controls:
                result[tab.target_id] = controls
        if not result:
            return "(no controls found on attached tabs)"
        return json.dumps(result, indent=2)

    def _resource_gist(self, name: str) -> str:
        from . import gists

        return gists.introspect(name)

    def _resource_registry(self) -> str:
        from . import gists

        return gists.registry_summary()

    _RESOURCE_DISPATCH = {
        "repld://browser/tabs": _resource_tabs,
        "repld://browser/network": _resource_network,
        "repld://browser/console": _resource_console,
        "repld://browser/controls": _resource_controls,
        "repld://gists/_registry": _resource_registry,
    }


def _format_spill(sp: dict, fallback: str) -> str:
    """Render a spill_text()/snapshot() dict as tool/resource response text."""
    parts = []
    if sp["text"]:
        parts.append(sp["text"].rstrip())
    if sp["truncated"]:
        parts.append(spill_marker(sp["spill_path"]))
    return "\n".join(parts) or fallback


def _has_browser() -> bool:
    return "browser" in __main__.__dict__


def _response(rid, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _error(rid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}
