"""Strong references for fire-and-forget asyncio tasks.

`asyncio` holds only a *weak* reference to a running task, so a task nothing
else refers to can be garbage-collected mid-flight — the stdlib documents this
under `asyncio.create_task` and it is the reason every call site that does not
keep the handle needs somewhere to put it.

The failures it produces are silent and unattributable, which is what makes it
worth a module rather than a comment at each site. The kernel's fire-and-forget
work is all of the "did the browser just not do that?" kind:

  * `CDPSession._enable_domains` — a tab attaches and then records no network
    or console events at all.
  * `BrowserSession._auto_attach` — a `watch()` pattern silently skips a tab.
  * the `Fetch.requestPaused` handler — the worst one. A paused request is
    resumed *only* from inside that coroutine, so losing it leaves the request
    hanging in Chrome until it times out, and `settle` waits on it.
  * `Tab._show_gate` — a human gate renders no pill, on a kernel where the
    pill may be the only surface that could have answered it.

Its own module for the reason `channel.py` is: four modules across both halves
of the package need it, it depends on nothing but the stdlib, and the
alternative is each of them growing a private keepalive set.

Deliberately *not* a place to handle exceptions. The done-callback discards and
nothing more, so a task that raises still surfaces the usual "Task exception was
never retrieved" — the same as before, except it can no longer be swallowed by
the task vanishing first.
"""

import asyncio
from collections.abc import Coroutine
from typing import Any

# Discarded by the done-callback, so this holds only genuinely in-flight tasks
# and cannot grow without bound. Touched from the loop thread only: the
# `loop=` path defers `_track` onto the loop along with the task creation it
# guards, and the `loop=None` path is on the loop by construction (there is no
# `asyncio.create_task` without a running one).
_running: set[asyncio.Task] = set()


def _track(task: asyncio.Task) -> asyncio.Task:
    _running.add(task)
    task.add_done_callback(_running.discard)
    return task


def spawn(
    coro: Coroutine[Any, Any, Any],
    *,
    name: str | None = None,
    loop: asyncio.AbstractEventLoop | None = None,
) -> "asyncio.Task | None":
    """Schedule *coro* and keep a strong reference until it finishes.

    Pass *loop* when the caller is not itself running on it — `Tab`'s sync
    property setters reach the kernel loop that way, and `asyncio.create_task`
    would have no running loop to find. That path returns None: the task does
    not exist yet on this thread, and it must not, for the reason below.

    **A task has to be created on the loop's own thread.** `loop.create_task`
    from anywhere else appends the first `__step` handle to the loop's ready
    queue without the `_write_to_self()` that `call_soon_threadsafe` does, so
    the loop is never woken — on an otherwise idle loop the coroutine simply
    does not run. It looked like it worked here only because the watchdog
    probes the loop once a second and every wakeup drains the queue, i.e. every
    off-loop `tab.label = …` / `tab.capture_bodies = …` was riding on an
    unrelated thread's timer for its liveness. It also trips `_check_thread`'s
    outright RuntimeError under `PYTHONASYNCIODEBUG=1`.

    `call_soon_threadsafe` rather than `run_coroutine_threadsafe`, which would
    otherwise be the obvious answer: it gives the task no name, and an unnamed
    loop task is exactly what `kernel._pick_victim` treats as fair game when
    the watchdog escalates. Every task spawned here is named `repld-…` so it is
    excluded; going through `ensure_future` would hand the watchdog our own
    fire-and-forget work as the thing to cancel.
    """
    if loop is None:
        return _track(asyncio.create_task(coro, name=name))
    loop.call_soon_threadsafe(lambda: _track(loop.create_task(coro, name=name)))
    return None


def count() -> int:
    """In-flight fire-and-forget tasks. For diagnostics."""
    return len(_running)
