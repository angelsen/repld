"""MCP JSON-RPC tool schemas + dispatch.

Kernel-side only. `bridge.py` is a stateful proxy — it tracks in-flight ids and
replays the client's handshake onto a fresh kernel — but it never interprets a
tool call, so every schema and handler below lives on this side of the socket.
"""

import __main__
import inspect
import json

from .browser_dispatch import BrowserDispatchMixin
from .core_schemas import (
    CAPABILITIES,
    CORE_TOOLS,
    DOC_HELP_ATTRS,
    PROTOCOL_VERSION,
    STATIC_RESOURCES,
    error as _error,
    response as _response,
    wire as _wire,
)
from .help import build_instructions as _build_instructions
from .kernel_context import KernelContext
from .tasks import spill_marker, spill_text as _spill_text

_TARGET_DESC = "Chrome target_id from browser_tabs"

# The `target` property, one object shared by the fifteen browser tools whose
# copy of it was byte-identical. Safe to share because TOOLS is read-only data:
# `_tools_list` filters it into a new list and json-encodes it, and the only
# `inputSchema` mutation in the tree is in `gists.py`, on schemas it builds
# itself for gist tools. Anything that later wants to edit a schema in place
# has to copy first, or it edits all fifteen.
#
# `browser_detach` and `browser_clear` deliberately keep their own literal —
# both say something the shared text can't ("detach one tab", "omit to clear
# all"), which is the distinction worth preserving over the uniformity.
_TARGET_PARAM = {"type": "string", "description": _TARGET_DESC}

