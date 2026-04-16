"""Human-gate primitives: ask, confirm, choose.

Gates pause user code (running on the bg asyncio loop) until a human answers
via the terminal stdin reader. They work by parking on a concurrent.futures.Future
with `await loop.run_in_executor(None, fut.result, timeout)` — so the loop
stays responsive for other tasks while waiting.

The gate registry maps gate_id → Future. The display thread's stdin reader
calls `resolve_gate()` when the human types a response.
"""

from __future__ import annotations

import concurrent.futures
import threading
import uuid

from .events import ChannelPush, HumanPromptOpen, HumanPromptResponse, emit

_gates: dict[str, concurrent.futures.Future] = {}
_gates_lock = threading.Lock()


def ask(
    prompt: str,
    *,
    default: str | None = None,
    timeout: float | None = None,
) -> str:
    """Prompt the human for a free-form string response."""
    return _gate("ask", prompt, None, default, timeout)  # type: ignore[return-value]


def confirm(
    prompt: str,
    *,
    default: bool | None = None,
    timeout: float | None = None,
) -> bool:
    """Prompt the human for a yes/no response. Returns bool."""
    value = _gate("confirm", prompt, None, default, timeout)
    return bool(value)


def choose(
    prompt: str,
    options: list[str],
    *,
    default: str | None = None,
    timeout: float | None = None,
) -> str:
    """Prompt the human to choose one of the given options."""
    return _gate("choose", prompt, options, default, timeout)  # type: ignore[return-value]


def _gate(kind, prompt, options, default, timeout):
    gate_id = uuid.uuid4().hex[:8]
    fut: concurrent.futures.Future = concurrent.futures.Future()
    with _gates_lock:
        _gates[gate_id] = fut

    emit(HumanPromptOpen(gate_id, kind, prompt, options))
    emit(
        ChannelPush(
            content=f"awaiting human: {prompt}",
            meta={
                "kind": "awaiting_human",
                "gate_id": gate_id,
                "prompt_kind": kind,
            },
        )
    )

    try:
        return fut.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
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
