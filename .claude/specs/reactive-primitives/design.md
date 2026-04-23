# Design: Pluggable Gate Resolution + Reactive Primitives

## Architecture Overview

Two changes to the kernel, both feeding through the existing `push_channel()` path:

1. **Gate queue** — `gates.py` becomes the gate registry. The `_gates` dict already exists; we add `pending_gates()` as the public query API. The `_awaiting_gate` singleton in `display.py` is replaced with a loop that drains `pending_gates()` one at a time. No new files.

2. **Reactive module** — new `src/repld/reactive.py` with `@every`, `@watch`, `@webhook`. Each decorator returns a `Handle` with `.cancel()`. All three register asyncio tasks on the kernel's shared loop. The `@webhook` server is a minimal `asyncio.StreamServer` with HTTP/1.1 parsing — no framework.

Both are injected into `__main__` alongside `notify`, `defer`, `ask`, `confirm`, `choose`.

## Component Changes

### `src/repld/gates.py`

Add `pending_gates()`:

```python
def pending_gates() -> list[dict]:
    """Return all unresolved gates as dicts. Any resolver can read this."""
    with _gates_lock:
        return [
            {"gate_id": gid, **_gate_meta[gid]}
            for gid in _gates
            if not _gates[gid].done()
        ]
```

Add `_gate_meta: dict[str, dict]` alongside `_gates` — stores `kind`, `prompt`, `options`, `created_at` for each gate so resolvers have context. Populated in `_gate()`, cleaned up in `finally`.

`resolve_gate()` is unchanged — first caller wins.

### `src/repld/display.py`

Replace the `_awaiting_gate` / `_awaiting_gate_kind` singleton with a loop over `pending_gates()`:

```python
def _stdin_reader_loop(stop: threading.Event) -> None:
    while not stop.is_set():
        line = _STDIN.readline()
        if not line:
            break
        line = line.rstrip("\n")
        # Find the oldest pending gate.
        pending = gates.pending_gates()
        if not pending:
            continue
        gate = pending[0]
        gate_id, kind = gate["gate_id"], gate["kind"]
        # Parse and resolve (same logic as today).
        ...
```

`_render_prompt_open()` no longer sets globals — it just renders the prompt. The stdin reader independently queries `pending_gates()` when input arrives.

After resolving one gate, render the next pending gate's prompt (if any).

### `src/repld/reactive.py` (new)

```python
"""Reactive decorators: @every, @watch, @webhook.

All stdlib, all on the kernel's shared asyncio loop, all push to channel.
"""

@dataclass
class Handle:
    """Cancellable registration returned by every/watch/webhook."""
    label: str
    _task: asyncio.Task

    def cancel(self) -> None:
        self._task.cancel()
        _registry.discard(self)

_registry: set[Handle] = set()
```

**`every(seconds, *, label=None)`**

```python
def every(seconds: float, *, label: str | None = None):
    def decorator(fn):
        async def _loop():
            while True:
                await asyncio.sleep(seconds)
                try:
                    result = fn()
                    if inspect.iscoroutine(result):
                        result = await result
                except Exception as exc:
                    push_channel(f"@every error: {exc}", {"kind": "every", "label": label or fn.__name__, "error": "1"})
                    continue
                if result is not None:
                    push_channel(str(result), {"kind": "every", "label": label or fn.__name__})
        task = asyncio.run_coroutine_threadsafe(_loop(), _loop_ref)
        handle = Handle(label or fn.__name__, task)
        _registry.add(handle)
        fn._handle = handle
        return fn
    return decorator
```

**`watch(path, *, interval=1.0, label=None)`**

```python
def watch(path: str, *, interval: float = 1.0, label: str | None = None):
    def decorator(fn):
        async def _poll():
            target = Path(path).resolve()
            prev = _snapshot(target)
            while True:
                await asyncio.sleep(interval)
                curr = _snapshot(target)
                changes = _diff(prev, curr)
                if changes:
                    try:
                        result = fn(changes)
                        if inspect.iscoroutine(result):
                            result = await result
                    except Exception as exc:
                        push_channel(f"@watch error: {exc}", {"kind": "watch", "path": path, "error": "1"})
                        continue
                    if result is not None:
                        push_channel(str(result), {"kind": "watch", "path": path})
                    else:
                        push_channel(f"changed: {', '.join(c['path'] for c in changes)}", {"kind": "watch", "path": path})
                prev = curr
        # ...same handle pattern
```

