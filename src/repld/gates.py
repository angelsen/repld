"""Human-gate primitives: ask, confirm, choose — plus the boot-time `tty_prompt`.

Gates are async coroutines — `await ask("name?")` yields to the asyncio loop
while waiting on the human, so uvicorn / watchdog / other bg tasks keep
running. Something calls `resolve_gate(gate_id, ...)` when the human answers;
that resolves the underlying future and the awaiting cell resumes.

**Three surfaces can answer, and which exist depends on how the kernel was
started.** A kernel run as `repld` in a pane has the display thread's stdin
reader. A pinned browser tab has the pill UI. A kernel spawned *for* the user —
Claude Code's bridge, `repld restart`, a systemd unit — has neither: it runs
`--no-display` with stdin on /dev/null, and that is now the common case. So the
registry below is introspectable (`open_gates`) and resolvable out-of-band, and
`repld gate` drives both over IPC. Without that a `confirm()` on an auto-spawned
kernel blocks forever with nothing able to answer it.

`parse_response` is shared by every surface on purpose: "y", "2", and an option
name have to mean the same thing whether they were typed into the pane or into
`repld gate answer`.

`tty_prompt` is the synchronous sibling at the bottom of this file: same job
(ask the human), different mechanism. It runs during boot — installing gist
deps, restoring a browser session — before the loop, the display, or the
output tee exist, so it talks straight to the controlling terminal instead of
routing through an event. Boot-only is a hard requirement, not a description:
past it the display thread owns stdin, and `_stdin_owned` is what makes a late
caller decline instead of deadlocking against it.
"""

import asyncio
import concurrent.futures
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from typing import IO, Literal

from .channel import push_channel
from .events import HumanPromptClosed, HumanPromptOpen, HumanPromptResponse, emit


@dataclass
class _Gate:
    """A pending gate. The prompt is kept so `open_gates` can describe it —
    a surface that never saw the original channel push has nothing else to
    render, and `repld gate` is exactly that surface."""

    future: "concurrent.futures.Future"
    kind: str
    prompt: str
    options: list[str] | None
    opened_at: float
    # Whether a human actually answered, as opposed to the gate ending some
    # other way. Read by `_gate`'s finally to decide between the two closing
    # events, and a flag rather than an inspection of `future` because the
    # states are not the same question: a cancelled future is indistinguishable
    # from one whose `set_result` lost a race, and only this is set by the one
    # code path that means "a human said something".
    answered: bool = False


_gates: dict[str, _Gate] = {}
_gates_lock = threading.Lock()

# Whether this kernel has a pane whose stdin reader can answer a gate. Set once
# at boot by `kernel._boot_runtime`; False for every `--no-display` kernel,
# which is what lets `_gate` tell the agent that nobody is listening.
_has_terminal = False

# Whether that reader is *running right now*, i.e. parked in readline() on
# sys.__stdin__. A separate flag because it answers a separate question, and
# the two are deliberately true at different times: `_has_terminal` is set at
# the top of `kernel._boot_runtime` and means "a pane will exist", which is
# what a gate needs to know when it decides whether to print the `repld gate
# answer` fallback. This one is set by `display.run_display`, long after boot
# finishes, and is what `tty_prompt` consults — see its docstring for why
# anything that reads stdin directly has to.
_stdin_owned = False

_YES = frozenset({"y", "yes", "1", "true"})
_NO = frozenset({"n", "no", "0", "false"})


def set_terminal(value: bool) -> None:
    """Record whether a display thread will be reading stdin for gate answers."""
    global _has_terminal
    _has_terminal = value


def set_stdin_owned(value: bool) -> None:
    """Record that display's stdin reader thread is live and consuming lines."""
    global _stdin_owned
    _stdin_owned = value


def stdin_owned() -> bool:
    """Whether display's stdin reader owns sys.__stdin__ right now."""
    return _stdin_owned


def open_gates() -> list[dict]:
    """Every gate currently waiting on a human, oldest first.

    Serialisable by construction: it crosses the IPC socket to `repld gate`,
    via the kernel's `gates/list` method. That is the only caller today; the
    shape is JSON-ready so a second surface (the dashboard) wouldn't need a
    different accessor.
    """
    now = time.monotonic()
    with _gates_lock:
        pending = [(gid, g) for gid, g in _gates.items() if not g.future.done()]
    pending.sort(key=lambda item: item[1].opened_at)
    return [
        {
            "gate_id": gid,
            "kind": g.kind,
            "prompt": g.prompt,
            "options": list(g.options or []),
            "waiting_s": round(now - g.opened_at, 1),
        }
        for gid, g in pending
    ]


