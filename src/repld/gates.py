"""Human-gate primitives: `form` and its one-field wrappers ask, confirm, choose, plus the boot-time tty_prompt.

Gates are async coroutines. `await ask("name?")` yields to the asyncio loop
while waiting on the human, so uvicorn / watchdog / other bg tasks keep
running. Something calls `resolve_gate(gate_id, ...)` when the human answers;
that resolves the underlying future and the awaiting cell resumes.

Three surfaces can answer, and which exist depends on how the kernel was
started. A kernel run as `repld` in a pane has the display thread's stdin
reader. A pinned browser tab has the pill UI. A kernel spawned for the user
(Claude Code's bridge, `repld restart`, a systemd unit) has neither: it runs
`--no-display` with stdin on /dev/null, and that is now the common case. So
the registry below is introspectable (`open_gates`) and resolvable
out-of-band, and `repld gate` drives both over IPC. Without that a
`confirm()` on an auto-spawned kernel blocks forever with nothing able to
answer it.

`parse_response` is shared by every surface on purpose: "y", "2", and an
option name have to mean the same thing whether they were typed into the pane
or into `repld gate answer`.

`tty_prompt` is the synchronous sibling at the bottom of this file: same job
(ask the human), different mechanism. It runs during boot (installing gist
deps, restoring a browser session) before the loop, the display, or the
output tee exist, so it talks straight to the controlling terminal instead of
routing through an event. Boot-only is a hard requirement, not a description:
past it the display thread owns stdin, and `_stdin_owned` is what makes a
late caller decline instead of deadlocking against it.
"""

import asyncio
import concurrent.futures
import json
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from typing import IO, Literal

from . import bg, ipc, tasks
from .channel import push_channel
from .core_schemas import notification
from .events import HumanPromptClosed, HumanPromptOpen, HumanPromptResponse, emit


@dataclass
class _Gate:
    """A pending gate, kept so `open_gates` can describe it to surfaces that
    never saw the original channel push."""

    future: "concurrent.futures.Future"
    kind: str
    prompt: str
    options: list[str] | None
    opened_at: float
    schema: dict       # the elicitation `requestedSchema`; every kind carries one
    field: str | None  # the one field a wrapper returns bare; None for `form`
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
# finishes, and is what `tty_prompt` consults. See its docstring for why
# anything that reads stdin directly has to.
_stdin_owned = False

_YES = frozenset({"y", "yes", "1", "true"})
_NO = frozenset({"n", "no", "0", "false"})

# Elicitation requests in flight: request id -> (gate_id, the session asked).
_elicit_pending: dict[str, tuple[str, ipc.Session]] = {}

_PRIMITIVES = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}
_JSON_PRIMITIVES = frozenset(_PRIMITIVES.values())

# The field name every wrapper gate's one-field schema uses.
_ANSWER = "answer"


class GateDeclined(Exception):
    """The human pressed Decline on an elicitation for a gate with no "no" answer."""


async def ask(
    prompt: str,
    *,
    tab=None,
    default: str | None = None,
    timeout: float | None = None,
) -> str:
    """Prompt the human for a free-form string response.

    `tab` is accepted for symmetry with confirm/choose. The pill UI has
    no text input, so the response is always typed in the terminal.
    """
    schema = _one_field({"type": "string", "title": "Answer"})
    value = await _gate(
        "ask", prompt, None, default, timeout, tab=tab, schema=schema, field=_ANSWER
    )
    return str(value)


async def confirm(
    prompt: str,
    *,
    tab=None,
    default: bool | None = None,
    timeout: float | None = None,
    on_open=None,
) -> bool:
    """Prompt the human for a yes/no response. Returns bool.

    `on_open(gate_id)` is called once the gate is registered and answerable,
    for a surface that must tell this gate from another with the same prompt.
    """
    schema = _one_field({"type": "boolean", "title": "Confirm", "default": True})
    value = await _gate(
        "confirm",
        prompt,
        None,
        default,
        timeout,
        tab=tab,
        on_open=on_open,
        schema=schema,
        field=_ANSWER,
    )
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
    prop: dict = {"type": "string", "title": "Choose"}
    if options:
        prop["enum"] = list(options)
    value = await _gate(
        "choose",
        prompt,
        options,
        default,
        timeout,
        tab=tab,
        schema=_one_field(prop),
        field=_ANSWER,
    )
    return str(value)


