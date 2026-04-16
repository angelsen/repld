# Design: LLM REPL

## Architecture Overview

```
┌─────────────────────── kernel process ────────────────────────┐
│                                                                │
│  main thread                         daemon thread             │
│  ┌──────────────────┐                ┌────────────────────┐    │
│  │ display consumer │   events       │ asyncio loop       │    │
│  │  (pops queue,    │◀─ queue ───────│  - user cells      │    │
│  │   renders to     │   (bounded)    │  - bg tasks        │    │
│  │   __stdout__)    │                │  - notify(), etc.  │    │
│  │                  │                │                    │    │
│  │ also: reads      │                │ IPC accept thread  │    │
│  │   stdin for      │                │  + per-conn reader │    │
│  │   human gates    │                │  (same as today)   │    │
│  └──────────────────┘                └────────────────────┘    │
│           ▲                                    ▲               │
│           │ HumanPromptResponse                │ tool calls    │
│           ▼                                    ▼               │
│       ┌────────────────────────────────────────────┐           │
│       │         gate registry (threading)          │           │
│       │  { prompt_id → Future (for async waiters)} │           │
│       └────────────────────────────────────────────┘           │
│                                                                │
└────────────────────────────────────────────────────────────────┘
                           ▲
                           │ unix socket (NDJSON, unchanged)
                           ▼
                 ┌──────────────────┐
                 │  repld bridge    │  (unchanged — dumb byte-pipe)
                 │  stdio subprocess │
                 └──────────────────┘
                           ▲
                           │ stdio MCP
                           ▼
                   Claude Code client
```

Key inversion from the IPython port: the asyncio loop moves back to a **daemon thread** (like the prototype). Main thread becomes a display consumer + stdin reader for gates. This kills three problems at once: `asyncio.create_task` from sync user code works (loop always running), `patch_stdout` never exists (no prompt_toolkit), and the ContextVar attribution is never disrupted (no deferred flushing).

## Component Changes

### Deleted

- `src/repld/kernel.py` → rewritten (IPython setup, `_run_cell_outer` sys.stdout swap, `_tee_stdout` stashing, `_run_init_file` via `loop.run_until_complete`, signal plumbing for mainloop mode — all gone)
- IPython dep from `pyproject.toml`
- IPython-aware bits in `tasks.py` (the `_tee_stdout`/`_tee_stderr` module-level references used by the sys.stdout swap)

### Rewritten

- `src/repld/kernel.py` — bg-thread loop, display thread, gate registry, stdin reader. ~180 LOC.
- `src/repld/tasks.py` — `_Tee` emits events to the display queue instead of appending to a per-task buffer directly. Task buffer now reconstructs from events. ~140 LOC.
- `src/repld/cli.py` — add `--no-display` flag (for CI: skip display thread, just run loop + IPC); `--init` semantics unchanged but executes via our own compile/eval path.

### New

- `src/repld/events.py` — event dataclasses and queue. ~80 LOC.
- `src/repld/runtime.py` — compile + eval for user cells with top-level await (port of prototype `bootstrap.py:107-142`). ~60 LOC.
- `src/repld/display.py` — the display thread; `rich` if available, stdlib fallback. ~180 LOC.
- `src/repld/gates.py` — `ask`/`confirm`/`choose` primitives + gate registry. ~100 LOC.

### Unchanged

- `src/repld/ipc.py` — unchanged.
- `src/repld/bridge.py` — unchanged.
- `src/repld/protocol.py` — `Dispatcher` unchanged; `KernelContext` protocol unchanged. What changes is what `start_task` / `snapshot` return, but the shapes match.
- `tests/smoketest.py` — should run unchanged (all MCP-surface behaviors preserved).

## Data Flow

### Cell execution
1. IPC accept thread receives `tools/call exec {code, timeout}` request, calls `Dispatcher.handle` (on that thread).
2. `Dispatcher._exec` calls `ctx.start_task(code)` which:
   - Creates a new task record (task_id + done_event + buf list).
   - Emits `CellStart(task_id, source=code, t=now())` to the event queue.
   - Submits `_run_cell(task_id, code)` via `run_coroutine_threadsafe` to the bg loop.
3. `_run_cell` coroutine (runs on bg loop):
   - `_current_task.set(task_id)` (ContextVar inside coroutine → copy_context propagates to spawned tasks).
   - Runs compiled code; if coroutine, awaits it.
   - Captures exceptions, converts to traceback string, emits `StderrChunk`.
   - Emits `CellDone(task_id, elapsed, error?)`.
4. User code's `print(x)` → `sys.stdout` is `_Tee` → `_Tee.write`:
   - Reads `_current_task` ContextVar.
   - Emits `StdoutChunk(task_id, text)` to queue.
   - Appends `text` to `tasks._tasks[task_id]["buf"]` (for sync `exec` return + spill to disk).
