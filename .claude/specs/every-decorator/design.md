# Design: `@every(seconds)` + watchdog escalation

## Architecture Overview

Two changes, both small:

1. **`every()` factory in `kernel.py`** — a `_make_every(loop)` factory
   (same pattern as `_make_defer(loop)`) that returns the decorator. The
   decorator schedules a ticker coroutine on the kernel's shared loop via
   `asyncio.run_coroutine_threadsafe`. No new file — lives in `kernel.py`
   alongside `defer`/`notify`.

2. **Watchdog escalation in `_loop_watchdog`** — after the existing warn
   at `REPLD_LOOP_BLOCK_THRESHOLD` (5s), a second threshold
   `REPLD_LOOP_KILL_THRESHOLD` (default 30s) cancels the longest-running
   asyncio task. The 300s passive wait becomes an active kill attempt.

## Component Changes

### `src/repld/kernel.py` — `_make_every` + handle registry

```python
@dataclass
class EveryHandle:
    label: str
    seconds: float
    _task: asyncio.Task

    def cancel(self) -> None:
        self._task.cancel()
        _every_registry.discard(self)

    def __repr__(self) -> str:
        return f"<every {self.seconds}s: {self.label}>"

_every_registry: set[EveryHandle] = set()


def _make_every(loop: asyncio.AbstractEventLoop):

    async def _ticker(fn, seconds, handle_box):
        handle = handle_box[0]
        while True:
            try:
                result = fn()
                if inspect.iscoroutine(result):
                    result = await result
            except asyncio.CancelledError:
                _every_registry.discard(handle)
                raise
            except Exception as exc:
                push_channel(
                    f"@every {handle.label}: {type(exc).__name__}: {exc}",
                    {"kind": "every", "label": handle.label, "error": "1"},
                )
            else:
                if result is not None:
                    push_channel(
                        str(result),
                        {"kind": "every", "label": handle.label},
                    )
            await asyncio.sleep(seconds)

    def every(seconds: float, *, label: str | None = None):
        def decorator(fn):
            name = label or fn.__name__
            handle_box: list = [None]  # mutable box for task reference
            fut = asyncio.run_coroutine_threadsafe(
                _ticker(fn, seconds, handle_box), loop
            )
            task = asyncio.wrap_future(fut)  # not needed — see below
            # We need the actual asyncio.Task, not the future. Schedule
            # a coroutine that captures it.
            ...
            # Simpler: schedule directly and let the loop give us the Task.
        return decorator

    every.list = lambda: list(_every_registry)
    every.cancel_all = lambda: [h.cancel() for h in list(_every_registry)]

    return every
```

The tricky bit: `asyncio.run_coroutine_threadsafe` returns a
`concurrent.futures.Future`, not an `asyncio.Task`. We need the Task for
`.cancel()`. Solution: wrap the scheduling in a helper coroutine that
captures `asyncio.current_task()`:

```python
async def _start_ticker(fn, seconds, label):
    task = asyncio.current_task()
    handle = EveryHandle(label, seconds, task)
    _every_registry.add(handle)
    fn._handle = handle
    fn.cancel = handle.cancel
    # Run first tick immediately, then loop
    while True:
        try:
            result = fn()
            if inspect.iscoroutine(result):
                result = await result
        except asyncio.CancelledError:
            _every_registry.discard(handle)
            raise
        except Exception as exc:
            push_channel(
                f"@every {label}: {type(exc).__name__}: {exc}",
                {"kind": "every", "label": label, "error": "1"},
            )
        else:
            if result is not None:
                push_channel(str(result), {"kind": "every", "label": label})
        await asyncio.sleep(seconds)

def _make_every(loop):
    def every(seconds, *, label=None):
        def decorator(fn):
            name = label or fn.__name__
            asyncio.run_coroutine_threadsafe(
                _start_ticker(fn, seconds, name), loop
            )
            return fn
        return decorator

    every.list = lambda: list(_every_registry)
    every.cancel_all = lambda: [h.cancel() for h in list(_every_registry)]
    return every
```