async def form(
    prompt: str,
    fields: dict,
    *,
    required: list[str] | None = None,
    timeout: float | None = None,
) -> dict:
    """Prompt the human for several typed fields at once; returns `{name: value}`.

    A field is `str`, `int`, `float`, `bool`, a list of strings (a choice), or
    a dict holding a primitive JSON-schema property (`minimum`, `format`,
    `description`...). All are required unless `required=` narrows it. Raises
    `ValueError` before asking if a field is not one of those.
    """
    schema = build_form_schema(fields, required)
    value = await _gate("form", prompt, None, None, timeout, schema=schema, field=None)
    assert isinstance(value, dict)
    return value


def _one_field(prop: dict) -> dict:
    """The `requestedSchema` of a wrapper gate: one required field named `_ANSWER`."""
    return {
        "type": "object",
        "properties": {_ANSWER: prop},
        "required": [_ANSWER],
    }


def build_form_schema(fields: dict, required: list[str] | None = None) -> dict:
    """The flat `requestedSchema` for `fields`; raises `ValueError` on a non-primitive."""
    if not fields:
        raise ValueError("form() needs at least one field")
    props: dict[str, dict] = {}
    for name, spec in fields.items():
        props[str(name)] = _field_property(str(name), spec)
    names = list(props) if required is None else list(required)
    unknown = [n for n in names if n not in props]
    if unknown:
        raise ValueError(f"required names no field: {', '.join(unknown)}")
    return {"type": "object", "properties": props, "required": names}


def _field_property(name: str, spec) -> dict:
    """One field spec as a JSON-schema property; raises `ValueError` if it is not flat."""
    if isinstance(spec, type) and spec in _PRIMITIVES:
        return {"type": _PRIMITIVES[spec]}
    if isinstance(spec, list) and spec and all(isinstance(o, str) for o in spec):
        return {"type": "string", "enum": list(spec)}
    if isinstance(spec, dict) and spec.get("type") in _JSON_PRIMITIVES:
        return dict(spec)
    raise ValueError(
        f"field {name!r}: use str, int, float, bool, a list of strings, "
        "or a dict with a primitive JSON-schema type"
    )