5. `Dispatcher._exec` waits on `done_event.wait(timeout)`.
   - If set in time: returns `snapshot(task_id)` as inline response.
   - If not: `mark_nudged(task_id)` + returns nudge response. Later, `_maybe_push_done(task_id)` inside `_run_cell`'s finally block pushes the channel notification.

### Display
1. Display thread runs `while not _stop: ev = queue.get(timeout=0.5); _render(ev)`.
2. `_render` dispatches on event type:
   - `CellStart`: print cell header (task_id short hash, time, dim color), followed by indented source (`rich.Syntax` if available, else plain).
   - `SourceLine`: for streaming source (not yet used; reserved).
   - `StdoutChunk(task_id, text)`: if `task_id == _foreground_task_id`, print text directly; else prefix each line with `[<task_id>] `.
   - `StderrChunk`: same but red/yellow.
   - `CellDone(task_id, elapsed, error)`: print `✓ <id> · done · Xms` or `✗ <id> · err · Xms` footer; update `_foreground_task_id` (pop if this was foreground).
   - `ChannelPush(content, meta)`: print a bordered block showing `content` + a dim one-line `meta` summary.
   - `HumanPromptOpen(prompt_id, kind, prompt, options)`: print the gate banner and set `_awaiting_gate = prompt_id`; the stdin reader picks up the response.
   - `HumanPromptResponse(prompt_id, value)`: clear `_awaiting_gate`; events that queued during the gate now flush naturally from the queue.
3. Bounded queue: `queue.Queue(maxsize=10000)`. If full, drop oldest + emit a one-time warning event.

### Human gates
1. User code calls `confirm("ok?")` (sync, on bg loop).
2. `confirm` creates a `gate_id`, registers a `concurrent.futures.Future` in the gate registry, emits `HumanPromptOpen(gate_id, "confirm", "ok?", None)`, emits a sister `ChannelPush` with `meta={"kind":"awaiting_human","gate_id":gate_id,"prompt":"ok?"}`.
3. Blocks on `future.result()` (sync from bg-loop coroutine is fine because the wait happens on a worker thread — we actually `await loop.run_in_executor(None, future.result)` to not block the loop).
4. Display thread shows the prompt; stdin reader thread (main-thread companion) reads a line from stdin.
5. Stdin reader parses the response per gate kind (`y`/`n` for confirm, integer for choose, raw for ask), calls `future.set_result(value)`, emits `HumanPromptResponse(gate_id, value)`.
6. `confirm` returns the value to user code.

### --no-display mode
Skip step 2 entirely — don't start the display thread, don't read stdin. Kernel is headless: IPC + bg loop only. Event queue drains via a drop-on-full drainer thread so memory doesn't grow. This is the CI/smoketest mode.

## Method Signatures

### `src/repld/events.py`

```python
from dataclasses import dataclass
from typing import Literal

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

Event = CellStart | StdoutChunk | StderrChunk | CellDone | ChannelPush | HumanPromptOpen | HumanPromptResponse

# Module-level singleton queue (kernel creates, all other modules import)
EVENT_QUEUE: "queue.Queue[Event]"  # set by kernel.init_event_queue()

def emit(ev: Event) -> None: ...
```

### `src/repld/runtime.py`

```python
import ast
import asyncio
import inspect
from typing import Any

def compile_cell(src: str, task_id: str) -> tuple[Any, bool]:
    """Compile user code with top-level-await support.

    Returns (code_object, is_expression). `is_expression` is True when the
    single parsed statement is an Expression — in that case the caller should
    eval and print repr(value) after execution for the last-expr repr feature.
    """
    # Try as a single expression first (enables last-expr repr).
    try:
        code = compile(src, f"<repld:{task_id}>", "eval",
                       flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
        return code, True
    except SyntaxError:
        code = compile(src, f"<repld:{task_id}>", "exec",
                       flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
        return code, False


async def run_cell(code: Any, is_expression: bool, ns: dict) -> Any:
    """Run compiled cell on the current (bg) loop. If its result is a
    coroutine (from PyCF_ALLOW_TOP_LEVEL_AWAIT), await it. Returns the final
    value for expression cells, None for statement cells."""
    ...
```

### `src/repld/tasks.py`

