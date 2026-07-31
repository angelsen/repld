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

# The MCP revision every repld process advertises. Here rather than in
# `protocol.py` because three of them answer `initialize` and only one can
# import it: the kernel (`protocol._initialize`), the bridge when no kernel has
# ever run in this project (`bridge._try_bridge_intercept`), and `repld exec`,
# which handshakes over the socket itself. Importing `protocol.py` from the
# other two would drag in browser_dispatch/help/kernel_context/tasks for one
# string, so the alternative is a hand-synced copy in each — a drift no test
# can see, since every side would still be internally consistent.
PROTOCOL_VERSION = "2024-11-05"

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


def wire(resources: list[dict]) -> list[dict]:
    """Strip internal `_`-prefixed keys so a resource dict is MCP-clean."""
    return [{k: v for k, v in r.items() if not k.startswith("_")} for r in resources]
