# Architecture

Design rationale, system architecture, and project status for repld. For user-facing documentation see [README.md](../README.md).

## Why

Two things happened at once:

**1. Traditional REPL-agent integration is miserable.** PTY transport means fake keystrokes and prompt parsing. State disappears between script runs. Long jobs block the whole turn. Most "agent does thing in Python" setups work around this by writing files and running them — losing all the iteration speed a REPL is supposed to provide.

**2. The agent collapses the library moat.** Selenium, Puppeteer, BeautifulSoup, ORMs, form-filling kits, OpenAPI client generators — these existed because writing orchestration code used to be expensive. An LLM with access to CDP + `httpx` + a live SQL connection writes the equivalent code on demand, tuned to the exact task, against the exact page/API/schema. The library becomes overhead. Per-service MCP servers scale linearly — one per service, maintained forever. repld replaces them all: attach to your logged-in browser, discover the API surface from the traffic, synthesize a client.

`repld` is what falls out when you take both seriously.

## Two modes

**Dev shell for existing projects.** Drop `.mcp.json` into an existing FastAPI/Django/Flask app, `repld --init repl.py` to pre-load the app + DB session, and the agent has a live handle on your running service's memory. Faster than `pytest -k` for ad-hoc verification; faster than DBeaver for ad-hoc queries. Zero changes to your app.

**Autonomous agent runtime.** Set up watchers (`@every`, `@watch`, `@webhook`), give the agent the clients it needs (captured via CDP from your logged-in browser tabs), and the agent processes inbound events on its own between turns. The kernel is the cron + systemd + webhook receiver; the agent is the action layer.

## System architecture

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

Six CLI subcommands, all dispatched from `repld:main`:

- `repld` — long-running Python kernel in the project cwd. Writes `./.pyrepl.lock` with `{pid, socket_path}`; listens on a unix-domain socket for IPC.
- `repld bridge` — short-lived stdio MCP subprocess spawned by Claude Code via `.mcp.json`. Inherits cwd, reads the lockfile, proxies stdio MCP ↔ the kernel's IPC socket. Also relays channel notifications (`notifications/claude/channel`) back to the client.
- `repld exec [CODE]` — execute Python in a running kernel via IPC. With no args, drops into a minimal interactive REPL (readline history at `~/.repld/history`). With a string arg, runs one-shot and prints the result.
- `repld init` — idempotent project scaffold: writes `.mcp.json` (adding a `repld` entry if one isn't present) and appends `.pyrepl.lock` / `.pyrepl.sock` to `.gitignore`.
- `repld help [TOPIC]` — agent-facing docs. Single source of truth shared with the MCP `initialize` `instructions` field.
- `repld gist <name>` — scaffold a tool gist in `./gists/<name>.py` with `__repld_tools__` declaration and `_tool_*` handler skeleton.

## Design properties

- **Stdio MCP subprocess** — canonical shape per channel docs. Claude Code spawns it; no always-on daemon, no port management, no gateway.
- **Per-cwd lockfile** — the kernel's IPC path lives in `./.pyrepl.lock`. Both the bridge and `repld exec` inherit `cwd`, read the lockfile, connect.
- **Stdlib REPL** — `compile()` + `eval()` with `PyCF_ALLOW_TOP_LEVEL_AWAIT`. Last-expression auto-display binds to `_` and `_N`. AST split lets `x = 1; "last"` still display the trailing expression.
- **Shared asyncio loop** — one process-wide loop on a daemon thread. `asyncio.create_task(...)` works from anywhere, tasks survive the exec return. A watchdog channel-pushes if the loop wedges.
- **Stdlib only in core** — zero required dependencies. Optional extras: `repld[pretty]` (rich-rendered display), `repld[browser]` (CDP + DuckDB for browser integration).

## Design principles

- **Substrate, not library.** Primitives composable by the agent, not a feature catalog. The LLM writes the integration code against live pages/APIs/DBs — repld just gives it a persistent place to run, observe, and react.
- **No per-service MCP.** Don't write a Slack-MCP + GitHub-MCP + PowerOffice-MCP. Capture auth once (CDP, env, OAuth), hold the client in the namespace, let the agent compose.
- **One process, shared state.** Human and agent operate on the same memory. Stage in one, read from the other.
- **Async-native.** Nothing blocks the turn. Long jobs push to channel on completion.
- **Honest scope.** Not for production. Dev dependency only. Your app's memory is live to whoever can connect.

## Status

Research preview. The thesis is validated — full MCP-over-stdio with channel push and top-level await works end-to-end.

- [x] Stdlib REPL with top-level await, `_` / `__` / `___` / `_N` history, AST-split last-expression display
- [x] Stdio MCP bridge + unix-socket IPC
- [x] `repld`, `repld bridge`, `repld init`, `repld help` CLI subcommands
- [x] MCP tools: `exec`, `get_task`, `cancel` (await-yielding cancellation)
- [x] Always-spill to disk for all cell output + head/tail inline preview
- [x] Human gates (`ask`, `confirm`, `choose`, async) and `notify`
- [x] Loop watchdog (`loop_blocked` channel, env-tunable threshold)
- [x] Asyncio exception handler (`bg_task_error` channel) and `init_error` channel
- [x] `repld exec` — human CLI + interactive REPL over IPC
- [x] `repld[browser]` — CDP integration (async BrowserSession, DuckDB event store, HAR view, Fetch body capture, MCP tools)
- [x] `defer(coro, label=None)` — fire-and-forget with channel push on completion
- [x] `@every(seconds)` — periodic ticker on the shared loop
- [x] Gists layer — `./gists/` + `~/.repld/gists/` on sys.path, auto-reload import hook, `scan()` discovery, `introspect()` AST parsing, `repld://gists/{name}` resource templates
- [x] Gist tools — `__repld_tools__` declaration + `_tool_*` handlers, auto-discovery in `tools/list`, `repld gist` scaffolding
- [x] Browser observation pipeline — mutations return tree + network delta + console delta; Playwright-aligned selectors; iframe composition; parent dialog detection
- [x] Browser target hierarchy — nested tabs output, iframe navigate guard
- [ ] `notify_on_logs` — stdlib logging → channel
- [ ] `@watch("/path")` — poll-based file watcher → channel (stdlib only)
- [ ] `@webhook("/path")` — stdlib asyncio HTTP server → channel
- [ ] Pluggable gate resolution (queue + first-resolver-wins)
- [ ] Framework presets (`--preset fastapi`, `--preset django`)
- [ ] CI + lint pass
