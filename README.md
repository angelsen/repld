# repld

A persistent Python runtime the agent can actually work in. Stdlib REPL + MCP channel push + browser/file/event primitives. Substrate, not a library.

```bash
uv tool install repld
repld                    # starts a kernel + MCP bridge in the project
```

Point Claude Code at it and long-running tasks complete *into* your conversation. Webhooks, file changes, and scheduled jobs arrive as `<channel>` injections. The agent doesn't poll — the world pushes.

## Why

Two things happened at once:

**1. Traditional REPL → agent integration is miserable.** PTY transport means fake keystrokes and prompt parsing. State disappears between script runs. Long jobs block the whole turn. Most "agent does thing in Python" setups work around this by writing files and running them — losing all the iteration speed a REPL is supposed to provide.

**2. The agent collapses the library moat.** Selenium, Puppeteer, BeautifulSoup, ORMs, form-filling kits, OpenAPI client generators — these existed because writing orchestration code used to be expensive. An LLM with access to CDP + `httpx` + a live SQL connection writes the equivalent code on demand, tuned to the exact task, against the exact page/API/schema. The library becomes overhead. Per-service MCP servers scale linearly — one per service, maintained forever. repld replaces them all: attach to your logged-in browser, discover the API surface from the traffic, synthesize a client.

`repld` is what falls out when you take both seriously:

- **Stateful** — auth once, hold the client, query 50 times across turns. Setup cost amortizes across the whole session.
- **Async-native** — top-level `await`, fire-and-forget `asyncio.create_task`, `await asyncio.to_thread(slow)` for blocking work. Long jobs never block the turn.
- **Shared namespace** — human and agent operate on the same `__main__.__dict__`. Stage data in one, use it in the other. Live debugging, not isolated sandboxes.
- **Channel push** — `notifications/claude/channel` on task completion, webhook arrival, file change, timer fire. Agent becomes ambient rather than turn-based.
- **Substrate, not library** — stdlib + small helpers. The agent generates the integration code against live pages/APIs/DBs. No per-service MCP server to write.

## Two modes

**Dev shell for existing projects.** Drop `.mcp.json` into an existing FastAPI/Django/Flask app, `repld --init repl.py` to pre-load the app + DB session, and the agent has a live handle on your running service's memory. Faster than `pytest -k` for ad-hoc verification; faster than DBeaver for ad-hoc queries. Zero changes to your app.

**Autonomous agent runtime.** Set up watchers (`@every`, `@watch`, `@webhook`), give the agent the clients it needs (captured via CDP from your logged-in browser tabs), and the agent processes inbound events on its own between turns. PowerOffice overdue-invoice reminders, GitHub PR auto-review, email triage, build-failure remediation — all the same shape, ~10 lines of setup each. The kernel is the cron + systemd + webhook receiver; the agent is the action layer.

One `uv tool install`, one entrypoint, one `.mcp.json`. Either mode, or both.

## Quickstart

```bash
# install once, globally available as a CLI
uv tool install repld

# in any project where you want repld:
cd path/to/project
repld init               # writes .mcp.json + updates .gitignore
repld                    # starts the kernel (or `repld --init repl.py` if you wrote one)
```

Project-local install (alternative): `uv add --dev repld`, then point `.mcp.json` at `uv run repld bridge`.

`repld init` produces this `.mcp.json` at the project root:

```json
{
  "mcpServers": {
    "repld": { "type": "stdio", "command": "repld", "args": ["bridge"], "env": {} }
  }
}
```

Anywhere: `repld help` for the substrate-level overview, `repld help <topic>` for details (`exec`, `channel`, `notify`, `init`, `gists`, `browser`). The agent can call `!repld help` from inside Claude Code at any time — the docs ship with the binary, no separate file to keep in sync.

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

**Core tools:**

| Tool         | Behavior                                                                                             |
| ------------ | ---------------------------------------------------------------------------------------------------- |
| `exec`       | Execute Python in the kernel. Returns inline if it finishes within `timeout` (default 2s); otherwise returns `{task_id, done:false}` and the completion arrives as a channel notification. |
| `get_task`   | Current status + head/tail preview of a task's output.                                               |
| `cancel`     | Cancel a running task by id. Works on await-yielding code; cannot preempt tight sync loops.          |

