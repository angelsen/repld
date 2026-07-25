"""Event types, the display queue, and the event-log sink.

`emit()` fans one typed event out to two independent destinations:

  - the **sink**, a callback registered by `eventlog.install()` that writes the
    event to disk. Always runs, and runs first.
  - the **queue**, a bounded `queue.Queue` drained by the live TUI.

The queue exists only for the TUI. A headless kernel (`--no-display`, which is
how every auto-spawned kernel runs) has no consumer for it, so `run_kernel`
calls `disable_queue()` instead of `init_event_queue()` and `emit()` returns
after the sink. See `disable_queue` for why the queue can't simply be left
unread.

`emit()` is safe to call before either — events are buffered in
`_pre_init_buf` and flushed on init, or dropped if the queue is disabled.
"""

import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable, Literal

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
# Set once at boot by disable_queue(), never cleared. Distinguishes "no queue
# because nothing will ever read one" from "no queue *yet*" — both have
# _queue is None, but the first drops and the second buffers.
_disabled = False

# Optional second consumer, independent of whoever drains the queue.
# eventlog.install() registers itself here; keeping it a registered callback
# (rather than importing eventlog) is what lets eventlog import display.
_sink: "Callable[[Event], None] | None" = None

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


def disable_queue() -> None:
    """Run without a display queue: `emit()` stops after the sink.

    Called instead of `init_event_queue()` when nothing will consume the
    queue — i.e. `--no-display`, which is how every auto-spawned kernel runs.
    One-way and boot-only; the queue can't be turned back on.

    The alternative — initializing a queue and draining it into nothing — is
    what this replaces, and it is not something the drain thread was free to
    stop doing: an unread queue pins at `_MAXSIZE`, after which every `emit()`
    takes the drop-oldest path below, which is strictly more work per event
    than draining. The queue is the redundant part, not the drain.

    Drops anything buffered pre-init: it was only ever headed for the TUI, and
    the sink has already written it to the event log.
    """
    global _disabled
    with _pre_init_lock:
        _disabled = True
        _pre_init_buf.clear()


def get_queue() -> "queue.Queue[Event]":
    """Return the live queue. Raises RuntimeError if not yet initialized."""
    if _queue is None:
        raise RuntimeError("events.init_event_queue() has not been called")
    return _queue


def set_sink(fn: "Callable[[Event], None] | None") -> None:
    """Register (or clear) a tee that sees every event as it is emitted."""
    global _sink
    _sink = fn


def emit(ev: Event) -> None:
    """Emit an event. Non-blocking.

    Before `init_event_queue()` is called the event is buffered in-process
    and flushed when the queue is created. After init, if the queue is full
    the oldest event is dropped and a drop-count warning is scheduled. After
    `disable_queue()` the queue half is skipped entirely.

    The registered sink (the event log) sees the event regardless — it is
    not subject to the queue's drop-oldest policy, and runs first so that a
    kernel with no queue at all still has a complete history on disk.
    """
    sink = _sink
    if sink is not None:
        try:
            sink(ev)
        except Exception:
            pass  # a broken log must never break the kernel

    # Unlocked read: _disabled only ever goes False → True, once, during boot.
    # A stale False costs one lock acquisition and is caught below.
    if _disabled:
        return

    q = _queue
    if q is None:
        with _pre_init_lock:
            if _disabled:
                return
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
