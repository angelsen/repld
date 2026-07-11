---
title: Exec & channels
description: exec, defer, every, notify, ask/confirm/choose, and channel push.
slug: docs/reference/exec
---

## exec

```python
exec(code, timeout=2.0)
```

Execute Python in the shared `__main__`. Returns inline within timeout; otherwise returns `{task_id, done: false}` and pushes a channel notification on completion.

Output spills to `$XDG_RUNTIME_DIR/repld/{pid}-{tid}.out` from byte 1. The inline response carries a head+tail preview and the spill path.

### Result history

| Variable | Description |
|----------|-------------|
| `_` | Last result |
| `__`, `___` | Previous two |
| `_N` | Result of cell N |

Top-level `await` is supported.

## no_display

```python
no_display(value) → value
```

Return a value from a cell without the auto-display hook re-printing it — still binds `_`/`_N`, and still unwraps on direct assignment (`x = no_display(await foo())`). For functions that already print their own output.

## defer

```python
defer(coro, label=None) → task_id
```

Fire-and-forget. The coroutine runs in the background; a `task_done` channel notification pushes on completion. Visible to `get_task` and `cancel`.

## @every

```python
@every(seconds, label=None)
def fn(): ...
```

Periodic ticker. First tick runs immediately. The decorated function gets a `.cancel()` method. Errors don't stop the ticker — they push an `every` channel notification with the traceback.

```python
every.list()        # active EveryHandles
every.cancel_all()  # stop all tickers
```

## notify

```python
notify(content, **meta)
```

Push a `user` channel notification to the agent. Metadata appears as extra fields in the notification payload.

## Human gates

```python
answer = ask(prompt)                    # free-form text input
ok = confirm(prompt)                    # yes/no → bool
choice = choose(prompt, options)        # pick one → str
```

These block the exec until a human responds. If a browser tab is pinned, gates route to the pill UI — terminal and browser resolve the same Future, first wins.

## Channel notification kinds

| Kind | Source |
|------|--------|
| `task_done` | exec or defer finished |
| `user` | `notify()` from user code |
| `every` | periodic tick result or error |
| `awaiting_human` | ask/confirm/choose pending |
| `bg_task_error` | uncaught exception in background task |
| `loop_blocked` | asyncio loop blocked > 5s |
| `loop_kill` | watchdog cancelled a stuck task |
| `init_error` | `--init` file failed |

## get_task / cancel

```python
get_task(task_id) → {done, text, spill_path, ...}
cancel(task_id)   → {cancelled: bool}
```

`cancel` only works on `await`-yielding code — tight sync loops (`while True: pass`) can't be preempted.
