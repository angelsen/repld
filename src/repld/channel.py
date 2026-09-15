"""Channel push — the one way anything in repld speaks to the agent.

Every `<channel>` injection the agent sees originates here: task completions,
`@every` output, console errors, human gates, dashboard actions, bare
`notify()`. Sending one means two things at once — a JSON-RPC notification out
over IPC, and a local event so the pane and `repld log` mirror what was sent —
and doing only the first is a bug that is invisible until someone goes looking
for history that was never written.

This lives apart from `kernel.py` for the same reason `paths` / `state` /
`spawn` do: it needs only `ipc`, `events` and the `core_schemas` envelope
builders, while `kernel` pulls in the
asyncio loop, the dispatcher, gists and the display. Six modules across the
kernel and the browser stack push to channel, and keeping it here is what lets
each of them import it at module level rather than reaching for a
function-local `from .kernel import push_channel` to dodge a cycle.
"""

from . import events, ipc
from .core_schemas import notification as _notification
from .events import ChannelPush

_CONTENT_LIMIT = 4000
_META_VALUE_LIMIT = 2000


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"… ({len(text)} chars total)"


def push_channel(
    content: str,
    meta: dict | None = None,
    *,
    session: "ipc.Session | None" = None,
    fallback_broadcast: bool = False,
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

    `fallback_broadcast=True` is the one deliberate exception: a *best-guess*
    affinity (e.g. controls observations routed to whichever session last
    touched the tab) is nobody's specific request the way `origin` is, so a
    stale guess should degrade to the old broadcast behavior rather than
    silently vanish.

    `content` and every `meta` value are clipped here (`_CONTENT_LIMIT`,
    `_META_VALUE_LIMIT`) — the backstop for callers that pass through
    external or user data unbounded (`notify()`'s content, `@every`'s
    stringified result, a controls observation's state), so a single huge
    push can't burn through a receiving session's whole context. A caller
    with a domain-specific preview shape (e.g. `cdp._check_controls_observation`)
    should still clip its own way first; this only catches what isn't.
    """
    meta = meta or {}
    content = _clip(content, _CONTENT_LIMIT)
    meta = {k: _clip(str(v), _META_VALUE_LIMIT) for k, v in meta.items()}
    msg = _notification(
        "notifications/claude/channel", {"content": content, "meta": meta}
    )
    if session is None or (not ipc.post_to(session, msg) and fallback_broadcast):
        ipc.broadcast_channel(msg)
    events.emit(ChannelPush(content, meta))


def push_kind(content: str, kind: str, *, session=None, **meta: str) -> None:
    """push_channel with the ubiquitous {"kind": ...} meta shape spelled out once.

    Every push whose meta keys are known at the call site goes through here.
    Three deliberately don't, and they are the ones that build their meta
    conditionally — `gates._gate` (`options` only for a `choose`),
    `kernel._maybe_push_done` (`label` only when the task has one),
    `cdp._check_controls_observation` (`stateBefore`/`stateAfter` only when the
    control reported them). Forcing those through `**meta` would mean building
    the dict anyway and splatting it, which is the literal dict with extra
    steps.

    `tests/phases/channels.py::phase_4_push_kind_args` guards the argument
    order across every call site, including callers that import this under
    another name — `kernel.py` takes it as `_push`, which is where seven of
    them are.
    """
    push_channel(content, {"kind": kind, **meta}, session=session)