```python
# _Tee.write no longer directly appends to task["buf"]; it emits events AND
# appends to buf. The buf remains the source of truth for inline `exec`
# responses and spill; events are for the display layer.

class _Tee(io.TextIOBase):
    def __init__(self, real, stream: Literal["stdout", "stderr"]):
        self.real = real
        self.stream = stream

    def write(self, s):
        if not s:
            return 0
        task_id = _current_task.get()
        task = _tasks.get(task_id) if task_id else None
        if task is not None:
            if not task["spilled"]:
                task["buf"].append(s)
                if sum(len(x) for x in task["buf"]) > INLINE_CAP:
                    _spill_to_disk(task, task_id)
            else:
                task["spill_file"].write(s); task["spill_file"].flush()
        # Emit event even when no task (e.g., module-level prints on startup).
        # Display thread handles None task_id by showing unprefixed.
        cls = StdoutChunk if self.stream == "stdout" else StderrChunk
        emit(cls(task_id, s))
        return len(s)
```

Note: `_Tee.write` no longer writes to `self.real`. The display thread owns `sys.__stdout__`. This is the clean break — user code writing to "stdout" goes to the event queue, not to the tty directly.

### `src/repld/gates.py`

```python
import concurrent.futures
import threading
import uuid

_gates: dict[str, concurrent.futures.Future] = {}
_gates_lock = threading.Lock()


def ask(prompt: str, *, default: str | None = None, timeout: float | None = None) -> str:
    return _gate("ask", prompt, None, default, timeout)

def confirm(prompt: str, *, default: bool | None = None, timeout: float | None = None) -> bool:
    value = _gate("confirm", prompt, None, default, timeout)
    return bool(value)

def choose(prompt: str, options: list[str], *, default: str | None = None,
           timeout: float | None = None) -> str:
    return _gate("choose", prompt, options, default, timeout)


def _gate(kind, prompt, options, default, timeout):
    gate_id = uuid.uuid4().hex[:8]
    fut: concurrent.futures.Future = concurrent.futures.Future()
    with _gates_lock:
        _gates[gate_id] = fut
    emit(HumanPromptOpen(gate_id, kind, prompt, options))
    emit(ChannelPush(
        content=f"awaiting human: {prompt}",
        meta={"kind": "awaiting_human", "gate_id": gate_id, "prompt_kind": kind},
    ))
    try:
        return fut.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        with _gates_lock:
            _gates.pop(gate_id, None)
        if default is not None:
            return default
        raise TimeoutError(f"no response to {prompt!r} within {timeout}s")
    finally:
        with _gates_lock:
            _gates.pop(gate_id, None)


def resolve_gate(gate_id: str, value) -> None:
    """Called by stdin reader when human responds."""
    with _gates_lock:
        fut = _gates.get(gate_id)
    if fut is not None and not fut.done():
        fut.set_result(value)
        emit(HumanPromptResponse(gate_id, value))
```

### `src/repld/display.py`

```python
def run_display(stop: threading.Event) -> None:
    """Main-thread loop. Pop events, format, print. Also drive stdin reader
    for human gates."""
    ...

def _render(ev: Event) -> None:
    # dispatches on isinstance; uses rich.Console if available
    ...
```

### `src/repld/kernel.py` (new shape)

```python
def run_kernel(socket_path: str | None = None, *, display: bool = True,
               init_file: str | None = None) -> int:
    _check_existing_kernel()

    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True,
                     name="repld-asyncio").start()

    events.init_event_queue()
    tasks.install_tee()

    # Inject helpers into __main__
    __main__.notify = _notify
    __main__.ask = gates.ask
    __main__.confirm = gates.confirm
    __main__.choose = gates.choose

    ctx = _Context(loop)
    dispatcher = Dispatcher(ctx)
    ipc.start_server(sock_path, lambda req, sess: dispatcher.handle(req, sess))
    _write_lockfile(sock_path)

    if init_file:
        _run_init_file(Path(init_file), loop)

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    if display:
        display.run_display(stop)   # blocks on main thread
    else:
        stop.wait()                  # headless mode

    _shutdown(loop)
    return 0
```

## Exploration Findings

Channel wire shape (per agent check against `channels-reference.md`):
- `content: string` (required), `meta: Record<string, string>` (optional)
- Meta keys with hyphens are silently dropped — all our meta keys already use underscores ✓
- One-way server→client; no built-in response path. Scoped out: no `respond_to_prompt` tool in this spec; `ChannelPush` for `awaiting_human` is informational only.

Current `experimental.claude/channel` + `experimental.claude/channel/permission` capability declarations are the canonical form — preserved as-is.

## Error Handling

- User code exceptions: converted to `traceback.format_exc()`, emitted as `StderrChunk`, `CellDone.error` set to the exception's type name (e.g., `"NameError"`).
- Compile errors: caught during `compile_cell`, emitted as `StderrChunk` + `CellDone(error="SyntaxError")` immediately; no coroutine is submitted.
- Bounded queue full: drop oldest, emit a single `StderrChunk(None, "[repld] display queue full, dropped N events\n")` warning per 10s.
- Stdin reader EOF (terminal closed mid-gate): `resolve_gate(gate_id, default_value_or_raise)`. Display thread keeps running until process signals.