**Browser tools** (requires `repld[browser]`):

| Tool              | Behavior                                                    |
| ----------------- | ----------------------------------------------------------- |
| `browser_attach`  | Add URL watch pattern, attach matching tabs now + auto-attach future matches. |
| `browser_tabs`    | List currently attached tabs.                               |
| `browser_pages`   | List all Chrome targets (attached or not).                  |
| `browser_js`      | Evaluate JavaScript in a tab (`Runtime.evaluate` with auto-await). |
| `browser_network` | Query captured network traffic (HAR-style, DuckDB-backed). |
| `browser_body`    | Fetch response body for a captured request.                 |
| `browser_click`   | Click an element (trusted `Input.dispatchMouseEvent`).      |
| `browser_type`    | Type into an element (trusted `Input.dispatchKeyEvent`).    |
| `browser_console` | Query console logs and exceptions.                          |
| `browser_screenshot` | Capture page screenshot.                                 |
| `browser_cdp`     | Raw CDP passthrough (escape hatch).                         |
| `browser_navigate` | Navigate a tab to a URL. Blocked on iframe targets.        |
| `browser_open`    | Open new tab and navigate.                                  |
| `browser_key`     | Send a key press (Enter, Escape, etc).                      |
| `browser_fetch`   | In-page fetch (inherits page auth/cookies).                 |
| `browser_request` | Inspect captured request headers/postData.                  |
| `browser_clear`   | Reset captured network/console for a tab.                   |
| `browser_detach`  | Remove watch pattern, detach tabs.                          |

Every cell with output spills to `$XDG_RUNTIME_DIR/repld/{pid}-{tid}.out` from byte 1; the inline response carries a head+tail preview plus the absolute spill path. For full output, the agent uses the standard `Read`/`Grep` tools on that path — no dedicated MCP tool needed.

## Helpers (in-kernel namespace)

Available now:

```python
notify(content, **meta)                 # one-off channel push; meta → XML attrs
ask(prompt, *, default=None, timeout=None)        # block on free-form human input
confirm(prompt, *, default=None, timeout=None)    # block on yes/no
choose(prompt, options, *, default=None, timeout=None)  # block on pick-one
defer(coro, label=None)                 # run on shared loop, channel-push on finish
```

Planned (priority order — smallest first):

```python
notify_on_logs(level, logger=None)      # route stdlib logging to channel
@every(seconds)                         # periodic channel emission
@watch("/path")                         # file changes → channel (needs watchdog)
@webhook("/path")                       # http route → channel
```

Browser builtins (requires `repld[browser]`):

```python
browser.attach("*pattern*")             # watch pattern, auto-attach matching tabs
browser.find("9222:abc123")             # resolve one Tab by target ID
browser.tabs                            # list attached tabs
browser.pages                           # list all Chrome targets
browser.detach("*pattern*")             # remove watch + detach

tab = browser.find("9222:abc123")
tab.js("document.title")                # eval JS (auto-await, trusted gestures)
tab.click("#search-btn")                # trusted click (Input.dispatchMouseEvent)
tab.type_text("#search", "query")       # trusted typing (Input.dispatchKeyEvent)
tab.fetch("https://api.example.com/v1") # in-page fetch (inherits auth/cookies)
tab.navigate("https://example.com")     # navigate tab to URL
tab.network(url="*api*", status=200)    # query captured traffic (DuckDB HAR view)
tab.console(level="error")              # query console logs + exceptions
tab.body(request_id)                    # fetch response body (Fetch-captured)
tab.cdp("Page.navigate", url="...")     # raw CDP passthrough
```

Also planned: a remote-ask variant of the human gates that routes through the
MCP client (Claude Code) instead of the kernel's terminal pane, for when the
human lives in the chat rather than next to the kernel.

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

## Browser — authenticated access to any SaaS

`repld[browser]` attaches to your Chrome tabs via CDP. You log in normally; the agent sees your traffic, discovers the API surface, and works with authenticated sessions — no API keys, no OAuth dance, no per-service MCP server.

```bash
repld exec 'browser.attach("*salesforce*")'    # watch pattern, auto-attach
repld exec 'browser.find("*salesforce*").network(url="*/api/*")'   # discover API calls
```

The agent sees every request your browser makes: URLs, headers, auth tokens, request/response bodies. From one captured request it can synthesize an `httpx` client with the right headers pre-filled, and start calling the API directly.