def parse_response(kind: str, text: str, options: list[str] | None = None):
    """Coerce a typed line into the value a gate of `kind` resolves to.

    Shared by the pane's stdin reader and `repld gate answer` so the two can't
    disagree about what "y" or "2" means. Raises ValueError with a
    human-readable reason for anything the kind doesn't accept; callers decide
    whether to re-prompt (the pane) or exit non-zero (the CLI).
    """
    text = text.strip()
    if kind == "confirm":
        low = text.lower()
        if low in _YES:
            return True
        if low in _NO:
            return False
        raise ValueError("type y or n")
    if kind == "choose":
        if not options:
            return text
        if text in options:
            return text
        # The prompt renders options as `1=alpha, 2=beta`, so an index is the
        # obvious thing to type, so it must resolve to the option rather than
        # to the literal "1".
        if text.isdigit() and 1 <= int(text) <= len(options):
            return options[int(text) - 1]
        listed = ", ".join(options)
        raise ValueError(f"choose one of: {listed} (or 1-{len(options)})")
    return text


async def ask(
    prompt: str,
    *,
    tab=None,
    default: str | None = None,
    timeout: float | None = None,
) -> str:
    """Prompt the human for a free-form string response.

    `tab` is accepted for symmetry with confirm/choose — the pill UI has
    no text input, so the response is always typed in the terminal."""
    value = await _gate("ask", prompt, None, default, timeout, tab=tab)
    return str(value)


async def confirm(
    prompt: str,
    *,
    tab=None,
    default: bool | None = None,
    timeout: float | None = None,
) -> bool:
    """Prompt the human for a yes/no response. Returns bool."""
    value = await _gate("confirm", prompt, None, default, timeout, tab=tab)
    return bool(value)


async def choose(
    prompt: str,
    options: list[str],
    *,
    tab=None,
    default: str | None = None,
    timeout: float | None = None,
) -> str:
    """Prompt the human to choose one of the given options."""
    value = await _gate("choose", prompt, options, default, timeout, tab=tab)
    return str(value)


async def _gate(
    kind: Literal["ask", "confirm", "choose"],
    prompt: str,
    options: list[str] | None,
    default: str | bool | None,
    timeout: float | None,
    *,
    tab=None,
) -> str | bool:
    gate_id = uuid.uuid4().hex[:8]
    fut: concurrent.futures.Future = concurrent.futures.Future()
    with _gates_lock:
        _gates[gate_id] = _Gate(fut, kind, prompt, options, time.monotonic())

    # `kind` is part of this, not just pinnedness: the pill is a row of
    # buttons with no text input, so `_show_gate` renders confirm and choose
    # and returns without drawing anything for an ask. Deciding on pinnedness
    # alone made `tab.ask()` suppress the "repld gate answer" hint below *and*
    # show no pill — on a headless kernel (the usual one) a gate that no
    # surface could answer.
    use_pill = tab is not None and getattr(tab, "_pinned", False) and kind != "ask"

    # Channel push first, then prompt — so the panel renders on a fresh
    # line in the viewer before the prompt text (which ends with `: ` and
    # waits on stdin, so it mustn't be followed by panel borders).
    meta = {"kind": "awaiting_human", "gate_id": gate_id, "prompt_kind": kind}
    if options:
        meta["options"] = ",".join(options)
    content = f"awaiting human: {prompt}"
    if not _has_terminal and not use_pill:
        # Nothing attached to this kernel can answer, and without timeout= the
        # cell blocks indefinitely. The agent is the one who sees this push, so
        # give it the literal command to hand the human rather than leaving both
        # of them waiting on a pane that doesn't exist.
        content += (
            f"\nno terminal attached — answer from the project dir with: "
            f"repld gate answer {gate_id} <value>"
        )
    push_channel(content, meta)
    emit(HumanPromptOpen(gate_id, kind, prompt, options))

    # Route to pill UI if tab is pinned
    if use_pill and tab is not None:
        asyncio.create_task(
            tab._show_gate(gate_id, kind, prompt, options),
            name=f"repld-gate-show-{gate_id}",
        )

    # Only read when nobody answered, so the remaining question is which of the
    # two silent endings it was: `timeout=` expiring, or the awaiting cell being
    # cancelled (`cancel`, or the loop watchdog).
    reason: Literal["timeout", "cancelled"] = "cancelled"
    try:
        wrapped = asyncio.wrap_future(fut)
        if timeout is not None:
            return await asyncio.wait_for(wrapped, timeout=timeout)
        return await wrapped
    except asyncio.TimeoutError:
        reason = "timeout"
        if default is not None:
            return default
        raise TimeoutError(f"no response to {prompt!r} within {timeout}s")
    finally:
        with _gates_lock:
            gate = _gates.pop(gate_id, None)
        # A gate ending unanswered has to be announced, not just forgotten:
        # `open_gates` is this dict, so `repld gate` self-corrects, but every
        # surface that *mirrors* it learns only from an event. Without this the
        # pane's `_open_gates` keeps the entry forever — stdin keeps routing to
        # a gate nothing can resolve, and the dead prompt is re-shown whenever a
        # later one is answered. Note `timeout=` is the documented way to keep a
        # gate from parking a cell indefinitely, so this is the recommended path.
        if gate is not None and not gate.answered:
            emit(HumanPromptClosed(gate_id, reason))


