"""Event types and module-level queue for the display layer.

All display-bound output flows through a single bounded queue of typed events.
The queue is initialized by `init_event_queue()` (called from kernel.run_kernel
before anything else). `emit()` is safe to call before init — events are
buffered in `_pre_init_buf` and flushed on first init.
"""

import queue
import threading
import time
from dataclasses import dataclass
from typing import Literal

# ---------------------------------------------------------------------------
# Event types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CellStart:
    task_id: str
    source: str
    t: float


@dataclass(frozen=True, slots=True)
class StdoutChunk:
    task_id: str | None  # None when outside any task context
    text: str


@dataclass(frozen=True, slots=True)
class StderrChunk:
    task_id: str | None
    text: str


@dataclass(frozen=True, slots=True)
class CellDone:
    task_id: str
    elapsed_ms: float
    error: str | None


@dataclass(frozen=True, slots=True)
class ChannelPush:
    content: str
    meta: dict[str, str]


@dataclass(frozen=True, slots=True)
class HumanPromptOpen:
    gate_id: str
    kind: Literal["ask", "confirm", "choose"]
    prompt: str
    options: list[str] | None  # for choose


@dataclass(frozen=True, slots=True)
class HumanPromptResponse:
    gate_id: str
    value: str | bool


@dataclass(frozen=True, slots=True)
class BrowserTabAttached:
    target: str
    url: str
    title: str


@dataclass(frozen=True, slots=True)
class BrowserTabDetached:
    target: str


Event = (
    CellStart
    | StdoutChunk
    | StderrChunk
    | CellDone
    | ChannelPush
    | HumanPromptOpen
    | HumanPromptResponse
    | BrowserTabAttached
    | BrowserTabDetached
)

# ---------------------------------------------------------------------------
# Queue singleton
# ---------------------------------------------------------------------------

_MAXSIZE = 10_000
_queue: "queue.Queue[Event] | None" = None
_pre_init_buf: list[Event] = []
_pre_init_lock = threading.Lock()

# Drop counter + warning throttle
_drop_count = 0
_last_drop_warn: float = 0.0
_DROP_WARN_INTERVAL = 10.0


def init_event_queue(maxsize: int = _MAXSIZE) -> None:
    """Create the module-level queue. Must be called before the kernel starts
    accepting IPC connections. Flushes any pre-init buffered events."""
    global _queue, _pre_init_buf
    q: queue.Queue[Event] = queue.Queue(maxsize=maxsize)
    with _pre_init_lock:
        buf = _pre_init_buf
        _pre_init_buf = []
        _queue = q
    for ev in buf:
        _put_nonblocking(q, ev)


def get_queue() -> "queue.Queue[Event]":
    """Return the live queue. Raises RuntimeError if not yet initialized."""
    if _queue is None:
        raise RuntimeError("events.init_event_queue() has not been called")
    return _queue


def emit(ev: Event) -> None:
    """Emit an event. Non-blocking.

    Before `init_event_queue()` is called the event is buffered in-process
    and flushed when the queue is created. After init, if the queue is full
    the oldest event is dropped and a drop-count warning is scheduled.
    """
    q = _queue
    if q is None:
        with _pre_init_lock:
            if _queue is None:
                _pre_init_buf.append(ev)
                return
            q = _queue

    _put_nonblocking(q, ev)


def _put_nonblocking(q: "queue.Queue[Event]", ev: Event) -> None:
    global _drop_count, _last_drop_warn
    try:
        q.put_nowait(ev)
    except queue.Full:
        # Drop oldest to make room, then enqueue the new event.
        try:
            q.get_nowait()
        except queue.Empty:
            pass
        _drop_count += 1
        now = time.monotonic()
        if now - _last_drop_warn >= _DROP_WARN_INTERVAL:
            _last_drop_warn = now
            n = _drop_count
            _drop_count = 0
            warn = StderrChunk(
                None,
                f"[repld] display queue full, dropped {n} event(s)\n",
            )
            try:
                q.put_nowait(warn)
            except queue.Full:
                pass
        try:
            q.put_nowait(ev)
        except queue.Full:
            pass
