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
@every(seconds, label=None, delay=0, tab=None)
def fn(): ...
```

Periodic ticker. The first tick runs immediately unless `delay=` holds it back — use that when you're watching something you just started, or the first check races its warmup and a false negative sends the ticker after something that was about to be fine. The decorated function gets a `.cancel()` method. Errors don't stop the ticker — they push an `every` channel notification with the traceback.

`tab=<pattern>` resolves `browser.get(pattern)` fresh on every tick and passes the live `Tab` to `fn`, so a ticker acting on a browser tab across hours or days doesn't need a captured-once `Tab` that goes stale across a navigation or crash. A missing match errors that tick but the ticker survives, and self-heals once a matching tab reappears. Registering with `tab=` on a kernel with no browser builtin refuses immediately rather than erroring on every tick forever.

A ticker outlives the cell that registered it, so its output is ambient: rendered unattributed and uncapped, not charged against that cell's budget.

```python
every.list()        # active EveryHandles
every.cancel_all()  # stop all tickers
```

## notify

```python
notify(content, *, session=None, exclude=None, **meta)
```

Push a channel notification to the agent. Metadata appears as extra fields in the notification payload.

`session=<claude_session_id>` targets one connected Claude Code session instead of broadcasting — returns `True` if delivered, `False` if that session isn't connected (no fallback to broadcast). `exclude=<claude_session_id>` — only meaningful with `session` left at its default — skips that one session from an otherwise-broadcast push, e.g. a self-report whose caller already has the result synchronously and doesn't need it echoed back.

```python
claude_sessions() → [(claude_session_id, project_dir, kind), ...]
current_session_id() → str | None
```

`claude_sessions()` lists every connected MCP session. `kind` is `"bg"` for a `claude --bg` worker, else `None`. `current_session_id()` returns the id of whoever triggered the code currently running — the same id `notify()`'s `session=`/`exclude=` take — or `None` with nothing to attribute to.

## Human gates

```python
await ask(prompt, *, tab=None, default=None, timeout=None)      → str
await confirm(prompt, *, tab=None, default=None, timeout=None)  → bool
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
| `loop_kill`                                                                    | watchdog cancelled the task holding the loop              |
| `loop_unblocked`                                                               | a reported block ended                                    |
| `init_loaded`                                                                  | `repld_init.py` ran at boot — `__main__` is pre-populated |
| `init_error`                                                                   | `repld_init.py` raised                                    |
| `venv`                                                                         | a project venv was adopted onto the running kernel        |
| `console_error`                                                                | `console.error` or uncaught exception from a watched tab  |
| `pin_lost`                                                                     | a pinned tab navigated cross-origin                       |
| `controls`                                                                     | `window.controls` action observation                      |
| `dialog`                                                                       | native JS dialog auto-dismissed                           |
| `filechooser`                                                                  | native file chooser opened                                |
| `auth`                                                                         | HTTP auth challenge cancelled (no pre-arm)                |
| `browser_warning`                                                              | a drag endpoint was occluded                              |
| `browser_connect` / `browser_disconnect` / `browser_watch` / `browser_unwatch` | dashboard browser actions                                 |

`loop_blocked` fires once a probe on the kernel loop misses `REPLD_LOOP_BLOCK_THRESHOLD` (default 5 s). It names the task holding the loop and the top of its stack, and a `loop_unblocked` with `blocked_s` follows when the block ends. Past `REPLD_LOOP_KILL_THRESHOLD` (default 30 s; `0` disables) the watchdog asks that task to cancel. The cancel can't interrupt synchronous code; it lands at the task's next `await`.

A bare `notify("...")` carries **no** kind at all — meta is whatever keywords you passed. Pass `kind=` yourself if you want to filter on it.

A task's completion is pushed to the session that started it; ambient output (`@every`, browser connect/disconnect, bare `notify()`) is broadcast. Console errors and `controls` observations go to the session that last drove that tab, and `loop_blocked`/`loop_kill`/`loop_unblocked` to the session whose task holds the loop; both broadcast when that session is gone.

## get_task / cancel

```python
get_task(task_id) → {done, text, spill_path, ...}
cancel(task_id)   → {cancelled: bool}
```

The snapshot's `push_delivered` is `true`/`false` once a completion push was sent to the session that started the task, and `null` when none was owed. `false` means that session had disconnected, so nobody has seen the result.

`cancel` only works on `await`-yielding code — tight sync loops (`while True: pass`) can't be preempted.

A session that ends its turn never sees a later channel push — confirmed for `claude --bg` workers, and true by construction for any one-shot client. If you need a deferred task's result, poll `get_task(task_id)` yourself in a loop before ending your turn rather than trusting the push to bring you back.

From a terminal instead of the agent, `repld tasks wait <task_id>` blocks on the same task and exits 0 on success or 1 on an exception or unknown id; `repld tasks cancel <task_id>` is the CLI face of `cancel`, exiting 0 if accepted. `repld tasks` alone lists every in-flight task and ticker.
