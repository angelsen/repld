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

$XDG_RUNTIME_DIR/repld/        → 0700; /tmp/repld-{uid} if XDG is unset
 └─ projects/<slug>/            → per-project runtime state, 0700
     ├─ kernel.sock             → unix-domain IPC socket
     ├─ kernel.lock             → {pid, socket_path, cwd, dashboard_port}
     ├─ kernel.flock            → single-kernel-per-project mutex
     ├─ kernel.dashboard        → dashboard port/token + browser restore hint
     ├─ kernel.events           → NDJSON event log (`repld log` reads this)
     └─ kernel.cache            → last-computed instructions/tools/resources,
                                  read by the bridge before a kernel exists
 └─ sessions/<pid>.json         → user-scoped index of every live kernel
 └─ {pid}-{task_id}.out         → task spill files (per-process, not per-project)

Every file above is created 0600, the directories 0700 — the tree holds cell
output and socket paths, and the /tmp fallback has no protected parent.

Terminal 1: `claude …`          spawns `repld bridge` via stdio MCP.
                                Attaches to a kernel already running, or —
                                if none is — answers MCP discovery from
                                kernel.cache and starts one lazily on first
                                real tool call. Heals it if it dies.
Terminal 2 (optional): `repld`  Kernel with the live TUI display, when you
                                want to watch it rather than `repld log -f`
Terminal 3: `repld exec`        Human REPL / one-shot CLI, same IPC socket
```

Eleven CLI subcommands, all dispatched from `repld:main`:

- `repld` — long-running Python kernel for the cwd, with the live display. Takes a flock mutex; if a kernel already owns the project it prints a note and exits 0 rather than competing.
- `repld bridge` — stdio MCP subprocess spawned by Claude Code via `.mcp.json`. Inherits cwd; attaches to a kernel that's already running, or, if none exists, answers MCP discovery from `kernel.cache` and spawns a headless one lazily on the first real tool call. Proxies stdio MCP ↔ the kernel's IPC socket. Survives kernel death: it replays the client's handshake onto a fresh kernel and answers orphaned requests with `-31001`. Also relays channel notifications (`notifications/claude/channel`) back to the client.
- `repld exec [CODE]` — execute Python in a running kernel via IPC. With no args, drops into a minimal interactive REPL (readline history at `~/.repld/history`). With a string arg, runs one-shot and prints the result.
- `repld log [-n N] [-f] [--json]` — replay (or follow) the kernel's event log: the same cells, output, and channel pushes the TUI renders, for a kernel with no pane.
- `repld status [--json]` — this project's kernel (pid, uptime, socket, dashboard, active tasks/tickers) plus every live kernel elsewhere, so auto-spawned ones don't accumulate unseen.
- `repld stop [--all]` — SIGTERM this project's kernel and wait for it to clear; `--all` stops every live kernel in the session registry.
- `repld restart` — stop, then spawn a fresh headless kernel.
- `repld dashboard [--print]` — resolve the dashboard port and open it, printing the URL when a browser can't be opened.
- `repld init` — idempotent project scaffold: writes `.mcp.json` (adding a `repld` entry if one isn't present) and the CLAUDE.md block. No `.gitignore` changes — nothing repld writes lands in the project directory.
- `repld help [TOPIC]` — agent-facing docs. Single source of truth shared with the MCP `initialize` `instructions` field.
- `repld gist <verb>` — `new` / `add` / `rm` / `list` / `lint` for tool gists in `./gists/`.

## Design properties

- **Stdio MCP subprocess** — canonical shape per channel docs. Claude Code spawns it; no port management, no gateway. The kernel it manages persists until explicitly stopped, so in-memory state survives a Claude Code restart.
- **Per-cwd runtime dir** — the kernel's IPC path lives in `$XDG_RUNTIME_DIR/repld/projects/<slug>/kernel.lock`, where `<slug>` is `{basename}-{sha256(realpath)[:8]}`. Both the bridge and `repld exec` inherit `cwd`, resolve the same slug, read the lockfile, connect. `--socket` / `REPLD_SOCKET` override it, and every sibling file follows the socket by suffix.
- **One kernel per project, enforced by flock** — the winner holds the fd for its whole life; a loser exits 0 without touching the winner's lockfile. That is what makes an externally-started kernel adopted rather than raced.
- **The bridge outlives the kernel** — MCP client dispatchers are single-shot, so a bridge that exits on kernel EOF would end the session permanently. It never closes its own stdout, caches the client's `initialize` for replay onto a fresh kernel, and probes liveness *before* forwarding (never retries after a failure — `exec` runs arbitrary code and must not run twice).
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
- [x] Gist tools — typed `_tool_*` handlers with inferred schemas, auto-discovery in `tools/list`, `repld gist` scaffolding
- [x] Browser observation pipeline — mutations return tree + network delta + console delta; Playwright-aligned selectors; iframe composition; parent dialog detection
- [x] Browser target hierarchy — nested tabs output, iframe navigate guard
- [x] Ready signal — `browser.get(ready=selector)`, session recovery on HMR, navigate/reload wait
- [x] Touch input — `tab.tap()`, `tab.swipe()`, 3s timeout for blocking handlers
- [x] No-focus-steal selectors — CSS selectors use `DOM.querySelector` + `DOM.getContentQuads` (no JS eval)
- [x] Gist dependency management — `__repld_deps__` declaration, boot-time scan + interactive install prompt
- [x] Dynamic `__version__` — `importlib.metadata.version("repld-tool")`, `repld --version` CLI flag
- [x] `tab.wait_for_idle()` — network idle detection exposed on Tab API; replaces hardcoded 300ms in ready signal
- [x] XDG runtime paths — socket/lockfile/hint/event log under `$XDG_RUNTIME_DIR/repld/projects/<slug>/`; nothing lands in the project directory
- [x] Single-kernel flock mutex — losers exit 0, externally-started kernels are adopted
- [x] Slim loader — `repld bridge` auto-spawns a headless kernel and heals it across kernel death (handshake replay, `-31001` for orphaned requests)
- [x] Lazy kernel spawn — MCP discovery served from `kernel.cache` when no kernel is running; a real tool call is what actually spawns one, so a session that never uses repld never pays for it
- [x] Targeted channel push — a task's completion notifies the session that started it; ambient pushes stay broadcast
- [x] Event log + `repld log` / `status` / `stop` / `restart` / `dashboard` — a headless kernel is observable and controllable from any terminal
- [ ] `notify_on_logs` — stdlib logging → channel
- [ ] `@watch("/path")` — poll-based file watcher → channel (stdlib only)
- [ ] `@webhook("/path")` — stdlib asyncio HTTP server → channel
- [ ] Pluggable gate resolution (queue + first-resolver-wins)
- [ ] Framework presets (`--preset fastapi`, `--preset django`)
- [ ] CI + lint pass