async def _gate(
    kind: Literal["ask", "confirm", "choose", "form"],
    prompt: str,
    options: list[str] | None,
    default: str | bool | None,
    timeout: float | None,
    *,
    schema: dict,
    field: str | None,
    tab=None,
    on_open=None,
) -> str | bool | dict:
    """Register a gate, announce it, optionally render a pill, and await the answer.

    The try/finally opens right after registration so that anything raising
    between registration and the await (a failed push, a bg.spawn error) still
    pops the entry. A gate left in `_gates` with nobody awaiting its future
    would let `repld gate` "answer" a dead promise.
    """
    gate_id = uuid.uuid4().hex[:8]
    fut: concurrent.futures.Future = concurrent.futures.Future()
    with _gates_lock:
        _gates[gate_id] = _Gate(
            fut, kind, prompt, options, time.monotonic(), schema, field
        )

    # Only read when nobody answered, so the remaining question is which of the
    # two silent endings it was: `timeout=` expiring, or the awaiting cell
    # being cancelled (`cancel`, or the loop watchdog).
    reason: Literal["timeout", "cancelled", "declined"] = "cancelled"
    elicit_id: str | None = None
    # `kind` is part of this, not just pinnedness: the pill is a row of
    # buttons with no text input, so `_show_gate` renders confirm and
    # choose and returns without drawing anything for an ask. Deciding on
    # pinnedness alone made `tab.ask()` suppress the "repld gate answer"
    # hint below *and* show no pill: on a headless kernel (the usual one)
    # a gate that no surface could answer.
    use_pill = tab is not None and getattr(tab, "_pinned", False) and kind != "ask"
    try:
        # Channel push first, then prompt, so the panel renders on a fresh
        # line in the viewer before the prompt text (which ends with `: ` and
        # waits on stdin, so it must not be followed by panel borders).
        meta = {"kind": "awaiting_human", "gate_id": gate_id, "prompt_kind": kind}
        if options:
            meta["options"] = ",".join(options)
        content = f"awaiting human: {prompt}"
        if not _has_terminal and not use_pill:
            # Nothing attached to this kernel can answer, and without timeout=
            # the cell blocks indefinitely. The agent sees this push, so give it
            # the literal command to hand the human rather than leaving both of
            # them waiting on a pane that does not exist.
            content += (
                f"\nno terminal attached. Answer from the project dir with: "
                f"repld gate answer {gate_id} <value>"
            )
        push_channel(
            content, meta, session=tasks.current_origin(), fallback_broadcast=True
        )
        emit(HumanPromptOpen(gate_id, kind, prompt, options, schema))
        if on_open is not None:
            on_open(gate_id)
        elicit_id = _send_elicitation(gate_id, prompt, schema, tab)

        # Route to pill UI if tab is pinned.
        if use_pill and tab is not None:
            # bg.spawn, not a bare create_task: `use_pill` is precisely the
            # case where the push above withheld the `repld gate answer`
            # command because a pill was going to render, so a task collected
            # mid-flight leaves the gate with no surface at all. See bg.py.
            bg.spawn(
                tab._show_gate(gate_id, kind, prompt, options),
                name=f"repld-gate-show-{gate_id}",
            )

        wrapped = asyncio.wrap_future(fut)
        if timeout is not None:
            return await asyncio.wait_for(wrapped, timeout=timeout)
        return await wrapped
    except GateDeclined:
        reason = "declined"
        raise
    except TimeoutError:
        reason = "timeout"
        if default is not None:
            return default
        # `from None`: same class either way on 3.11+, and this message is
        # the one that names the prompt nobody answered.
        raise TimeoutError(f"no response to {prompt!r} within {timeout}s") from None
    finally:
        if elicit_id is not None:
            _cancel_elicitation(elicit_id)
        with _gates_lock:
            gate = _gates.pop(gate_id, None)
        if use_pill and tab is not None:
            bg.spawn(tab._hide_gate(gate_id), name=f"repld-gate-hide-{gate_id}")
        # A gate ending unanswered has to be announced, not just forgotten:
        # `open_gates` is this dict, so `repld gate` self-corrects, but
        # every surface that *mirrors* it learns only from an event. Without
        # this the pane's `_open_gates` keeps the entry forever: stdin keeps
        # routing to a gate nothing can resolve, and the dead prompt is
        # re-shown whenever a later one is answered. `timeout=` is the
        # documented way to keep a gate from parking a cell indefinitely, so
        # this is the recommended path.
        if gate is not None and not gate.answered:
            emit(HumanPromptClosed(gate_id, reason))


def _send_elicitation(gate_id: str, prompt: str, schema: dict, tab) -> str | None:
    """Ask the origin session's client to put this gate in front of the human.

    Returns the request id, or None when the origin cannot be elicited (no
    session, no `elicitation` capability, a `bg` worker, a peer): such a gate
    keeps the `awaiting_human` push as its only announcement.
    """
    session = tasks.current_origin()
    if session is None or not ipc.can_elicit(session):
        return None
    message = prompt
    if tab is not None and getattr(tab, "_pinned", False):
        message = f"{tab.url}\n{prompt}"
    request_id = f"repld-elicit-{gate_id}"
    _elicit_pending[request_id] = (gate_id, session)
    session.write(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "elicitation/create",
            "params": {
                "message": message,
                "requestedSchema": schema,
            },
        }
    )
    return request_id


def _cancel_elicitation(request_id: str) -> None:
    """Close the client's modal for a gate that ended; a no-op once it answered."""
    pending = _elicit_pending.pop(request_id, None)
    if pending is None:
        return
    pending[1].write(
        notification(
            "notifications/cancelled",
            {"requestId": request_id, "reason": "gate closed"},
        )
    )


