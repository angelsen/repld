---
title: Getting started
description: Install repld, start the kernel, and connect Claude Code.
---

## Install

```bash
uv tool install repld-tool
```

For browser integration (CDP + DuckDB), run the kernel with `repld browser` instead of `repld` — it re-execs under `uv run` with the extra dependencies for that invocation, no project changes needed:

```bash
repld browser
```

Or install the extras permanently — both, because a `uv tool install` names the whole set and `[http]` (what `tab.http_client()` needs) on its own would drop `browser`:

```bash
uv tool install repld-tool[browser,http]
```

## Set up a project

```bash
cd your-project
claude mcp add repld -- repld bridge
```

That's the whole setup. repld writes nothing into your project — no `.mcp.json`, no `CLAUDE.md` block, nothing to `.gitignore`. Runtime state lives under `$XDG_RUNTIME_DIR/repld/`.

### Git worktrees

A kernel belongs to the directory it runs in, so a `claude --worktree` session gets its own kernel by default. To have worktrees share the main checkout's kernel instead, register the bridge with `--project-git` (before the subcommand):

```bash
claude mcp add repld -- repld --project-git bridge
```

The kernel is then keyed on, and always started from, the repo's main checkout (the first entry of `git worktree list`). Its `./gists`, `.venv`, `.env` and `repld_init.py` all come from there, so a worktree session runs the main checkout's code, not its own branch's. For `repld status`, `log` and friends in a worktree terminal, pass the same flag or set `REPLD_PROJECT_GIT=1`. `--project DIR` / `REPLD_PROJECT=DIR` does the same for any directory. Neither combines with `--socket`.

## Connect Claude Code

Launch Claude Code with channel support:

```bash
claude --dangerously-load-development-channels server:repld
```

Claude Code spawns `repld bridge` as a stdio subprocess. If a kernel for this project is already running, the bridge attaches to it; if not, MCP discovery is answered from a cache and a headless kernel is spawned lazily on the first real tool call — so a session that never touches repld never pays for one. The agent can now call `exec` to run Python.

### Throwaway sessions

That sharing is the point of a kernel: a second session in the same directory sees the same `__main__`, and the kernel outlives every bridge. For a session that should get neither — a CI job, a `claude -p` one-shot, an experiment whose state you don't want left behind — register the bridge with `--ephemeral`:

```bash
claude mcp add repld-scratch -- repld bridge --ephemeral
```

It inverts both halves. The kernel starts the moment the bridge does, at a private socket under `$XDG_RUNTIME_DIR/repld/ephemeral/` that no other bridge can resolve, so nothing attaches to it and it attaches to nothing. When the bridge's stdin closes it sends the kernel SIGTERM (giving `@every` tickers and `defer()` tasks their usual shutdown drain), and removes the directory. If the bridge dies without closing stdin — a kill, a timeout — the kernel notices on its own and exits the same way. The kernel still runs in your cwd, so `./gists`, `repld_init.py`, `.env` and `.venv` binding all apply as usual. It can't be combined with `--socket` (and ignores `REPLD_SOCKET`): the flag's whole point is a path nothing else could already be using.

## Watching the kernel

You don't have to start a kernel by hand, but you can watch and control one from any terminal:

```bash
repld status     # pid, uptime, socket, active tasks — plus live kernels elsewhere
repld log -f     # follow the same cells and channel pushes the display renders
repld tasks      # in-flight defer() tasks and @every tickers, --json for detail
repld start      # start a headless kernel now (no-op if one is running)
repld restart    # stop, then start a fresh headless kernel
repld stop       # shut this project's kernel down (--all: every kernel on the machine)
repld dashboard  # open the built-in web control panel
```

`repld status --json` gives the same as JSON; add `--counts` for each sibling
kernel's active task and ticker counts.
`repld tasks wait <task_id>` blocks until that task finishes and prints its
result, exiting 0 on success or 1 on an exception or unknown id.
`repld tasks cancel <task_id>` stops a running one — the CLI face of the
`cancel` MCP tool.

Run `repld` in a terminal instead when you want the live TUI display. Either way the kernel writes its PID and socket path to `$XDG_RUNTIME_DIR/repld/projects/<slug>/kernel.lock` and stays up until stopped. See the [dashboard guide](/repld/docs/guides/dashboard/) for the control panel.

### Where the headless kernel runs

On a systemd system, a spawned kernel runs as a transient user service, `repld-<slug>-<hash>.service`, rather than as a child of the bridge or `repld start` that spawned it — its own cgroup, its own lifetime, and its output in the journal, which is the only place a boot failure of a bridge-spawned kernel can be read:

```bash
journalctl --user -u 'repld-<slug>-*'
```

Without systemd, or if the unit can't be created, it falls back to a detached child process.

The service gets no resource limits by default: a cell that loads several gigabytes of model weights on purpose is a legitimate use, and the user manager's own OOM policy applies unchanged. Two environment variables, read by whatever spawns the kernel (the bridge, `repld start`, `repld restart`), opt in:

- `REPLD_MEMORY_HIGH` — a systemd `MemoryHigh=` value (`4G`, `50%`), the throttle-then-reclaim ceiling.
- `REPLD_OOM_SCORE_ADJUST` — a systemd `OOMScoreAdjust=` value. It can't go below the user manager's own adjustment without extra privilege, and systemd clamps rather than failing, so a value under that floor is silently raised to it.

Both are systemd-only; the fallback path ignores them.

## Your own REPL

In a third terminal:

```bash
repld exec
```

This drops you into a readline REPL connected to the same kernel. Anything the agent created is visible — variables, imports, running tasks. You share `__main__`.

One-shot mode works too:

```bash
repld exec "len(orders)"
```

## With an existing app

Create a `repld_init.py` at the project root. Every kernel that boots for this project executes it into `__main__` — including the headless one the bridge starts for you:

```python
from myapp.main import app
from myapp.db import async_session_maker
import asyncio, uvicorn

asyncio.create_task(uvicorn.Server(
    uvicorn.Config(app, host="127.0.0.1", port=8000, log_level="warning")
).serve())

session = async_session_maker()
print("FastAPI on :8000, db session ready")
```

Nothing to pass — the file is found by name. The agent now has a live handle on your running app: inspect routes, query the ORM, call handlers directly. A bootstrap that raises leaves the kernel up (you need a live kernel to fix it from) and pushes an `init_error` notification.

`./.env` is read at boot too, and never overrides a variable that's already set. Unlike `./gists`, it doesn't reload on mtime — so if you write a value after the kernel is up, re-read it yourself:

```python
from repld import load_dotenv
load_dotenv()
```

Existing variables still win, so a name captured while empty stays empty — `os.environ.pop("KEY", None)` first if you're correcting one.

## Answering a prompt

`ask()`, `confirm()` and `choose()` in a cell block until a human responds. On a kernel you started yourself, you answer in its pane; on a pinned browser tab, the pill UI. On the headless kernel the bridge spawned there is neither, so the notification carries the command:

```bash
repld gate                          # list what's pending
repld gate answer <id> yes
```

Everything after `answer <id>` is the answer verbatim, so free text needs no quoting. Flags for the command itself go before `answer`.

## What's next

- [Browser guide](/repld/docs/guides/browser/) — attach to Chrome, discover APIs, capture traffic
- [Gists guide](/repld/docs/guides/gists/) — reusable modules that wrap any web app
- [Dashboard guide](/repld/docs/guides/dashboard/) — the kernel's built-in web control panel
