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

from collections.abc import Callable

from . import events, ipc
from .core_schemas import notification as _notification
from .events import ChannelPush
from .tasks import spill_marker as _spill_marker
from .tasks import spill_text as _spill_text

_META_VALUE_LIMIT = 2000

# Optional single callback, same shape as events.set_sink: a project's
# repld_init.py registers one so push_channel never has to import anything
# project-specific.
_meta_augmenter: "Callable[[], dict[str, str]] | None" = None


def set_meta_augmenter(fn: "Callable[[], dict[str, str]] | None") -> None:
    """Register (or clear) a callback whose returned dict merges into every push's meta.

    Must be synchronous and cheap -- push_channel runs on whatever thread or
    loop called it, including the kernel's shared asyncio loop. Exceptions
    are swallowed so a broken augmenter drops its own contribution instead
    of taking every push in the kernel down with it.
    """
    global _meta_augmenter
    _meta_augmenter = fn


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
    exclude: "ipc.Session | None" = None,
) -> bool | None:
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

    `exclude` only applies to the broadcast path (`session=None`): skip one
    session — the caller's own — that already has this update some other way
    (a self-report's synchronous return value) and doesn't need it echoed
    back. Ignored when `session` is set, since targeting one session and
    excluding another are different requests.

    `fallback_broadcast=True` is the one deliberate exception: a *best-guess*
    affinity (e.g. controls observations routed to whichever session last
    touched the tab) is nobody's specific request the way `origin` is, so a
    stale guess should degrade to the old broadcast behavior rather than
    silently vanish.

    `content` is a backstop for callers that pass through external or user
    data unbounded (`notify()`'s content, `@every`'s stringified result, a
    controls observation's state): it goes through `tasks.spill_text`, the
    same head+tail preview and spill-to-disk exec output uses, so a huge push
    loses nothing — the full text stays reachable at the `[full output: ...]`
    path instead of being cut off. `meta` values only get the flat
    `_META_VALUE_LIMIT` clip — they're XML-attribute-shaped (`kind`,
    `control`, `target`, ...), never a payload worth a spill file of its own.
    A caller with a domain-specific preview shape (e.g.
    `cdp._check_controls_observation`) should still clip its own way first;
    this only catches what isn't.

    Returns whether a targeted push reached `session` (False when dropped or
    downgraded to a broadcast), or None for a broadcast.
    """
    if _meta_augmenter is not None:
        try:
            meta = {**_meta_augmenter(), **(meta or {})}
        except Exception:
            pass  # a broken augmenter must never break the push (events.set_sink's own rule)
    meta = meta or {}
    sp = _spill_text(content, label="channel")
    content = sp["text"]
    if sp["truncated"]:
        marker = _spill_marker(sp["spill_path"])
        content = f"{content}\n{marker}" if content else marker
    meta = {k: _clip(str(v), _META_VALUE_LIMIT) for k, v in meta.items()}
    msg = _notification(
        "notifications/claude/channel", {"content": content, "meta": meta}
    )
    delivered = None
    if session is None:
        ipc.broadcast_channel(msg, exclude=exclude)
    else:
        delivered = ipc.post_to(session, msg)
        if not delivered and fallback_broadcast:
            ipc.broadcast_channel(msg)
    events.emit(ChannelPush(content, meta))
    return delivered


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
