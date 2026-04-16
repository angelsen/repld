# Feature: LLM REPL (not human REPL)

## Overview

Reframe `repld` from "Python REPL with MCP access" into an **LLM REPL with a human observer**. The human watches a structured log of what the agent is doing. They don't type code at a prompt. When the agent's code needs a human decision (approve an action, choose between options), a discrete gate prompts the human inline.

This means: drop IPython and `prompt_toolkit` entirely. The kernel becomes a pure-stdlib asyncio runtime with its own compile+eval, plus a main-thread display consumer that renders a structured event stream.

## Specification Heritage

- **Evolved from:** `/home/fredrik/.claude/plans/synchronous-inventing-diffie.md` (the IPython-based port that shipped earlier in this session and validated end-to-end).
- **Changed:** drops IPython (+ prompt_toolkit + ~15 transitive deps), inverts the threading model back to prototype shape (asyncio loop on daemon thread, main thread owns display), replaces the interactive prompt with an append-only event-stream log, adds `ask`/`confirm`/`choose` human-gate primitives.
- **Preserved:** stdio MCP bridge + unix-socket IPC, exec/get_task/read_spill tools, channel push semantics, per-cwd `.pyrepl.lock`, `_current_task` ContextVar for output attribution, nudge-on-timeout behavior.
- **Why:** IPython's `patch_stdout` StdoutProxy defers writes until *after* the cell's ContextVar clears, breaking per-task output attribution. Working around it required a sys.stdout swap hack and leaked terminal escape codes into responses. IPython also forced main-thread loop ownership (breaking `asyncio.create_task` from sync code) and added deps for features (magics, tab-completion, rich display) that an LLM never uses.

## What It Does

### Log view (main thread, what the human sees)
- Kernel startup prints a compact banner: pid, socket path, register/launch commands.
- When an IPC exec arrives, the log shows a cell header (task id, short timestamp, dim color) followed by the source (indented, syntax-colored if `rich` is available, plain otherwise).
- User code output appears below the source, tagged with the task id if and only if the output arrives after the foreground cell has ended (i.e., from a fire-and-forget background task). Foreground output is untagged.
- A "done" line marks cell completion with elapsed time. Errors are shown with the traceback in red.
- Channel pushes from user `notify()` calls render as a bordered block with the meta dict visible.
- Human-gate prompts pause the log and render inline; on response, the log resumes.

### Human-gate primitives (injected into `__main__`)
- `ask(prompt: str, *, default: str | None = None, timeout: float | None = None) -> str`
- `confirm(prompt: str, *, default: bool | None = None, timeout: float | None = None) -> bool`
- `choose(prompt: str, options: list[str], *, default: str | None = None, timeout: float | None = None) -> str`

Behavior:
- Default (`timeout=None`): block forever until the human responds in the terminal.
- `timeout=N, default=X`: return `X` if the timeout expires.
- `timeout=N, default=None`: raise `TimeoutError` if the timeout expires.
- While a gate is pending, the log pauses; other incoming events queue and flush after the response.
- A `notify()` is auto-emitted when the gate opens (`kind="awaiting_human"`, `prompt="<text>"`) so any connected bridge sees that a human is being asked. No response path via channel — the agent sees the prompt text and can decide whether to wait or move on.

### Kernel runtime (background thread)
- One asyncio loop in a daemon thread, always running. User code runs on this loop via `run_coroutine_threadsafe`. `asyncio.create_task` from sync user code works again (was broken under IPython where the loop stopped between cells).
- Top-level `await` supported via `compile(..., PyCF_ALLOW_TOP_LEVEL_AWAIT)` + our own eval wrapper (port of prototype `bootstrap.py:107-142`).
- Last-expression repr: if the cell's last statement is an expression, its value is printed to stdout as `repr(value)` (user sees `42`, not silence).
- Tracebacks use stdlib `traceback.format_exc()`; `rich.traceback` if the optional extra is installed.

### Event stream (internal abstraction)
All display-bound output flows through a single queue of typed events. Display thread pops, formats, writes to `sys.__stdout__`. Event types: `CellStart`, `SourceLine`, `StdoutChunk`, `StderrChunk`, `CellDone`, `ChannelPush`, `HumanPromptOpen`, `HumanPromptResponse`. User code's `sys.stdout`/`sys.stderr` are replaced with a `_Tee` that converts writes into `StdoutChunk`/`StderrChunk` events tagged with the current `_current_task` ContextVar.

### MCP surface (unchanged from heritage)
Tools: `exec`, `get_task`, `read_spill`. Wire: NDJSON over unix socket via `repld bridge` stdio proxy. Channel notifications gated per-session on `notifications/initialized`. All current smoketest behaviors must continue to pass.

## Constraints

- **Pure stdlib in core.** `rich` is the only optional dep (guarded behind `repld[pretty]` extra; plain-text fallback when absent). No IPython, no `prompt_toolkit`, no singleton state.
- **ContextVar propagation into fire-and-forget tasks must survive.** `asyncio.create_task(bg())` inside a cell must carry the task_id so `bg()`'s later output tags correctly.
- **Python 3.12+.**
- **Display never blocks the loop.** Writing to the event queue is non-blocking (bounded queue, drop oldest with a warning if full — user output spam can't stall the kernel).

## Out of Scope

- `repld attach` interactive companion (future — if anyone actually wants to type code at a prompt, they launch a separate process that connects via the socket).
- Desktop notifications for pending human gates (future optional extra).
- Claude Code tool-based prompt response (`respond_to_prompt`) (future; channel is one-way per MCP spec, so this would need a new tool — defer).
- Helper API beyond human gates and `notify`: `defer`, `@every`, `@watch`, `@webhook`, `browser.*` stay out of this spec (unchanged from heritage: later phase).
- Rich Live redraws, TUI frames, or anything that breaks append-only scroll.
