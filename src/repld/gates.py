"""Human-gate primitives: ask, confirm, choose.

Gates are async coroutines — `await ask("name?")` yields to the asyncio loop
while waiting on the human, so uvicorn / watchdog / other bg tasks keep
running. The display thread's stdin reader calls `resolve_gate(gate_id, ...)`
when the human types a response; that resolves the underlying future and
the awaiting cell resumes.
"""

import asyncio
import concurrent.futures
import threading
import uuid

from .events import HumanPromptOpen, HumanPromptResponse, emit

_gates: dict[str, concurrent.futures.Future] = {}
_gates_lock = threading.Lock()


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
    return await _gate("ask", prompt, None, default, timeout, tab=tab)  # type: ignore[return-value]


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
    return await _gate("choose", prompt, options, default, timeout, tab=tab)  # type: ignore[return-value]


async def _gate(kind, prompt, options, default, timeout, *, tab=None):
    # Lazy import to avoid a gates↔kernel cycle.
    from .kernel import push_channel

    gate_id = uuid.uuid4().hex[:8]
    fut: concurrent.futures.Future = concurrent.futures.Future()
    with _gates_lock:
        _gates[gate_id] = fut

    # Channel push first, then prompt — so the panel renders on a fresh
    # line in the viewer before the prompt text (which ends with `: ` and
    # waits on stdin, so it mustn't be followed by panel borders).
    meta = {"kind": "awaiting_human", "gate_id": gate_id, "prompt_kind": kind}
    if options:
        meta["options"] = ",".join(options)
    push_channel(f"awaiting human: {prompt}", meta)
    emit(HumanPromptOpen(gate_id, kind, prompt, options))

    # Route to pill UI if tab is pinned
    if tab is not None and getattr(tab, "_pinned", False):
        asyncio.create_task(
            tab._show_gate(gate_id, kind, prompt, options),
            name=f"repld-gate-show-{gate_id}",
        )

    try:
        wrapped = asyncio.wrap_future(fut)
        if timeout is not None:
            return await asyncio.wait_for(wrapped, timeout=timeout)
        return await wrapped
    except asyncio.TimeoutError:
        if default is not None:
            return default
        raise TimeoutError(f"no response to {prompt!r} within {timeout}s")
    finally:
        with _gates_lock:
            _gates.pop(gate_id, None)


def resolve_gate(gate_id: str, value) -> None:
    """Called by the stdin reader when the human responds to a gate."""
    with _gates_lock:
        fut = _gates.get(gate_id)
    if fut is not None and not fut.done():
        fut.set_result(value)
        emit(HumanPromptResponse(gate_id, value))