def resolve_gate(gate_id: str, value) -> bool:
    """Answer a pending gate. Returns whether this call is the one that did.

    The pane's stdin reader, a browser pill, and `repld gate answer` can all
    race to resolve the same gate — first one wins by design; set_result on an
    already-done future raises InvalidStateError, which we swallow rather than
    crash the caller.

    `answered` is claimed *before* `set_result` and released again if that
    raises. Setting it afterwards would leave a window in which a gate whose
    timeout has just fired is marked answered by a losing caller, and `_gate`'s
    finally would then emit no closing event at all — the very leak the flag
    exists to close.
    """
    with _gates_lock:
        gate = _gates.get(gate_id)
        if gate is None or gate.answered:
            return False
        gate.answered = True
    try:
        gate.future.set_result(value)
    except concurrent.futures.InvalidStateError:
        with _gates_lock:
            gate.answered = False
        return False
    emit(HumanPromptResponse(gate_id, value))
    return True


# ---------------------------------------------------------------------------
# Boot-time prompt (pre-loop, pre-display)
# ---------------------------------------------------------------------------


def tty_prompt(prompt: str, *, stream: "IO[str] | None" = None) -> str | None:
    """Write `prompt` to the real terminal (bypassing any stdout/stderr tee)
    and read one stripped, lowercased line of response.

    Defaults to sys.__stderr__ / sys.__stdin__. Returns None if there is
    nobody to ask rather than raising, so callers can treat "can't ask" as
    distinct from any answer a human could actually give.

    **stdin must be a tty, not merely open.** Every auto-spawned kernel runs
    headless with stdin on /dev/null — as a systemd unit (`StandardInput=null`)
    or a detached child (`Popen(stdin=DEVNULL)`) — where `readline()` returns
    "" immediately. Callers that read a bare Enter as consent (`[Y/n]`) would
    otherwise see unattended agreement from a process no human is watching.

    **Boot-time only, and that is now enforced rather than assumed.** Once
    `display.run_display` starts its stdin reader, that thread is parked in
    `readline()` on this same `sys.__stdin__` and consumes every line whether
    or not a gate is open. A second reader here doesn't compete for the line so
    much as deadlock behind the reader's buffer lock — and `gist_deps` reaches
    this function well after boot, from `_check_reload` / `_scan_new_deps`,
    which run under `builtins.__import__` inside a user cell. On an `await`
    cell that cell *is* the loop (`runtime._eval` only threads pure-sync ones),
    so the kernel wedges: the watchdog's cancel lands at the next await point,
    and there isn't one until a blocking `readline()` that nothing will
    satisfy returns. `stdin_owned()` is False for the whole of boot — which is
    when the two legitimate callers, the gist-dep prompt and the browser-restore
    prompt, actually run — and True from `run_display` onward. Returning None
    puts a late caller on the same path as a headless kernel, which every
    caller already handles.
    """
    out = stream if stream is not None else sys.__stderr__
    stdin = sys.__stdin__
    if out is None or stdin is None:
        return None
    if _stdin_owned:
        return None
    try:
        if not stdin.isatty():
            return None
    except (OSError, ValueError):
        return None
    out.write(prompt)
    out.flush()
    try:
        return stdin.readline().strip().lower()
    except OSError:
        return None
