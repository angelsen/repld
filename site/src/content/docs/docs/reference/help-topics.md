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

| Variable    | Description      |
| ----------- | ---------------- |
| `_`         | Last result      |
| `__`, `___` | Previous two     |
| `_N`        | Result of cell N |

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
@every(seconds, label=None, delay=0)
def fn(): ...
```

Periodic ticker. The first tick runs immediately unless `delay=` holds it back — use that when you're watching something you just started, or the first check races its warmup and a false negative sends the ticker after something that was about to be fine. The decorated function gets a `.cancel()` method. Errors don't stop the ticker — they push an `every` channel notification with the traceback.

A ticker outlives the cell that registered it, so its output is ambient: rendered unattributed and uncapped, not charged against that cell's budget.

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
await ask(prompt, *, tab=None, default=None, timeout=None)            → str
await confirm(prompt, *, tab=None, default=None, timeout=None)        → bool
await choose(prompt, options, *, tab=None, default=None, timeout=None) → str
```

`tab=` routes the gate to a pinned tab's pill (requires `tab.pin()`); `ask` accepts it for symmetry but the pill has no text input. Without a `default`, an expired `timeout` raises `TimeoutError`.

These block the exec until a human responds. There are exactly three answering surfaces, and they race — first to resolve wins:

1. **The kernel's own pane**, if you started it with `repld` and it has a terminal.
2. **A pinned browser tab's pill UI**, for `confirm` and `choose` (the pill is a row of buttons with no text input, so an `ask` never renders there).
3. **`repld gate answer <id> <value>`** from any terminal.

The third is the one that always exists. Since the bridge spawns kernels lazily, the common kernel is headless with no pane at all — so when there's no terminal and no pill, the `awaiting_human` push carries the literal command to run:

```bash
repld gate                              # list what's pending
repld gate answer g3f2a1 yes
repld gate answer g3f2a1 deploy now     # free text, no quoting needed
```

`y`/`n`, an option name, and a 1-based option number all mean the same thing whichever surface answers. Pass `timeout=` to stop a gate parking a cell indefinitely.

Gates are deliberately **not** MCP tools — an agent able to answer its own `confirm()` defeats the primitive.

## Channel notification kinds

| Kind                                                                           | Source                                                    |
| ------------------------------------------------------------------------------ | --------------------------------------------------------- |
| `task_done`                                                                    | exec or defer finished                                    |
| `every`                                                                        | periodic tick result or error (`label` in meta)           |
| `awaiting_human`                                                               | ask/confirm/choose pending                                |
| `bg_task_error`                                                                | uncaught exception in background task                     |
| `loop_blocked`                                                                 | asyncio loop blocked > 5s                                 |
| `loop_kill`                                                                    | watchdog cancelled a stuck task                           |
| `init_loaded`                                                                  | `repld_init.py` ran at boot — `__main__` is pre-populated |
| `init_error`                                                                   | `repld_init.py` raised                                    |
| `venv`                                                                         | a project venv was adopted onto the running kernel        |
| `console_error`                                                                | `console.error` or uncaught exception from a watched tab  |
| `pin_lost`                                                                     | a pinned tab navigated cross-origin                       |
| `controls`                                                                     | `window.controls` action observation                      |
| `browser_connect` / `browser_disconnect` / `browser_watch` / `browser_unwatch` | dashboard browser actions                                 |

A bare `notify("...")` carries **no** kind at all — meta is whatever keywords you passed. Pass `kind=` yourself if you want to filter on it.

A task's completion is pushed to the session that started it; ambient output (`@every`, console errors, browser connect/disconnect, bare `notify()`) is broadcast.

## get_task / cancel

```python
get_task(task_id) → {done, text, spill_path, ...}
cancel(task_id)   → {cancelled: bool}
```

`cancel` only works on `await`-yielding code — tight sync loops (`while True: pass`) can't be preempted.
