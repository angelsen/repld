# repld

A persistent Python runtime the agent can actually work in. IPython kernel + MCP channel push + stdlib primitives. Substrate, not a library.

```bash
pip install repld
repld                    # starts an IPython kernel + MCP bridge in the project
```

Point Claude Code at it and long-running tasks complete *into* your conversation. Webhooks, file changes, and scheduled jobs arrive as `<channel>` injections. The agent doesn't poll — the world pushes.

## Why

Two things happened at once:

**1. Traditional REPL → agent integration is miserable.** PTY transport means fake keystrokes and prompt parsing. State disappears between script runs. Long jobs block the whole turn. Most "agent does thing in Python" setups work around this by writing files and running them — losing all the iteration speed a REPL is supposed to provide.

**2. The agent collapses the library moat.** Selenium, Puppeteer, BeautifulSoup, ORMs, form-filling kits, OpenAPI client generators — these existed because writing orchestration code used to be expensive. An LLM with access to CDP + `httpx` + a live SQL connection writes the equivalent code on demand, tuned to the exact task, against the exact page/API/schema. The library becomes overhead.

`repld` is what falls out when you take both seriously:

- **Stateful** — auth once, hold the client, query 50 times across turns. Setup cost amortizes across the whole session.
- **Async-native** — top-level `await`, fire-and-forget `asyncio.create_task`, `await asyncio.to_thread(slow)` for blocking work. Long jobs never block the turn.
- **Shared namespace** — human and agent operate on the same `__main__.__dict__`. Stage data in one, use it in the other. Live debugging, not isolated sandboxes.
- **Channel push** — `notifications/claude/channel` on task completion, webhook arrival, file change, timer fire. Agent becomes ambient rather than turn-based.
- **Substrate, not library** — stdlib + IPython + small helpers. The agent generates the integration code against live pages/APIs/DBs. No per-service MCP server to write.

## Two modes

**Dev shell for existing projects.** Drop `.mcp.json` into an existing FastAPI/Django/Flask app, `repld --init repl.py` to pre-load the app + DB session, and the agent has a live handle on your running service's memory. Faster than `pytest -k` for ad-hoc verification; faster than DBeaver for ad-hoc queries. Zero changes to your app.

**Autonomous agent runtime.** Set up watchers (`@every`, `@watch`, `@webhook`), give the agent the clients it needs (captured via CDP from your logged-in browser tabs), and the agent processes inbound events on its own between turns. PowerOffice overdue-invoice reminders, GitHub PR auto-review, email triage, build-failure remediation — all the same shape, ~10 lines of setup each. The kernel is the cron + systemd + webhook receiver; the agent is the action layer.

One pip install, one entrypoint, one `.mcp.json`. Either mode, or both.

## Quickstart

```bash
# install
pip install repld        # or: uv pip install repld

# in your project's cwd:
repld                    # starts the kernel
```

Project-level integration (`.mcp.json` at the repo root):

```json
{
  "mcpServers": {
    "repld": { "command": "repld", "args": ["bridge"] }
  }
}
```

Launch Claude Code from that directory:

```bash
claude --dangerously-load-development-channels server:repld
```

Now tool calls go through `exec` into your live kernel, and any long task, webhook hit, or file-watcher event arrives as `<channel source="repld" …>…</channel>` without polling.

## With an existing app

`repld` inherits your project's environment. For a FastAPI project, a `repl.py` at the project root:

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

The agent now has a live handle on your running app: inspect routes, query the ORM session directly, call handlers bypassing HTTP, hot-reload modules after edits. Same pattern works for Django, Flask, or pure-script projects.

A runnable end-to-end version lives at [`examples/fastapi/`](examples/fastapi/).

## Tools (exposed to the agent over MCP)

| Tool         | Behavior                                                                                             |
| ------------ | ---------------------------------------------------------------------------------------------------- |
| `exec`       | Execute Python in the kernel. Returns inline if it finishes within `timeout` (default 2s); otherwise returns `{task_id, done:false}` and the completion arrives as a channel notification. |
| `get_task`   | Current status + head/tail preview of a task's output.                                               |

Every cell with output spills to `$XDG_RUNTIME_DIR/repld/{pid}-{tid}.out` from byte 1; the inline response carries a head+tail preview plus the absolute spill path. For full output, the agent uses the standard `Read`/`Grep` tools on that path — no dedicated MCP tool needed.

## Helpers (in-kernel namespace)

Available now:

```python
notify(content, **meta)                 # one-off channel push; meta → XML attrs
ask(prompt, *, default=None, timeout=None)        # block on free-form human input
confirm(prompt, *, default=None, timeout=None)    # block on yes/no
choose(prompt, options, *, default=None, timeout=None)  # block on pick-one
```

