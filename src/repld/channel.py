"""Channel push — the one way anything in repld speaks to the agent.

Every `<channel>` injection the agent sees originates here: task completions,
`@every` output, console errors, human gates, dashboard actions, bare
`notify()`. Sending one means two things at once — a JSON-RPC notification out
over IPC, and a local event so the pane and `repld log` mirror what was sent —
and doing only the first is a bug that is invisible until someone goes looking
for history that was never written.

This lives apart from `kernel.py` for the same reason `paths` / `state` /
`spawn` do: it needs only `ipc` and `events`, while `kernel` pulls in the
asyncio loop, the dispatcher, gists and the display. Six modules across the
kernel and the browser stack push to channel, and every one of them used to
reach for a function-local `from .kernel import push_channel` to dodge the
cycle that a module-level import would have created.
"""

from . import events, ipc
from .events import ChannelPush


def push_channel(
    content: str,
    meta: dict | None = None,
    *,
    session: "ipc.Session | None" = None,
) -> None:
    """Send a notifications/claude/channel notification AND emit a local
    ChannelPush event so the pane and the event log mirror what the MCP agent
    receives. Single source of truth for every channel push.

    `session=None` broadcasts — that's the right thing for genuinely ambient
    output (@every errors, console errors, browser connect/disconnect, bare
    `notify()` from shared user code) in repld's shared-__main__ model.
    Passing a session targets the one that asked for the work. If that session
    has since disconnected the push is *dropped*, never downgraded to a
    broadcast: leaking one session's output into every other one is worse than
    silence, and the local event still reaches `repld log`.
    """
    meta = meta or {}
    msg = {
        "jsonrpc": "2.0",
        "method": "notifications/claude/channel",
        "params": {"content": content, "meta": meta},
    }
    if session is None:
        ipc.broadcast_channel(msg)
    else:
        ipc.post_to(session, msg)
    events.emit(ChannelPush(content, {k: str(v) for k, v in meta.items()}))


def push_kind(content: str, kind: str, *, session=None, **meta: str) -> None:
    """push_channel with the ubiquitous {"kind": ...} meta shape spelled out once."""
    push_channel(content, {"kind": kind, **meta}, session=session)