def parse_elicit_result(
    schema: dict, field: str | None, result: dict
) -> tuple[Literal["accept", "decline", "cancel"], str | bool | dict | None]:
    """Map an `elicitation/create` result to the action and gate value it means.

    Content that does not fit `schema` reads as `cancel`: the gate stays open
    for the other surfaces rather than taking a value nobody chose. A `field`
    unwraps the one-field object a wrapper gate asked for.
    """
    action = result.get("action")
    if action == "decline":
        return "decline", None
    if action != "accept":
        return "cancel", None
    try:
        content = validate_form(result.get("content"), schema)
    except (ValueError, TypeError):
        return "cancel", None
    return "accept", content if field is None else content[field]


def handle_elicit_result(
    request_id: str, result: dict | None, error: dict | None
) -> None:
    """Apply the client's reply to an elicitation request; unknown ids are ignored.

    Accept answers the gate. Decline is a "no": `False` for `confirm`, else
    `GateDeclined` raised into the cell. Cancel or an error answers nothing.
    """
    pending = _elicit_pending.pop(request_id, None)
    if pending is None or error is not None or result is None:
        return
    gate_id = pending[0]
    with _gates_lock:
        gate = _gates.get(gate_id)
    if gate is None:
        return
    action, value = parse_elicit_result(gate.schema, gate.field, result)
    if action == "accept":
        resolve_gate(gate_id, value)
    elif action == "decline" and gate.kind == "confirm":
        resolve_gate(gate_id, False)
    elif action == "decline":
        _decline_gate(gate)


def _decline_gate(gate: _Gate) -> None:
    """Fail the gate's future with `GateDeclined`; a lost race is ignored."""
    try:
        gate.future.set_exception(GateDeclined(gate.prompt))
    except concurrent.futures.InvalidStateError:
        pass


def resolve_gate(gate_id: str, value) -> bool:
    """Answer a pending gate. Returns whether this caller won the race.

    The pane's stdin reader, a browser pill, `repld gate answer` and the
    dashboard can all race to resolve the same gate. First one wins by design;
    set_result on an already-done future raises InvalidStateError, which is
    swallowed rather than crashing the caller.

    `answered` is claimed *before* `set_result` and released again if that
    raises. Setting it afterwards would leave a window in which a gate whose
    timeout has just fired is marked answered by a losing caller, and
    `_gate`'s finally would then emit no closing event at all, the very leak
    the flag exists to close.
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


def open_gates() -> list[dict]:
    """Every gate currently waiting on a human, oldest first.

    Serialisable by construction: it crosses the IPC socket to `repld gate`,
    via the kernel's `gates/list` method, and the dashboard's `gates` RPC
    reads the same shape.
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
            "schema": g.schema,
            "waiting_s": round(now - g.opened_at, 1),
        }
        for gid, g in pending
    ]


def parse_response(
    kind: str,
    text: str,
    options: list[str] | None = None,
    schema: dict | None = None,
) -> bool | str | dict:
    """Coerce a typed line into the value a gate of `kind` resolves to.

    Shared by the pane's stdin reader and `repld gate answer` so the two
    cannot disagree about what "y" or "2" means. Raises ValueError with a
    human-readable reason for anything the kind does not accept; callers decide
    whether to re-prompt (the pane) or exit non-zero (the CLI).
    """
    text = text.strip()
    if kind == "form":
        return parse_form(text, schema or {})
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


def parse_form(text: str, schema: dict) -> dict:
    """A form answer typed as one JSON object; `ValueError` names what is wrong."""
    try:
        content = json.loads(text)
    except json.JSONDecodeError:
        raise ValueError(
            "answer with one JSON object, e.g. " + _example(schema)
        ) from None
    try:
        return validate_form(content, schema)
    except TypeError as exc:
        raise ValueError(str(exc)) from None


def validate_form(content, schema: dict) -> dict:
    """`content` if it satisfies the flat `schema`; else `ValueError` (`TypeError` if not an object)."""
    if not isinstance(content, dict):
        raise TypeError("answer with one JSON object, e.g. " + _example(schema))
    props = schema.get("properties", {})
    for name in content:
        if name not in props:
            raise ValueError(f"no such field: {name}")
    for name in schema.get("required", []):
        if name not in content:
            raise ValueError(f"missing field: {name}")
    for name, value in content.items():
        _check_field(name, value, props[name])
    return content


