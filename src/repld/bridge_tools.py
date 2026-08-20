"""MCP tools answered by the bridge itself, never forwarded to the kernel.

**The boundary — keep it narrow.** A tool belongs here only if it must work
while the kernel is dead, or if it *kills* the kernel. Everything else goes in
``protocol.py``. A kernel cannot answer "restart yourself": the reply would
still be in flight when the process goes away, so the client would receive
``-31001`` for an operation that actually succeeded. That, and nothing broader,
is what this module exists for. Without the rule the bridge slowly accretes
into a second, worse kernel.

**Import weight.** The bridge is spawned once per Claude Code session, which is
the whole reason ``spawn.py`` exists as its own module. Keep this one
stdlib-only at import time and put handler-specific imports inside the
handlers.

Wiring, in two halves that must not drift:

  * ``SCHEMAS`` is folded into the kernel's ``tools/list``
    (``protocol.Dispatcher._tools_list``), so these appear alongside the
    built-in and gist tools with no response rewriting. The bridge forwards raw
    lines in both directions and stays byte-preserving on the happy path;
    parsing and re-serializing every ``tools/list`` reply just to append them
    would throw that away.
  * ``BRIDGE_TOOLS`` is consulted by ``bridge.Bridge._handle_client_line`` on
    the *request* side, which already parses each message. A registered name is
    answered locally and never forwarded.

Both halves read this one dict, so the kernel cannot advertise a tool the
bridge doesn't implement.

A handler takes ``(bridge, arguments)`` and returns an MCP ``result`` body; the
bridge turns a raised exception into an ``isError`` result.
"""

from __future__ import annotations

from typing import Any


def _restart(bridge: Any, args: dict) -> dict:
    """Replace the kernel process, keeping the MCP session alive."""
    old_pid, new_pid = bridge.restart_kernel()
    if new_pid is None:
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"stopped kernel pid {old_pid}, but a fresh one did not "
                        "come up — see the bridge's stderr"
                    ),
                }
            ],
            "isError": True,
        }
    return {
        "content": [
            {
                "type": "text",
                "text": (
                    f"kernel restarted: pid {old_pid} → {new_pid}. In-memory "
                    "state (variables, tickers, browser connections) is gone; "
                    "gists re-import on first use."
                ),
            }
        ],
        "isError": False,
        "_meta": {"old_pid": old_pid, "new_pid": new_pid},
    }


BRIDGE_TOOLS: dict[str, dict] = {
    "repld_restart": {
        "schema": {
            "name": "repld_restart",
            "description": (
                "Restart this project's repld kernel, discarding its in-memory "
                "state (variables, @every tickers, browser connections). The "
                "MCP session survives — the bridge respawns the kernel and "
                "replays the handshake, so tools keep working afterwards. Use "
                "when the kernel is wedged, or when it needs to pick up a "
                "changed environment. Not needed for gist edits: those "
                "auto-reload."
            ),
            "inputSchema": {"type": "object", "properties": {}},
            "annotations": {"destructiveHint": True, "idempotentHint": True},
        },
        "handler": _restart,
    },
}

SCHEMAS: list[dict] = [entry["schema"] for entry in BRIDGE_TOOLS.values()]