Planned:

```python
defer(coro, label=None)                 # run on shared loop, channel-push on finish
@every(seconds)                         # periodic channel emission
@watch("/path")                         # file changes → channel (needs watchdog)
@webhook("/path")                       # http route → channel
browser.find(url_pattern)               # attach to chrome via CDP (remote-debug port)
```

Minimal autonomous worker — five lines:

```python
po = PowerOfficeClient.from_browser_auth()
@every(300)
def check_overdue():
    for inv in po.get_overdue_invoices():
        notify(f"Overdue: {inv.customer} {inv.amount} NOK",
               kind="overdue", invoice_id=inv.id)
```

Kernel runs the watcher, agent reacts to each `<channel>` injection, calls `po.send_reminder(...)` via `exec`. You do nothing after setup.

## Architecture

```
Project cwd
 └─ .mcp.json                   → tells Claude Code to spawn `repld bridge`
 └─ .pyrepl.lock                → {pid, socket_path} of the running kernel

Terminal 1: `repld`             IPython kernel + IPC server (unix socket in cwd)
Terminal 2: `claude …`          spawns `repld bridge` via stdio MCP
                                bridge proxies stdio ↔ IPC socket
                                channel notifications flow through
```

- **Stdio MCP subprocess** — canonical shape per channel docs. Claude Code spawns it; no always-on daemon, no port management, no gateway.
- **Per-cwd lockfile** — the kernel's IPC path lives in `./.pyrepl.lock`. Stdio bridge inherits `cwd` from Claude Code, reads the lockfile, connects.
- **Stdlib REPL** — `compile()` + `eval()` with `PyCF_ALLOW_TOP_LEVEL_AWAIT`. Last-expression auto-display binds to `_` and `_N`. AST split lets `x = 1; "last"` still display the trailing expression. No IPython, no `prompt_toolkit` — the asyncio loop owns the main process and a separate display thread renders events.
- **Shared asyncio loop** — one process-wide loop on a daemon thread. `asyncio.create_task(...)` works from anywhere, tasks survive the exec return. A watchdog channel-pushes if the loop wedges (default >5s, tunable via `REPLD_LOOP_BLOCK_THRESHOLD`).
- **Stdlib only in core** — zero required dependencies. Optional extras: `repld[pretty]` (rich-rendered display), `repld[web]` (FastAPI/uvicorn for the example).

## Design principles

- **Substrate, not library.** Primitives composable by the agent, not a feature catalog. The LLM writes the integration code against live pages/APIs/DBs — repld just gives it a persistent place to run, observe, and react.
- **No per-service MCP.** Don't write a Slack-MCP + GitHub-MCP + PowerOffice-MCP. Capture auth once (CDP, env, OAuth), hold the client in the namespace, let the agent compose. Same applies to DOM scraping (skip BeautifulSoup; the LLM writes the `querySelectorAll` that extracts exactly what this task needs).
- **One process, shared state.** Human and agent operate on the same memory. Stage in one, read from the other.
- **Async-native.** Nothing blocks the turn. Long jobs push to channel on completion.
- **Honest scope.** Not for production. Dev dependency only. Your app's memory is live to whoever can connect.

## Scope & security

`repld` executes arbitrary Python in your project's environment. It's a **dev-time tool**, never a runtime dependency. The IPC socket is bound to `127.0.0.1` (or unix socket with user-only perms). Don't expose it beyond localhost; don't enable it in production images.

Channels are a research-preview feature of Claude Code. The current integration uses `--dangerously-load-development-channels server:repld` until channels exit preview or `repld` is submitted to the approved allowlist.

## Status

Research preview. The thesis is validated — full MCP-over-stdio with channel push and top-level await works end-to-end. Productization in progress:

- [x] Stdlib REPL with top-level await, `_` / `_N` history, AST-split last-expression display
- [x] Stdio MCP bridge + unix-socket IPC
- [x] `repld` and `repld bridge` CLI entrypoints
- [x] Lazy spill-to-disk for all cell output + head/tail inline preview
- [x] Loop watchdog (channel-pushes when the bg loop wedges)
- [x] Human gates (`ask`, `confirm`, `choose`) and `notify`
- [ ] Background-task helpers (`defer`, `@every`, `@watch`, `@webhook`)
- [ ] Browser helpers (`browser.find` via CDP)
- [ ] Framework presets (`--preset fastapi`, `--preset django`)
- [ ] Optional Claude Code plugin distribution

## License

MIT (planned).
