"""Protocol-surface data shared by the kernel and the bridge.

Pure data, no imports beyond stdlib. `protocol.py` (kernel-side) composes
these into its full `tools/list` / `resources/list` responses; `bridge.py`
uses the same dicts as its fallback when no kernel has ever run in this
project and no `kernel.cache` exists yet to answer from. Keeping one copy
here is what lets both sides agree on the schema without the bridge
importing `protocol.py` and pulling in the kernel's dependency chain.

`DOC_RESOURCES` entries carry a `_help_attr` alongside the wire fields: both
sides serve the same four docs out of `help.py`, so riding the attribute name
on the entry keeps adding a fifth doc to one edit instead of three across three
modules. It is stripped before the dict goes on the wire (`wire()`).
"""

# The MCP revisions repld speaks. Here rather than in `protocol.py` because
# three parties answer `initialize` and only one can import it: the kernel
# (`protocol._initialize`), the bridge when no kernel has ever run in this
# project (`bridge._try_bridge_intercept`), and `repld exec`, which handshakes
# over the socket itself. Importing `protocol.py` from the other two would drag
# in browser_dispatch/help/kernel_context/tasks for one string, so the
# alternative is a hand-synced copy in each — a drift no test can see, since
# every side would still be internally consistent.
#
# 2025-06-18 is the ceiling on purpose: 2025-11-25 adds nothing repld serves
# (icons, URL elicitation, experimental tasks), and 2026-07-28 removes the
# `initialize` handshake outright — adopting it is a bridge rearchitecture,
# not a version bump. Everything repld uses from 2025-06-18 (tool annotations,
# structuredContent/outputSchema, resource templates) is additive, which is
# what makes serving the older revisions from the same code correct.
PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")


def negotiate_version(requested) -> str:
    """The spec's negotiation rule (lifecycle.mdx): echo the client's requested
    version when we support it, otherwise answer our latest and let the client
    decide whether to proceed. Never echo an *unknown* string back — that would
    promise a revision nobody implements."""
    return requested if requested in SUPPORTED_VERSIONS else PROTOCOL_VERSION


# What `initialize` negotiates, from either side of the socket. Shared for the
# same reason the tool schemas are: the bridge answers `initialize` itself when
# no kernel has ever run here, so a capability declared only in `protocol.py`
# would be silently missing from exactly the sessions that start cold — and
# `build_discovery_cache()` does not carry capabilities, so not even a warm
# `kernel.cache` could correct for it. Both parties may only use what was
# negotiated, so a drifted copy disables the feature rather than degrading it:
# `listChanged` is what lets the bridge invalidate the client's tool list after
# respawning a kernel.
CAPABILITIES = {
    "tools": {"listChanged": True},
    "resources": {"listChanged": True},
    "experimental": {
        "claude/channel": {},
        "claude/channel/permission": {},
    },
}

CORE_TOOLS = [
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
        "annotations": {"openWorldHint": True},
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
        # Declaring outputSchema commits `_get_task` to sending
        # structuredContent conforming to it on every success — the shape is
        # tasks.snapshot(), so a field added there must be added here.
        "outputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "text": {
                    "type": "string",
                    "description": "head+tail preview of the task's output",
                },
                "truncated": {"type": "boolean"},
                "spilled": {"type": "boolean"},
                "spill_path": {"type": ["string", "null"]},
                "exception": {"type": ["string", "null"]},
                "result": {
                    "type": ["string", "null"],
                    "description": "bounded repr of the awaited value, when one exists",
                },
                "done": {"type": "boolean"},
                "label": {"type": ["string", "null"]},
            },
            "required": [
                "task_id",
                "text",
                "truncated",
                "spilled",
                "spill_path",
                "exception",
                "result",
                "done",
                "label",
            ],
        },
        "annotations": {"readOnlyHint": True},
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
        "annotations": {"idempotentHint": True},
    },
]

