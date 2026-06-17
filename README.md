# repld

A live Python kernel for your agent. Shared namespace, channel push, a scriptable browser, and one-off code that hardens into tools.

```bash
uv tool install repld-tool
```

## What it does

- **Persistent kernel** — one long-running Python process in your project directory. State survives across turns.
- **Shared namespace** — you and the agent operate on the same `__main__`. Inspect its variables, patch its functions, take over mid-task.
- **Channel push** — long jobs, timers, file watchers, and webhooks push back as notifications. The agent reacts; it never polls.
- **Browser** — attach to your logged-in Chrome via CDP. Every mutation settles, then returns the accessibility tree, network delta, and console delta.
- **Gists** — plain Python files the kernel hot-reloads. Reverse-engineer an API once, import it forever, link it across projects, register it as an MCP tool.

## Install

```bash
uv tool install repld-tool        # global install (recommended)

cd your-project
repld init                         # writes .mcp.json + updates .gitignore
repld                              # starts the kernel
```

Project-local alternative: `uv add --dev repld-tool`, then point `.mcp.json` at `uv run repld bridge`.

`repld init` produces:

```json
{
  "mcpServers": {
    "repld": { "type": "stdio", "command": "repld", "args": ["bridge"] }
  }
}
```

## Quick example

```python
# runs inline — result returned immediately
import httpx
httpx.get("https://api.example.com/status").json()

# long-running — returns task_id, pushes channel notification on completion
await asyncio.sleep(30)
notify("done", kind="migration")
```

Autonomous worker:

```python
@every(300)
async def check_overdue():
    for inv in await erp.get_overdue():
        notify(f"Overdue: {inv.customer} — {inv.amount}",
               kind="overdue", invoice_id=inv.id)
```

The kernel runs the watcher; the agent reacts to each channel notification.

## With an existing app

`repld` inherits your project's environment. A `repl.py` at the project root:

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

## Tools

**Core:**

| Tool | What it does |
|------|-------------|
| `exec` | Execute Python. Returns inline within timeout (default 2s); otherwise returns `task_id` and pushes channel on completion. |
| `get_task` | Status + head/tail preview of a running task's output. |
| `cancel` | Cancel a running task by id. |

**Browser** (requires `uv tool install repld-tool[browser]`):

| Tool | What it does |
|------|-------------|
| `browser_watch` | Watch URL pattern, auto-attach matching tabs. |
| `browser_tabs` | List attached tabs. |
| `browser_pages` | List all Chrome targets. |
| `browser_js` | Evaluate JavaScript (REPL semantics, top-level await). |
| `browser_network` | Query captured traffic (HAR-style, DuckDB). |
| `browser_request` | Full HAR entry — headers, postData, timing. |
| `browser_body` | Response body for a captured request. |
| `browser_fetch` | In-page fetch (inherits auth/cookies). |
| `browser_click` | Click element (auto-waits, returns observation). |
| `browser_type` | Type into element. |
| `browser_key` | Send key press (Enter, Escape, etc). |
| `browser_navigate` | Navigate tab to URL. |
| `browser_open` | Open new tab and navigate. |
| `browser_tree` | Accessibility tree snapshot. |
| `browser_console` | Query console logs and exceptions. |
| `browser_screenshot` | Capture page screenshot. |
| `browser_cdp` | Raw CDP passthrough. |
| `browser_clear` | Reset captured network/console. |
| `browser_detach` | Remove watch pattern, detach tabs. |

Output from every cell spills to `$XDG_RUNTIME_DIR/repld/` — the inline response carries a head/tail preview plus the spill path. Use standard `Read`/`Grep` tools for full output.

## Kernel builtins

```python
notify(content, **meta)              # channel push to the agent
ask(prompt)                          # block on free-form human input
confirm(prompt)                      # block on yes/no
choose(prompt, options)              # block on pick-one
defer(coro, label=None)              # fire-and-forget, channel push on completion
@every(seconds)                      # periodic ticker, fn.cancel() to stop
```

## Browser

`repld[browser]` attaches to Chrome via CDP (`--remote-debugging-port=9222`). You log in normally; the agent sees your traffic, discovers the API surface, and works with your authenticated sessions.

```python
tab = await browser.get("*example.com*")     # find tab by URL glob
tab = await browser.open("https://...")      # open new tab
await browser.watch("*pattern*")             # auto-attach matching tabs

await tab.js("document.title")               # eval JS (top-level await works)
await tab.fetch("/api/data")                 # in-page fetch (inherits session)
await tab.click("#submit")                   # click, settle, return observation
await tab.type_text("#search", "query")      # type into element
tab.network(url="*api*")                     # query captured traffic
```

Body capture via Fetch interception means login flows, redirects, and CSRF exchanges are never lost. See [docs/browser.md](docs/browser.md) for the full design.

## Gists

Gists are Python modules in `./gists/` (project) or `~/.repld/gists/` (global) that wrap anything into a callable API. The browser supplies auth; the gist captures the pattern.

```python
# gists/myapp.py
"""MyApp — accounts and transactions."""

class MyApp:
    def __init__(self, tab): self._tab = tab

    @classmethod
    async def connect(cls):
        from __main__ import browser
        tab = await browser.get("*myapp.com*")
        return cls(tab)

    async def accounts(self):
        return (await self._tab.fetch("/api/accounts"))["body"]
```

```python
from myapp import MyApp
app = await MyApp.connect()
await app.accounts()
```

Re-importing after edits auto-reloads. Gists can declare dependencies (`__repld_deps__`), register MCP tools (`__repld_tools__`), and link across projects (`repld gist add <name>`). See `repld help gists` for details.

## Scope

`repld` executes arbitrary Python in your project environment. It is a **dev-time tool** — never a runtime dependency. The IPC socket is localhost-only with user-only permissions.

Channel push requires Claude Code's `--channels` flag (research preview).

## License

[MIT](LICENSE)
