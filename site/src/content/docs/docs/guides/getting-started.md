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

Or install the extra permanently:

```bash
uv tool install repld-tool[browser]
```

## Set up a project

```bash
cd your-project
claude mcp add repld -- repld bridge
```

That's the whole setup. repld writes nothing into your project — no `.mcp.json`, no `CLAUDE.md` block, nothing to `.gitignore`. Runtime state lives under `$XDG_RUNTIME_DIR/repld/`.

## Connect Claude Code

Launch Claude Code with channel support:

```bash
claude --dangerously-load-development-channels server:repld
```

Claude Code spawns `repld bridge` as a stdio subprocess. If a kernel for this project is already running, the bridge attaches to it; if not, MCP discovery is answered from a cache and a headless kernel is spawned lazily on the first real tool call — so a session that never touches repld never pays for one. The agent can now call `exec` to run Python.

## Watching the kernel

You don't have to start a kernel by hand, but you can watch and control one from any terminal:

```bash
repld status     # pid, uptime, socket, active tasks — plus live kernels elsewhere
repld log -f     # follow the same cells and channel pushes the display renders
repld stop       # shut this project's kernel down
repld dashboard  # open the built-in web control panel
```

Run `repld` in a terminal instead when you want the live TUI display. Either way the kernel writes its PID and socket path to `$XDG_RUNTIME_DIR/repld/projects/<slug>/kernel.lock` and stays up until stopped. See the [dashboard guide](/repld/docs/guides/dashboard/) for the control panel.

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