def _check_field(name: str, value, prop: dict) -> None:
    """Raise `ValueError` unless `value` fits the primitive `prop`."""
    kind = prop["type"]
    # bool is an int subclass: `True` must not satisfy integer or number.
    ok = {
        "string": isinstance(value, str),
        "boolean": isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, int | float) and not isinstance(value, bool),
    }[kind]
    if not ok:
        raise ValueError(f"{name}: expected {kind}")
    if "enum" in prop and value not in prop["enum"]:
        raise ValueError(f"{name}: choose one of {', '.join(prop['enum'])}")


def _example(schema: dict) -> str:
    """A JSON object naming the fields of `schema`, for an error message."""
    props = schema.get("properties", {})
    return "{" + ", ".join(f'"{n}": <{p["type"]}>' for n, p in props.items()) + "}"


class GateAnswerError(ValueError):
    """An answer a gate refused: unknown or already-answered id, or a value its kind rejects."""


def answer_gate(gate_id: str, text: str) -> bool | str | dict:
    """Coerce `text` for the named pending gate and resolve it; returns the value.

    The one place a remote answer (`repld gate answer`, the dashboard) is
    turned into a gate value, so the kernel stays the authority on what a gate
    accepts. Raises `GateAnswerError` rather than returning False: a lost race
    is still an error to the caller, though the cell is unblocked either way.
    """
    gate = next((g for g in open_gates() if g["gate_id"] == gate_id), None)
    if gate is None:
        raise GateAnswerError(f"no gate awaiting an answer: {gate_id}")
    try:
        value = parse_response(gate["kind"], text, gate["options"], gate["schema"])
    except ValueError as exc:
        raise GateAnswerError(str(exc)) from None
    if not resolve_gate(gate_id, value):
        raise GateAnswerError(f"gate {gate_id} was already answered")
    return value


def yes_by_default(answer: str | None) -> bool:
    """Whether a `[Y/n]` prompt's answer means yes, with Enter/"" as yes.

    For the boot-time `tty_prompt` callers (restoring a browser session,
    installing gist deps) which ask before the loop, the display or the tee
    exist and so cannot use `confirm()`. They each kept their own
    `answer in ("", "y", "yes")`, which accepted a *narrower* set than
    `parse_response` does: typing `1` or `true` at the browser-restore
    prompt read as a decline while the same word answers a `confirm()` gate
    as yes. Same vocabulary now, in the one place that owns it; the
    empty-string default is the only thing genuinely different about these
    prompts, and it is what this function adds.

    None (nobody to ask) is not consent.
    """
    if answer is None:
        return False
    return answer.strip() == "" or answer.strip().lower() in _YES


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


def tty_prompt(prompt: str, *, stream: "IO[str] | None" = None) -> str | None:
    """Write `prompt` to the real terminal and read one stripped, lowercased line.

    Defaults to sys.__stderr__ / sys.__stdin__. Returns None if there is
    nobody to ask rather than raising, so callers can treat "can't ask" as
    distinct from any answer a human could actually give.

    stdin must be a tty, not merely open. Every auto-spawned kernel runs
    headless with stdin on /dev/null (a systemd unit with
    `StandardInput=null`, or a detached child with
    `Popen(stdin=DEVNULL)`), where `readline()` returns "" immediately.
    Callers that read a bare Enter as consent (`[Y/n]`) would otherwise see
    unattended agreement from a process no human is watching.

    Boot-time only, and that is now enforced rather than assumed. Once
    `display.run_display` starts its stdin reader, that thread is parked in
    `readline()` on this same `sys.__stdin__` and consumes every line
    whether or not a gate is open. A second reader here does not compete for
    the line so much as deadlock behind the reader's buffer lock, and
    `gist_deps` reaches this function well after boot, from
    `_check_reload` / `_scan_new_deps`, which run under
    `builtins.__import__` inside a user cell. On an `await` cell that cell
    *is* the loop (`runtime._eval` only threads pure-sync ones), so the
    kernel wedges: the watchdog's cancel lands at the next await point, and
    there is not one until a blocking `readline()` that nothing will satisfy
    returns. `stdin_owned()` is False for the whole of boot, which is when
    the two legitimate callers (the gist-dep prompt and the browser-restore
    prompt) actually run, and True from `run_display` onward. Returning None
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
