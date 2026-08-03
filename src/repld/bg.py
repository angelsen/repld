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
# and cannot grow without bound. `set.add`/`set.discard` are atomic under the
# GIL, which matters because some callers (`Tab`'s property setters) run off the
# loop thread.
_running: set[asyncio.Task] = set()


def spawn(
    coro: Coroutine[Any, Any, Any],
    *,
    name: str | None = None,
    loop: asyncio.AbstractEventLoop | None = None,
) -> asyncio.Task:
    """Schedule *coro* and keep a strong reference until it finishes.

    Pass *loop* when the caller is not itself running on it — `Tab`'s sync
    property setters reach the kernel loop that way, and `asyncio.create_task`
    would have no running loop to find.
    """
    task = (
        loop.create_task(coro, name=name)
        if loop is not None
        else asyncio.create_task(coro, name=name)
    )
    _running.add(task)
    task.add_done_callback(_running.discard)
    return task


def count() -> int:
    """In-flight fire-and-forget tasks. For diagnostics."""
    return len(_running)