```python
tab = browser.find("*salesforce*")
r = tab.network(url="*/api/records*")[0]       # find the API call
r.curl()                                       # copy as curl
r.request_headers["Authorization"]             # extract the bearer token
```

Body capture via Fetch interception means login flows, redirects, and CSRF token exchanges are never lost — even when Chrome would normally evict response bodies across navigation.

The browser exposes its own MCP tools alongside `exec`, so agents discover browser capabilities via the tool list without needing to know Python:

| Tool              | Behavior                                                    |
| ----------------- | ----------------------------------------------------------- |
| `browser_attach`  | Add URL watch pattern, attach matching tabs                 |
| `browser_tabs`    | List currently attached tabs                                |
| `browser_pages`   | List all Chrome targets (attached or not)                   |
| `browser_js`      | Evaluate JavaScript in a tab                                |
| `browser_network` | Query captured network traffic (HAR-style, DuckDB-backed)   |
| `browser_body`    | Fetch response body for a captured request                  |
| `browser_click`   | Click an element (trusted `Input.dispatchMouseEvent`)       |
| `browser_type`    | Type into an element (trusted `Input.dispatchKeyEvent`)     |
| `browser_console` | Query console logs and exceptions                           |
| `browser_screenshot` | Capture page screenshot                                  |
| `browser_cdp`     | Raw CDP passthrough (escape hatch)                          |
| `browser_navigate` | Navigate a tab to a URL. Blocked on iframe targets         |
| `browser_open`    | Open new tab and navigate                                   |
| `browser_key`     | Send a key press (Enter, Escape, etc)                       |
| `browser_fetch`   | In-page fetch (inherits page auth/cookies)                  |
| `browser_request` | Inspect captured request headers/postData                   |
| `browser_clear`   | Reset captured network/console for a tab                    |
| `browser_detach`  | Remove watch pattern, detach tabs                           |

Requires Chrome running with `--remote-debugging-port=9222`. See `docs/browser.md` for the full design.

## Gists — reusable recipes for any service

`gists/` are small Python modules that capture the *pattern* for talking to a service. The browser supplies fresh credentials each session; the gist captures which endpoints, which headers, which auth shape. Agents leave breadcrumbs as they go — each successful integration becomes a gist for next time.

```python
# gists/dnb.py — reusable bank client, 10 lines
"""DNB bank. Attach to logged-in DNB tab first."""

async def client():
    tab = browser.find("*dnb*")
    headers = tab.network(url="*/api/*")[0].request_headers
    return httpx.Client(base_url="https://www.dnb.no/api", headers=headers)

async def accounts(c):
    return c.get("/accounts").json()

async def transactions(c, account_id, since="2026-01-01"):
    return c.get(f"/accounts/{account_id}/transactions", params={"from": since}).json()
```

Usage, every session:

```python
import dnb
c = await dnb.client()                 # auth captured from browser
txns = await dnb.transactions(c, "1234")
```

Gists live in `~/.repld/gists/` (global) or `./gists/` (project-local), both directly on `sys.path` — import by module name, not `gists.name`. The agent writes the gist once by observing your traffic. Someone else with a DNB login does `browser.attach("*dnb*")` + `import dnb` and it works — the gist is the recipe, the browser is the auth.

The bridge exposes `repld://gists/{name}` resource templates so agents can discover available gists at init time. Re-importing a gist after edits auto-reloads it — no kernel restart needed.

Same shape for Salesforce, PowerOffice, Gmail, internal admin tools, any bank. Per-service MCP servers scale linearly (one per service, maintained forever). Gists scale with whatever's in your browser.

## Architecture

```
Project cwd
 └─ .mcp.json                   → tells Claude Code to spawn `repld bridge`
 └─ .pyrepl.lock                → {pid, socket_path} of the running kernel

Terminal 1: `repld`             Kernel (asyncio loop) + IPC server (unix socket in cwd)
Terminal 2: `claude …`          spawns `repld bridge` via stdio MCP
                                bridge proxies stdio ↔ IPC socket
                                channel notifications flow through
Terminal 3: `repld exec`        Human REPL / one-shot CLI, same IPC socket
```

Five CLI subcommands, all dispatched from `repld:main`:

- `repld` — long-running Python kernel in the project cwd. Writes `./.pyrepl.lock` with `{pid, socket_path}`; listens on a unix-domain socket for IPC.
- `repld bridge` — short-lived stdio MCP subprocess spawned by Claude Code via `.mcp.json`. Inherits cwd, reads the lockfile, proxies stdio MCP ↔ the kernel's IPC socket. Also relays channel notifications (`notifications/claude/channel`) back to the client.
- `repld exec [CODE]` — execute Python in a running kernel via IPC. With no args, drops into a minimal interactive REPL (readline history at `~/.repld/history`). With a string arg, runs one-shot and prints the result. Human-facing counterpart to the MCP `exec` tool — same kernel, same namespace, same state.
- `repld init` — idempotent project scaffold: writes `.mcp.json` (adding a `repld` entry if one isn't present) and appends `.pyrepl.lock` / `.pyrepl.sock` to `.gitignore`.
- `repld help [TOPIC]` — agent-facing docs. Single source of truth shared with the MCP `initialize` `instructions` field (`src/repld/help.py:INSTRUCTIONS`). Never let the two drift.

Key design properties:

- **Stdio MCP subprocess** — canonical shape per channel docs. Claude Code spawns it; no always-on daemon, no port management, no gateway.
- **Per-cwd lockfile** — the kernel's IPC path lives in `./.pyrepl.lock`. Both the bridge and `repld exec` inherit `cwd`, read the lockfile, connect.
- **Stdlib REPL** — `compile()` + `eval()` with `PyCF_ALLOW_TOP_LEVEL_AWAIT`. Last-expression auto-display binds to `_` and `_N`. AST split lets `x = 1; "last"` still display the trailing expression. The asyncio loop owns the main process and a separate display thread renders events.
- **Shared asyncio loop** — one process-wide loop on a daemon thread. `asyncio.create_task(...)` works from anywhere, tasks survive the exec return. A watchdog channel-pushes if the loop wedges (default >5s, tunable via `REPLD_LOOP_BLOCK_THRESHOLD`).
- **Stdlib only in core** — zero required dependencies. Optional extras: `repld[pretty]` (rich-rendered display), `repld[web]` (FastAPI/uvicorn for the example), `repld[browser]` (CDP + DuckDB for browser integration).

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

- [x] Stdlib REPL with top-level await, `_` / `__` / `___` / `_N` history, AST-split last-expression display
- [x] Stdio MCP bridge + unix-socket IPC
- [x] `repld`, `repld bridge`, `repld init`, `repld help` CLI subcommands
- [x] MCP tools: `exec`, `get_task`, `cancel` (await-yielding cancellation)
- [x] Always-spill to disk for all cell output + head/tail inline preview
- [x] Human gates (`ask`, `confirm`, `choose`, async) and `notify`
- [x] Loop watchdog (`loop_blocked` channel, env-tunable threshold)
- [x] Asyncio exception handler (`bg_task_error` channel) and `init_error` channel
- [x] `repld exec` — human CLI + interactive REPL over IPC
- [x] `repld[browser]` — CDP integration (async BrowserSession, DuckDB event store, HAR view, Fetch body capture, MCP tools). See `docs/browser.md`.
- [ ] `notify_on_logs` — stdlib logging → channel
- [ ] `@every(seconds)` — periodic channel emission on the shared loop
- [x] `defer(coro, label=None)` — fire-and-forget with channel push on completion
- [x] Gists layer — `./gists/` + `~/.repld/gists/` on sys.path, auto-reload import hook, `scan()` discovery, `introspect()` AST parsing, `repld://gists/{name}` resource templates
- [x] Browser observation pipeline — mutations return tree + network delta + console delta; Playwright-aligned selectors; iframe composition; parent dialog detection
- [x] Browser target hierarchy — nested tabs output, iframe navigate guard (blocked with override)
- [ ] `@watch("/path")` (watchdog) and `@webhook("/path")` (FastAPI)
- [ ] Remote-ask variant of human gates (route via MCP client)
- [ ] Multi-gate concurrency (queue stdin routing across simultaneous gates)
- [ ] Framework presets (`--preset fastapi`, `--preset django`)
- [ ] CI + lint pass
- [ ] Optional Claude Code plugin distribution

## License

MIT (planned).