DOC_RESOURCES = [
    {
        "uri": "repld://docs/guide",
        "name": "repld-guide",
        "description": "Working guide: execution model, gist patterns, conventions. Read before writing gists.",
        "mimeType": "text/plain",
        "_help_attr": "GUIDE",
    },
    {
        "uri": "repld://docs/browser",
        "name": "repld-browser",
        "description": "Browser API reference, internals (capture, settle, selectors, session recovery), and workflow patterns.",
        "mimeType": "text/plain",
        "_help_attr": "BROWSER_GUIDE",
    },
    {
        "uri": "repld://docs/playbook",
        "name": "repld-playbook",
        "description": "Workflow methodology: prototype interactive → extract gists → wire triggers → production. Read before designing automation.",
        "mimeType": "text/plain",
        "_help_attr": "PLAYBOOK",
    },
    {
        "uri": "repld://docs/production",
        "name": "repld-production",
        "description": "Graduation guide: move gists to FastMCP or FastAPI with the two-layer pattern, .env secrets, and concrete wiring examples.",
        "mimeType": "text/plain",
        "_help_attr": "PRODUCTION",
    },
]

# URI → name of the `help.py` module attribute holding that doc's text. Both
# the kernel (`protocol._read_resource`) and the bridge (`bridge._static_docs`)
# getattr through this; neither imports the constants eagerly, since a bridge
# that never serves a doc shouldn't pay to parse help.py.
DOC_HELP_ATTRS = {r["uri"]: r["_help_attr"] for r in DOC_RESOURCES}

# The cross-project gist registry. Advertised unconditionally by both sides,
# but *not* a doc: there is no help.py attribute behind it (the kernel renders
# it from the registry file), so it sits beside DOC_RESOURCES rather than in
# it, which would put a bogus entry in DOC_HELP_ATTRS. It belongs in this
# module for the reason everything else here does — `resources/list` has two
# authors, and this was declared only in `protocol.py`, so a session starting
# with no kernel and no cache was never told the resource existed.
REGISTRY_RESOURCE = {
    "uri": "repld://gists/_registry",
    "name": "gist-registry",
    "description": "Every gist seen across projects; link one in with `repld gist add`.",
    "mimeType": "text/plain",
}

# Every resource both sides advertise unconditionally. What the bridge serves
# as its cold fallback, and what the kernel builds its live list on top of.
STATIC_RESOURCES = [*DOC_RESOURCES, REGISTRY_RESOURCE]

# `resources/templates/list` has the same two authors as everything else here.
# Loaded gists are still enumerated concretely in `resources/list` (a client
# that never expands templates keeps working); the template is what tells a
# cold session the URI *shape* exists before any kernel has listed a gist.
RESOURCE_TEMPLATES = [
    {
        "uriTemplate": "repld://gists/{name}",
        "name": "gist-api",
        "description": (
            "Introspected API of a loaded gist: constructor signatures, "
            "methods, docstrings. `name` is the gist's module name."
        ),
        "mimeType": "text/plain",
    }
]


def wire(resources: list[dict]) -> list[dict]:
    """Strip internal `_`-prefixed keys so a resource dict is MCP-clean."""
    return [{k: v for k, v in r.items() if not k.startswith("_")} for r in resources]


# The JSON-RPC envelope, for the same reason everything else here is shared:
# more than one module writes these. `protocol.py` re-exports both (the kernel
# and `dashboard._handle_api` import them from there), and `bridge.py` — which
# deliberately cannot import `protocol`, since that drags in
# browser_dispatch/help/kernel_context/tasks for a two-line dict — hand-built
# them at ten sites, four of those full error bodies. This module is the one
# both sides may read.


def response(rid, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def error(rid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def notification(method: str, params: dict | None = None) -> dict:
    """An id-less JSON-RPC notification. `params` omitted when None, because
    MCP clients distinguish an absent `params` from an empty one."""
    msg: dict = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    return msg