`_snapshot()` walks the path with `os.scandir()`, records `{relpath: mtime}`. `_diff()` returns a list of `{"path": str, "event": "created"|"modified"|"deleted"}`.

**`webhook(route, *, methods=("POST",), label=None)`**

```python
def webhook(route: str, *, methods: tuple[str, ...] = ("POST",), label: str | None = None):
    def decorator(fn):
        _routes[route] = _Route(fn, methods, label or fn.__name__)
        _ensure_server()
        handle = Handle(label or fn.__name__, ...)
        fn._handle = handle
        return fn
    return decorator
```

The HTTP server:

```python
_server: asyncio.Server | None = None
_routes: dict[str, _Route] = {}

async def _ensure_server():
    global _server
    if _server is not None:
        return
    _server = await asyncio.start_server(_handle_connection, "127.0.0.1", 0)
    port = _server.sockets[0].getsockname()[1]
    push_channel(f"webhook server listening on http://127.0.0.1:{port}", {"kind": "webhook_server"})

async def _handle_connection(reader, writer):
    # Minimal HTTP/1.1: parse request line + headers + body.
    request_line = await reader.readline()
    method, path, _ = request_line.decode().split(" ", 2)
    headers = {}
    while True:
        line = await reader.readline()
        if line == b"\r\n":
            break
        k, v = line.decode().rstrip().split(": ", 1)
        headers[k.lower()] = v
    body = b""
    if "content-length" in headers:
        body = await reader.readexactly(int(headers["content-length"]))

    route = _routes.get(path)
    if route is None or method.upper() not in route.methods:
        writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
        writer.close()
        return

    try:
        result = route.fn(body, headers, method)
        if inspect.iscoroutine(result):
            result = await result
    except Exception as exc:
        push_channel(f"@webhook {path} error: {exc}", {"kind": "webhook", "route": path, "error": "1"})
        writer.write(b"HTTP/1.1 500 Internal Server Error\r\nContent-Length: 0\r\n\r\n")
        writer.close()
        return

    push_channel(str(result) if result else f"webhook hit: {method} {path}", {"kind": "webhook", "route": path, "method": method})
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
    await writer.drain()
    writer.close()
```

Localhost-only, ephemeral port, tears down when last route is cancelled.

### `src/repld/kernel.py`

In `run_kernel()`, after the existing helper injection block:

```python
from . import reactive
reactive.init(loop)
setattr(__main__, "every", reactive.every)
setattr(__main__, "watch", reactive.watch)
setattr(__main__, "webhook", reactive.webhook)
```

`reactive.init(loop)` stashes the loop reference so decorators can schedule tasks.

### `src/repld/help.py`

Add `gates` topic covering `pending_gates()` + `resolve_gate()`. Update `exec` topic instructions to mention `every`, `watch`, `webhook` as available builtins. Add `reactive` topic.

## Data Flow

```
@every(30) def check():     ─┐
@watch("./data") def on():   ├─→ asyncio task on shared loop
@webhook("/hook") def on():  ─┘        │
                                        ▼
                              push_channel(content, meta)
                                    │           │
                     ┌──────────────┘           └──────────────┐
                     ▼                                         ▼
          ipc.broadcast_channel()                  events.emit(ChannelPush)
          (→ bridge → MCP client)                  (→ display thread → pane)
```

Gate resolution:

```
await confirm("deploy?")
    │
    ▼
_gate() → Future + _gate_meta entry + push_channel("awaiting human: deploy?")
    │
    ├─→ stdin reader: pending_gates() → resolve_gate(id, True)
    ├─→ agent:        exec("resolve_gate('abc', True)")
    ├─→ telegram:     resolve_gate(id, True) via webhook
    └─→ web form:     resolve_gate(id, True) via webhook
         │
         ▼ (first caller wins)
    Future.set_result(True)
    awaiting coroutine resumes
```

## Method Signatures

```python
# gates.py
def pending_gates() -> list[dict]:
    """[{"gate_id": str, "kind": str, "prompt": str, "options": list|None, "created_at": float}, ...]"""

def resolve_gate(gate_id: str, value) -> None:  # unchanged

# reactive.py
def every(seconds: float, *, label: str | None = None) -> Callable:  # decorator
def watch(path: str, *, interval: float = 1.0, label: str | None = None) -> Callable:  # decorator
def webhook(route: str, *, methods: tuple[str, ...] = ("POST",), label: str | None = None) -> Callable:  # decorator

class Handle:
    label: str
    def cancel(self) -> None: ...

def init(loop: asyncio.AbstractEventLoop) -> None:  # called once from kernel
```