TOOLS = [
    *CORE_TOOLS,
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
                "target": _TARGET_PARAM,
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
                "target": _TARGET_PARAM,
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
                "target": _TARGET_PARAM,
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
                "target": _TARGET_PARAM,
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
                "target": _TARGET_PARAM,
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
                "target": _TARGET_PARAM,
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
                "target": _TARGET_PARAM,
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
                "target": _TARGET_PARAM,
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
                "target": _TARGET_PARAM,
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
                "target": _TARGET_PARAM,
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
                "target": _TARGET_PARAM,
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
                "target": _TARGET_PARAM,
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
                "target": _TARGET_PARAM,
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
                "target": _TARGET_PARAM,
            },
            "required": ["target"],
        },
    },
    {
        "name": "browser_invoke",
        "description": (
            "Invoke a control action on a tab. Returns {result, observation} — `result` is "
            "whatever window.controls.invoke() returned (by convention "
            "{returned, stateBefore, stateAfter, duration}), `observation` is the full "
            "observation pipeline (settle + tree + network + console delta) after the action."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": _TARGET_PARAM,
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
    r["uri"]: r["mimeType"] for r in STATIC_RESOURCES + _BROWSER_RESOURCES
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
            return self._tools_call(rid, req.get("params", {}), session)
        if method == "resources/list":
            return self._resources_list(rid)
        if method == "resources/templates/list":
            return _response(rid, {"resourceTemplates": []})
        if method == "resources/read":
            return self._read_resource(rid, req.get("params", {}))
        # Human gates. Deliberately JSON-RPC methods and *not* MCP tools: they
        # exist so a human can unblock a cell, and an agent that could answer
        # its own `confirm()` would defeat the primitive. `repld gate` is the
        # only caller; no MCP client is ever told these exist.
        if method == "gates/list":
            from . import gates

            return _response(rid, {"gates": gates.open_gates()})
        if method == "gates/resolve":
            return self._gates_resolve(rid, req.get("params", {}))
        if rid is None:
            return None
        return _error(rid, -32601, f"method not found: {method}")

    def _gates_resolve(self, rid, params: dict) -> dict:
        """Answer one pending gate, coercing the value the way its kind needs.

        Coercion happens here rather than in the CLI so the kernel stays the
        authority on what a gate accepts — the caller only knows the string a
        human typed, and `open_gates` is what says whether it's a confirm.
        """
        from . import gates

        gate_id = params.get("gate_id")
        if not gate_id:
            return _error(rid, -32602, "missing gate_id")
        pending = {g["gate_id"]: g for g in gates.open_gates()}
        gate = pending.get(gate_id)
        if gate is None:
            return _error(rid, -32602, f"no gate awaiting an answer: {gate_id}")
        if "value" not in params:
            return _error(rid, -32602, "missing value")
        try:
            value = gates.parse_response(
                gate["kind"], str(params["value"]), gate["options"]
            )
        except ValueError as exc:
            return _error(rid, -32602, str(exc))
        if not gates.resolve_gate(gate_id, value):
            # Raced: a pane or a browser pill answered between the lookup and
            # here. The cell is unblocked either way, which is what was wanted.
            return _error(rid, -32602, f"gate {gate_id} was already answered")
        return _response(rid, {"gate_id": gate_id, "value": value})

    def _initialize(self, rid) -> dict:
        return _response(
            rid,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": CAPABILITIES,
                "serverInfo": {
                    "name": "repld",
                    "version": self.server_version,
                },
                "instructions": _build_instructions(),
            },
        )

    def _tools_list(self, rid) -> dict:
        return _response(rid, {"tools": _compute_tools()})

    def _tools_call(self, rid, params: dict, session=None) -> dict:
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "exec":
            return self._exec(rid, args, session)
        if name == "get_task":
            return self._get_task(rid, args)
        if name == "cancel":
            return self._cancel(rid, args)
        if not name:
            return _error(rid, -32602, "missing tool name")
        if name in _bridge_tool_names():
            # We advertise these but never serve them — the bridge answers them
            # on the way in. Arriving here means the client is talking to the
            # socket directly, so say that rather than reporting "unknown tool"
            # for a name that is right there in tools/list.
            return _error(
                rid,
                -32601,
                f"{name} is served by `repld bridge`, not the kernel — this "
                "connection bypassed it",
            )
        # Everything past here answers inline off live state, with no deferral
        # to fall back on — so unlike `exec` above (which gets its task_id
        # immediately and waits inside `_run_cell`), these have to wait for the
        # project bootstrap here or risk running against a bare `__main__`.
        # `tools/call` is itself what lazily spawns a kernel, and the socket
        # binds before `repld_init.py` runs, so that window is the norm rather
        # than a race.
        self.ctx.wait_ready()
        if name.startswith("browser_"):
            return self._browser_tool(rid, name, args)
        return self._gist_tool(rid, name, args)

    def _exec(self, rid, args: dict, session=None) -> dict:
        src = args.get("code", "")
        timeout = float(args.get("timeout", 2.0))
        # origin=session: if this cell outruns `timeout`, its completion push
        # goes back to the caller alone, not to every connected session.
        task_id, done_event = self.ctx.start_task(src, origin=session)
        finished = done_event.wait(timeout=timeout)
        snap = self.ctx.snapshot(task_id)
        assert snap is not None  # task_id was just created by start_task
        # `mark_nudged` is what promises the completion push, and it refuses
        # when the cell finished first — including in the window this very
        # snapshot() opens, which for a multi-megabyte spill is milliseconds
        # wide. Taking its word for it is the difference between answering
        # with the result and telling the client to wait for a channel
        # notification nobody is going to send.
        if not finished:
            finished = not self.ctx.mark_nudged(task_id)
            if finished:
                snap = self.ctx.snapshot(task_id)
                assert snap is not None
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
            handler = gists.resolve_tool(name)
        except AttributeError as exc:
            return _error(rid, -32602, str(exc))
        if handler is None:
            return _error(rid, -32602, f"unknown tool: {name}")
        try:
            result = handler(**args)
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
        return _response(rid, {"resources": _compute_resources()})

    def _read_resource(self, rid, params: dict) -> dict:
        uri = params.get("uri", "")
        try:
            # Static docs first: `core_schemas.DOC_HELP_ATTRS` is the one map,
            # shared with the bridge's cache-less fallback. help.py is imported
            # here rather than at module scope so it stays a read-time cost.
            attr = DOC_HELP_ATTRS.get(uri)
            reader = self._RESOURCE_DISPATCH.get(uri)
            if attr is not None:
                from . import help as _help

                text = getattr(_help, attr)
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
                # The fallback is a marker, not `text`: it only fires if the
                # spill came back with neither preview nor truncation flag,
                # and emitting the whole >64KB payload there would be the one
                # thing _RESOURCE_MAX_BYTES exists to prevent.
                content, mime = _format_spill(sp, "(empty resource)"), "text/plain"
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


def _bridge_tool_names() -> frozenset[str]:
    from . import bridge_tools

    return frozenset(bridge_tools.BRIDGE_TOOLS)


def _compute_tools() -> list[dict]:
    from . import bridge_tools, gists

    has_browser = _has_browser()
    tools = [t for t in TOOLS if has_browser or not t["name"].startswith("browser_")]
    # bridge_tools.SCHEMAS is advertised from here rather than injected into
    # this response by the bridge: the bridge relays raw lines untouched in
    # both directions, and parsing + re-serializing every tools/list reply
    # just to append them would cost it that property. The bridge intercepts
    # the matching tools/call on the way in instead.
    return tools + bridge_tools.SCHEMAS + gists.scan_tools()


def _compute_resources() -> list[dict]:
    from . import gists

    resources = _wire(STATIC_RESOURCES) + (
        list(_BROWSER_RESOURCES) if _has_browser() else []
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
    return resources


def build_discovery_cache() -> dict:
    """Compute the full initialize/tools/resources triple for `kernel.cache`.

    Same composition `_initialize`/`_tools_list`/`_resources_list` produce —
    called once at boot so the bridge can answer MCP discovery methods without
    a live kernel (see `kernel._write_cache`).
    """
    from . import __version__

    return {
        "protocolVersion": PROTOCOL_VERSION,
        "version": __version__,
        "instructions": _build_instructions(),
        "tools": _compute_tools(),
        "resources": _compute_resources(),
    }


# `_response` / `_error` are imported from core_schemas at the top of this
# module: the bridge builds the same envelopes and cannot import this one.
