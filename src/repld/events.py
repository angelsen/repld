"""Event types, the display queue, and the event-log sink.

`emit()` fans one typed event out to two independent destinations:

  - the **sink**, a callback registered by `eventlog.install()` that writes the
    event to disk. Always runs, and runs first.
  - the **queue**, a bounded `queue.Queue` drained by the live TUI.

The queue exists only for the TUI. A headless kernel (`--no-display`, which is
how every auto-spawned kernel runs) has no consumer for it, so `run_kernel`
calls `disable_queue()` instead of `init_event_queue()` and `emit()` returns
after the sink. See `disable_queue` for why the queue cannot simply be left
unread.

`emit()` is safe to call before either: events are buffered in
`_pre_init_buf` and flushed on init, or dropped if the queue is disabled.
"""

import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

# ---------------------------------------------------------------------------
# Event types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CellStart:
    """An exec cell began running."""

    task_id: str
    source: str
    t: float
    caller: dict | None = None  # a peer kernel's `peer/call` caller, else None


@dataclass(frozen=True, slots=True)
class StdoutChunk:
    """A fragment of captured stdout for a task (or ambient, when `task_id` is None)."""

    task_id: str | None  # None when outside any task context
    text: str


@dataclass(frozen=True, slots=True)
class StderrChunk:
    """A fragment of captured stderr for a task (or ambient, when `task_id` is None)."""

    task_id: str | None
    text: str


@dataclass(frozen=True, slots=True)
class CellDone:
    """An exec cell finished, with its wall time and optional error text."""

    task_id: str
    elapsed_ms: float
    error: str | None


@dataclass(frozen=True, slots=True)
class ChannelPush:
    """A channel notification delivered to one or all connected sessions."""

    content: str
    meta: dict[str, str]


@dataclass(frozen=True, slots=True)
class HumanPromptOpen:
    """A human gate opened and is waiting for an answer."""

    gate_id: str
    kind: Literal["ask", "confirm", "choose", "form"]
    prompt: str
    options: list[str] | None   # for choose
    schema: dict | None = None  # absent only in pre-schema event logs


@dataclass(frozen=True, slots=True)
class HumanPromptResponse:
    """A human gate received an answer."""

    gate_id: str
    value: str | bool


@dataclass(frozen=True, slots=True)
class HumanPromptClosed:
    """A gate that stopped waiting without anyone answering it.

    The counterpart to HumanPromptResponse, and needed for the same reason: a
    surface that mirrors open gates (the pane's `_open_gates`, a `repld log`
    replay) has no other way to learn that one is gone. `timeout=` expiring and
    the awaiting cell being cancelled both end a gate with nobody having
    answered, and without this the pane keeps routing stdin at it forever.
    """

    gate_id: str
    reason: Literal["timeout", "cancelled", "declined"]


@dataclass(frozen=True, slots=True)
class BrowserTabAttached:
    """A Chrome tab became tracked by the browser pool."""

    target: str
    url: str
    title: str


@dataclass(frozen=True, slots=True)
class BrowserTabDetached:
    """A Chrome tab left the browser pool."""

    target: str


Event = (
    CellStart
    | StdoutChunk
    | StderrChunk
    | CellDone
    | ChannelPush
    | HumanPromptOpen
    | HumanPromptResponse
    | HumanPromptClosed
    | BrowserTabAttached
    | BrowserTabDetached
)

# ---------------------------------------------------------------------------
# Queue singleton
# ---------------------------------------------------------------------------

_MAXSIZE = 10_000
_queue: queue.Queue[Event] | None = None
_pre_init_buf: list[Event] = []
_pre_init_lock = threading.Lock()
# Set once at boot by disable_queue(), never cleared. Distinguishes "no queue
# because nothing will ever read one" from "no queue yet"; both have
# _queue is None, but the first drops and the second buffers.
_disabled = False

# Optional second consumer, independent of whoever drains the queue.
# eventlog.install() registers itself here; keeping it a registered callback
# (rather than importing eventlog) is what lets eventlog import display.
_sink: Callable[[Event], None] | None = None

# Drop counter + warning throttle
_drop_count = 0
_last_drop_warn: float = 0.0
_DROP_WARN_INTERVAL = 10.0


def init_event_queue(maxsize: int = _MAXSIZE) -> None:
    """Create the module-level queue and flush any pre-init buffered events.

    Must be called before the kernel starts accepting IPC connections.
    """
    global _queue, _pre_init_buf
    q: queue.Queue[Event] = queue.Queue(maxsize=maxsize)
    with _pre_init_lock:
        buf = _pre_init_buf
        _pre_init_buf = []
        _queue = q
    for ev in buf:
        _put_nonblocking(q, ev)


def disable_queue() -> None:
    """Disable the display queue permanently: `emit()` stops after the sink.

    Called instead of `init_event_queue()` when nothing will consume the
    queue, i.e. `--no-display`, which is how every auto-spawned kernel runs.
    One-way and boot-only; the queue cannot be turned back on.

    The alternative (initializing a queue and draining it into nothing) is
    what this replaces, and the drain thread was not free to stop: an unread
    queue pins at `_MAXSIZE`, after which every `emit()` takes the drop-oldest
    path below, which is strictly more work per event than draining. The queue
    is the redundant part, not the drain.

    Drops anything buffered pre-init: it was only ever headed for the TUI, and
    the sink has already written it to the event log.
    """
    global _disabled
    with _pre_init_lock:
        _disabled = True
        _pre_init_buf.clear()


def get_queue() -> queue.Queue[Event]:
    """Return the live queue, or raise `RuntimeError` if not yet initialized."""
    if _queue is None:
        raise RuntimeError("events.init_event_queue() has not been called")
    return _queue


def set_sink(fn: Callable[[Event], None] | None) -> None:
    """Register (or clear) a tee that sees every event as it is emitted."""
    global _sink
    _sink = fn


def emit(ev: Event) -> None:
    """Fan an event to the sink and the queue, non-blocking.

    Before `init_event_queue()` is called the event is buffered in-process
    and flushed when the queue is created. After init, if the queue is full
    the oldest event is dropped and a drop-count warning is scheduled. After
    `disable_queue()` the queue half is skipped entirely.

    The registered sink (the event log) sees the event regardless: it is
    not subject to the queue's drop-oldest policy, and runs first so that a
    kernel with no queue at all still has a complete history on disk.
    """
    sink = _sink
    if sink is not None:
        try:
            sink(ev)
        except Exception:
            pass  # a broken log must never break the kernel

    # Unlocked read: _disabled only transitions from False to True, once,
    # during boot. A stale False costs one lock acquisition and is caught below.
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


def _put_nonblocking(q: queue.Queue[Event], ev: Event) -> None:
    """Enqueue `ev`, dropping the oldest entry if the queue is full."""
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