The `fn._handle` and `fn.cancel` attributes are set from the loop thread
once `_start_ticker` runs. There's a brief window between decorator return
and attribute assignment (one event loop tick). Acceptable — the user
won't call `.cancel()` in the same expression they define the function.

### `src/repld/kernel.py` — injection

Line ~455, after `setattr(__main__, "defer", ...)`:

```python
setattr(__main__, "every", _make_every(loop))
```

Kernel shutdown (`_shutdown`) drives a generic drain coroutine
(`_drain_loop_tasks`) that cancels and awaits every non-internal loop
task — `@every` tickers, `defer()` coroutines, in-flight exec cells —
with a 2 s budget so a stuck `finally` can't hang shutdown. Then it
stops the loop. No atexit fallback for `_every_registry` is needed:
`atexit` runs after the loop has already stopped, so it can't drive
cancellation through tasks anyway.

### `src/repld/kernel.py` — watchdog escalation

Replace the passive 300s wait with an active kill:

```python
def _loop_watchdog(loop, stop, threshold, kill_threshold, interval):
    while not stop.is_set():
        future = asyncio.run_coroutine_threadsafe(asyncio.sleep(0), loop)
        try:
            future.result(timeout=threshold)
        except concurrent.futures.TimeoutError:
            # Warn
            active = [...]
            push_channel(f"[repld] event loop blocked > {threshold}s ...", ...)

            # Escalate: wait kill_threshold, then cancel longest task
            try:
                future.result(timeout=kill_threshold - threshold)
            except concurrent.futures.TimeoutError:
                # Find and cancel the longest-running asyncio task
                all_tasks = asyncio.all_tasks(loop)
                victim = _pick_victim(all_tasks)
                if victim is not None:
                    victim.cancel()
                    push_channel(
                        f"[repld] killed blocked task: {victim.get_name()}",
                        {"kind": "loop_kill", "task": victim.get_name()},
                    )
        if stop.wait(interval):
            return
```

`_pick_victim` skips internal tasks (names starting with `repld-`) and
picks the task that has been running longest. For v1, "longest running"
is approximated by picking the task whose name matches the oldest active
cell task_id, or falling back to the first non-internal task.

`kill_threshold` defaults to `REPLD_LOOP_KILL_THRESHOLD` env (30s).

### `src/repld/help.py`

Add `every` to the exec model instructions:

```
"every(seconds)(fn) schedules fn to run periodically; "
"fn.cancel() stops it. every.list() shows active tickers."
```

Add to the `exec` topic reference:

```
every(seconds, label=)(fn)   → fn    periodic ticker; fn.cancel() stops
every.list()                 → list  active EveryHandles
every.cancel_all()           → None  stop all tickers
```

## Data Flow

```
@every(30)
def check():
    return f"load: {get_load()}"
       │
       ▼
asyncio.run_coroutine_threadsafe(_start_ticker(check, 30, "check"), loop)
       │
       ▼  (on the shared asyncio loop, immediately)
    result = check()     ──→  "load: 0.42"
       │
       ▼
    push_channel("load: 0.42", {"kind": "every", "label": "check"})
       │
       ├──→ ipc.broadcast_channel()  →  bridge  →  Claude Code <channel>
       └──→ events.emit(ChannelPush) →  display thread  →  kernel pane
       │
       ▼
    await asyncio.sleep(30)
    ... repeat ...

check.cancel()  →  task.cancel()  →  CancelledError caught  →  handle removed
```

## Method Signatures

```python
# kernel.py — public API (injected into __main__)
def every(seconds: float, *, label: str | None = None) -> Callable[[F], F]
every.list() -> list[EveryHandle]
every.cancel_all() -> None

@dataclass
class EveryHandle:
    label: str
    seconds: float
    _task: asyncio.Task
    def cancel(self) -> None: ...

# On the decorated function:
fn._handle -> EveryHandle
fn.cancel() -> None  # shortcut for fn._handle.cancel()

# kernel.py — watchdog (internal, changed signature)
def _loop_watchdog(loop, stop, threshold, kill_threshold, interval) -> None
def _pick_victim(all_tasks: set[asyncio.Task]) -> asyncio.Task | None
```
