# repld

Persistent Python runtime with MCP channel push. Dev shell and autonomous-agent substrate in one package.

```bash
uv tool install repld-tool
```

## What it does

- **Stateful kernel** — auth once, hold the client, query across turns. State persists across cells.
- **Async-native** — top-level `await`, `defer()` for fire-and-forget, `@every()` for periodic tasks. Long jobs never block the turn.
- **Channel push** — task completion, webhooks, file changes, and timers arrive as `<channel>` injections. The agent reacts; it doesn't poll.
- **Shared namespace** — human and agent operate on the same `__main__`. Stage data in one, use it in the other.
- **Browser integration** — attach to your logged-in Chrome tabs via CDP. No API keys, no OAuth dance. The agent discovers the API surface from your traffic.
- **Gists** — reusable Python modules that wrap any web app's API. The browser supplies auth; the gist captures the pattern.

## Install

```bash
# install globally
uv tool install repld-tool

# in any project:
cd path/to/project
repld init               # writes .mcp.json + updates .gitignore
repld                    # starts the kernel
```

Project-local alternative: `uv add --dev repld-tool`, then point `.mcp.json` at `uv run repld bridge`.

`repld init` produces this `.mcp.json`:

```json
{
  "mcpServers": {
    "repld": { "type": "stdio", "command": "repld", "args": ["bridge"] }
  }
}
```

## Quick example

The agent calls `exec` to run Python in the kernel:

```python
# runs inline — result returned immediately
import httpx
httpx.get("https://api.example.com/status").json()

# long-running — returns task_id, pushes channel notification on completion
await asyncio.sleep(30)
notify("done", kind="migration")
```

Autonomous worker — five lines:

```python
@every(300)
async def check_overdue():
    for inv in await po.get_overdue():
        notify(f"Overdue: {inv.customer} {inv.amount} NOK",
               kind="overdue", invoice_id=inv.id)
```

The kernel runs the watcher; the agent reacts to each `<channel>` injection.

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

The agent now has a live handle on your running app: inspect routes, query the ORM, call handlers bypassing HTTP.

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
| `browser_attach` | Watch URL pattern, auto-attach matching tabs. |
| `browser_tabs` | List attached tabs. |
| `browser_pages` | List all Chrome targets. |
| `browser_js` | Evaluate JavaScript in a tab. |
| `browser_network` | Query captured traffic (HAR-style, DuckDB). |
| `browser_body` | Response body for a captured request. |
| `browser_request` | Request headers/postData for a captured request. |
| `browser_fetch` | In-page fetch (inherits auth/cookies). |
| `browser_click` | Click element (trusted dispatch). |
| `browser_type` | Type into element (trusted dispatch). |
| `browser_key` | Send key press (Enter, Escape, etc). |
| `browser_navigate` | Navigate tab to URL. |
| `browser_open` | Open new tab. |
| `browser_console` | Query console logs and exceptions. |
| `browser_screenshot` | Capture page screenshot. |
| `browser_cdp` | Raw CDP passthrough. |
| `browser_clear` | Reset captured network/console. |
| `browser_detach` | Remove watch pattern, detach tabs. |

Output from every cell spills to `$XDG_RUNTIME_DIR/repld/` — the inline response carries a head/tail preview plus the spill path. Use standard `Read`/`Grep` tools for full output.

## Helpers

Available in the kernel namespace:

```python
notify(content, **meta)              # channel push to the agent
ask(prompt)                          # block on free-form human input
confirm(prompt)                      # block on yes/no
choose(prompt, options)              # block on pick-one
defer(coro, label=None)              # fire-and-forget, channel push on completion
@every(seconds)                      # periodic ticker, fn.cancel() to stop
```

Browser builtins (when `repld[browser]` is installed):

```python
tab = await browser.get("*example.com*")  # find tab by URL glob
tab = await browser.open("https://...")   # open new tab
await browser.watch("*pattern*")          # auto-attach matching tabs

await tab.js("document.title")            # eval JS
await tab.fetch("/api/data")              # in-page fetch (inherits session)
await tab.click("#submit")                # trusted click
await tab.type_text("#search", "query")   # trusted typing
tab.network(url="*api*")                  # query captured traffic
```

## Gists

Gists are Python modules in `./gists/` (project) or `~/.repld/gists/` (global) that wrap anything into a callable API — web apps via the browser, databases, graph stores, embedding indexes, internal services.

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
import myapp
app = await myapp.MyApp.connect()
await app.accounts()
```

Re-importing after edits auto-reloads. Gists can register MCP tools via `__repld_tools__` — scaffold with `repld gist <name>`. Run `repld help gists` for details.

## Browser

`repld[browser]` attaches to Chrome via CDP (`--remote-debugging-port=9222`). You log in normally; the agent sees your traffic, discovers the API surface, and works with your authenticated sessions.

```python
tab = await browser.get("*salesforce*")
reqs = tab.network(url="*/api/*")           # discover API calls
auth = reqs[0].request_headers["Authorization"]  # extract auth
```

Body capture via Fetch interception means login flows, redirects, and CSRF exchanges are never lost. See [docs/browser.md](docs/browser.md) for the full design.

## Scope

`repld` executes arbitrary Python in your project environment. It is a **dev-time tool** — never a runtime dependency. The IPC socket is localhost-only with user-only permissions.

Channels are a research-preview feature of Claude Code. The current integration uses `--dangerously-load-development-channels server:repld`.

## License

[MIT](LICENSE)
