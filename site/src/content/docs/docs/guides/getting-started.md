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
repld init
```

This creates `.mcp.json` (so Claude Code discovers the MCP server) and adds runtime files to `.gitignore`. The generated config:

```json
{
  "mcpServers": {
    "repld": { "type": "stdio", "command": "repld", "args": ["bridge"] }
  }
}
```

## Start the kernel

```bash
repld
```

The kernel writes `.pyrepl.lock` with its PID and socket path, then listens for connections. It stays up until you stop it. It also prints a dashboard URL — a built-in web control panel, no setup required. See the [dashboard guide](/repld/docs/guides/dashboard/).

## Connect Claude Code

Launch Claude Code with channel support:

```bash
claude --dangerously-load-development-channels server:repld
```

Claude Code reads `.mcp.json`, spawns `repld bridge` as a stdio subprocess, and the bridge connects to the running kernel over the unix socket. The agent can now call `exec` to run Python.

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

Create a `repl.py` that sets up your app:

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

```bash
repld --init repl.py
```

The agent now has a live handle on your running app: inspect routes, query the ORM, call handlers directly.

## What's next

- [Browser guide](/repld/docs/guides/browser/) — attach to Chrome, discover APIs, capture traffic
- [Gists guide](/repld/docs/guides/gists/) — reusable modules that wrap any web app
- [Dashboard guide](/repld/docs/guides/dashboard/) — the kernel's built-in web control panel
