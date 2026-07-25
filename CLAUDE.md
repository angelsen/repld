# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status

Research preview. The kernel, bridge, MCP protocol (exec / get_task / cancel), human gates, channel infrastructure, and scaffolding commands are live. When implementing, treat `docs/ARCHITECTURE.md` as the design spec (architecture, status checklist, design principles) and this file for subsystem details and invariants. README.md is user-facing only. Don't drift from the shape described here without discussion.

## Build & run

Python 3.12+, managed with **uv** using the `uv_build` backend (see `pyproject.toml`).

```bash
uv sync                                 # install deps into .venv
uv run repld                            # runs the `repld:main` entrypoint
uv build                                # wheel + sdist via uv_build
uv run tests/smoketest.py --phase 12          # end-to-end smoketest
ruff check --fix && ruff format && basedpyright   # lint / format / type-check
```

No CI configured yet. If you add any, update this file.

## Releasing

Published to PyPI as `repld-tool`. Manual, no CI. The local `uv` is a wrapper
(`~/.local/bin/wrappers/uv`) that automates most of it — raw uv is `@ uv`.

```bash
# 1. Accrue changelog notes under CHANGELOG.md [Unreleased] as you work, and COMMIT them.
#    The bump needs a clean tree and promotes [Unreleased] → [X.Y.Z].
uv version --bump patch        # bumps pyproject + uv.lock, promotes changelog, commits "release repld-tool X.Y.Z", tags vX.Y.Z
rm -f dist/* && uv build       # clean stale artifacts first — publish refuses mixed versions
git push origin master --tags
uv publish                     # prints a review summary + a confirmation token (10-min TTL)
uv publish --confirm <token>   # GPG prompt for the PyPI token (from `pass pypi/uv-publish`), then uploads
```

Gotchas the wrapper enforces: clean working tree before `version --bump`; only
the target version in `dist/` before publish (it blocks on leftovers from a
prior release). Verify with the simple index (fast) — the JSON API lags:
`curl -s https://pypi.org/simple/repld-tool/ | grep X.Y.Z`. CHANGELOG covers
*packaged* changes only — `gists/` is not in the wheel.

## Testing

`tests/smoketest.py` is the entire test suite — no pytest setup. It starts a real kernel + bridge subprocess and drives MCP JSON-RPC over stdio. `--phase N` runs phases 1..N (default 3, current ceiling 15). When you add a feature, extend a phase rather than introducing a separate harness. Each phase lives in its own file under `tests/phases/` (e.g. `core.py`, `channels.py`, `defer.py`).

Phases:
- **2–3:** Core MCP plumbing — initialize, tools/list, sync exec, deferred exec, get_task polling.
- **4:** Channel notifications — task_done push, notify() from user code, pre-gate queuing.
- **5:** Single-kernel flock mutex (a second boot in the same project exits 0 and leaves the incumbent's lockfile intact), `--init` file execution.
- **6:** Tool registration, gist auto-reload, browser integration (requires Chrome with `--remote-debugging-port=9222`).
- **7:** `defer()` — fire-and-forget with channel push on completion.
- **8:** Gist resources — `resources/list` includes one entry per loaded gist (with first-docstring-line as description); `resources/read repld://gists/{name}` returns the introspected API.
- **9:** Gist-registered MCP tools — `__repld_tools__` discovery, dispatch, auto-reload, error handling.
- **10:** `@every(seconds)` decorator — periodic ticker, immediate first tick, error survival, `cancel()` / `cancel_all()`.
- **11:** Graceful shutdown — `_shutdown` drains `@every` + `defer()` `try/finally` blocks before stopping the loop, with a 2 s budget.
- **12:** Cross-project gist links — `add_link` resolves via the registry + AST sibling co-link, writes `./gists/.links`; a fresh kernel boots with the manifest and the linked gist imports (sibling resolving); stale entries skipped at load + pruned by `rm --stale`.
- **13:** Session registry — kernel boot writes `$XDG_RUNTIME_DIR/repld/sessions/<pid>.json`; `list_sessions()` returns it; file is removed after SIGTERM shutdown.
- **14:** Dashboard HTTP — Host-header allowlist serves loopback Hosts and 403s rebound hostnames (DNS-rebinding guard).
- **15:** Headless kernel — bridge auto-spawns a kernel, heals across kernel death (in-flight request answered `-31001`, handshake replayed, channel push still lands), targeted vs. broadcast push, event log read back by `repld log`, `repld status`, and racing boots collapsing to one kernel.

## Key subsystems

All source lives under `src/repld/`. Individual files are self-describing; what matters is how they connect:

**Threading model:** The kernel runs the asyncio loop on a daemon thread (`run_forever`); the main thread runs the display consumer (or parks on a stop event in `--no-display` mode). IPC accept runs on its own thread, spawning per-connection reader threads that call into the dispatcher. This matters because user code in `exec` runs on the daemon thread's loop — blocking calls will stall the kernel.

**Request flow:** Claude Code spawns `bridge.py` (stdio MCP) → bridge reads `$XDG_RUNTIME_DIR/repld/projects/<slug>/kernel.lock` (spawning a headless kernel if there isn't one) → proxies JSON-RPC over unix socket (`ipc.py`) → `protocol.py` dispatches to `exec`, `get_task`, `cancel`, or browser tools → `runtime.py` runs code in `__main__` → results (or `task_id` for deferred work) flow back. Channel notifications (`events.py`) flow kernel → bridge → Claude Code.

**Builtins injected into `__main__`:** The kernel injects these names at startup — they are the user/agent-facing API surface: `notify(content, **meta)`, `defer(coro, label)`, `every(seconds)` (decorator), `ask(prompt)`, `confirm(prompt)`, `choose(prompt, options)`, `browser` (lazy descriptor, only when `repld[browser]` is installed).

**Doc system (`help.py`):** Agent-facing docs are split across non-overlapping surfaces. Keep them in sync:
1. **INSTRUCTIONS** (dynamic) — behavioral model composed at MCP init by `build_instructions()`. Includes exec model always; browser model only when `browser` exists in `__main__`; gist signatures extracted via AST from available gists. This is what the agent reasons with. Terse; always loaded.
2. **Tool descriptions** — per-tool what + gotchas, defined in `protocol.py`.
3. **Topics** — pure API reference for `repld help <topic>`, defined as `_TOPICS` in `help.py`.
4. **MCP resources** — on-demand docs read via `repld://docs/*`, each a standalone constant in `help.py`: `GUIDE` (`docs/guide`, execution model + gist patterns + conventions), `BROWSER_GUIDE` (`docs/browser`, full browser API reference), `PLAYBOOK` (`docs/playbook`, workflow-building methodology), `PRODUCTION` (`docs/production`, graduating a gist's core logic out of repld into FastMCP/FastAPI/etc). Registered in `protocol.py`'s resource list; read on demand rather than always loaded.

**Browser (`browser/`):** 18 MCP tools registered dynamically by `protocol.py` only when `browser` exists in `__main__` (i.e. `repld[browser]` extra is installed). CDP integration via WebSocket multiplexer. DuckDB event store for network/console queries (HAR-style). Fetch domain interception for proactive body capture is enabled on `get()`/`open()` tabs; `watch()` tabs attach lightweight with on-demand body access via `Network.getResponseBody`. Observation pipeline (`observe.py`) returns accessibility tree + network delta + console delta after mutations. Pin/gate bridge: `tab.pin(reason)` injects a floating pill via `Runtime.evaluate` + `beforeunload` guard; `tab.confirm()`/`tab.choose()` route human gates to the pill UI; button clicks flow back via `Runtime.bindingCalled` → `resolve_gate()`. Terminal and browser resolve the same Future — first wins. See `docs/browser.md` for full design rationale.

**Dashboard (`dashboard.py`):** Pure-stdlib async HTTP server on an ephemeral port. Serves an inline HTML control panel (GET /) and a JSON-RPC API (POST /api) exposing kernel state: pid, uptime, active tasks, tickers, connected browser tabs. Port written to the hint file (`kernel.dashboard` in the project's runtime dir), which also carries the API token and the browser restore state.

**Gist system (`gists.py`, `gist_deps.py`, `gist_links.py`):** Custom import hook (`_GistFinder` + `_GistImportHook`) wraps `builtins.__import__`, tracks mtimes, evicts stale modules from `sys.modules` on re-import. Module docstring first line → auto-injected into MCP instructions. Override with `__repld_help__ = "..."`. Constructor signatures extracted via AST and shown alongside the description. Gists can also register MCP tools via `__repld_tools__` — `scan_tools()` discovers tool schemas across all gist files, `resolve_tool(name)` imports the owning gist and returns its `_tool_{name}` handler. Tools appear in `tools/list` automatically alongside built-in tools. Gists declare external dependencies via `__repld_deps__ = ["httpx>=0.27"]` — `gist_deps.scan_deps()` AST-scans at boot, `gist_deps.install_deps()` prompts interactively and installs into the tool venv via `uv pip install`. Every import is recorded in a central registry (`~/.config/repld/gist-registry.json`, name → path/project/last_used). **Cross-project links (`gist_links.py`):** `repld gist add <name>` resolves a registered gist's path, AST-follows its same-dir sibling imports, and records absolute paths in a committed `./gists/.links` manifest — without copying. At boot `_load_links()` populates the `_linked` name→path overlay (consulted by `_GistFinder` + `_iter_gist_files` *after* local dirs, so local gists always shadow); stale entries are skipped (never auto-rewritten — the manifest is committed). `repld gist list` shows local + linked (flagging stale), `repld gist rm <name>` / `rm --stale` unlink. The three modules share helpers via an intentional call-time-attribute import cycle (`gists._parse`, `gists.registry`, `gist_links._linked`, ...).

## Architecture (target shape)

Eleven CLI subcommands, all dispatched from `repld:main`:

- `repld` — long-running Python kernel for the cwd, with the live TUI display. Takes the project's flock mutex (`kernel.flock`) and writes `kernel.lock` with `{pid, socket_path, cwd, started_at, dashboard_port}`; listens on a unix-domain socket. Losing the flock is not an error — it prints a note and exits 0 so the incumbent is adopted, never raced.
- `repld bridge` — stdio MCP subprocess spawned by Claude Code via `.mcp.json`. Inherits cwd, guarantees a kernel exists, proxies stdio MCP ↔ the kernel's IPC socket, and relays channel notifications (`notifications/claude/channel`) back to the client. See the slim-loader invariants below.
- `repld log` / `status` / `stop` / `restart` / `dashboard` — observe and control a kernel this terminal never started (`log_cmd.py`, `lifecycle_cmd.py`, `dashboard_cmd.py`).
- `repld init` — idempotent project scaffold: writes `.mcp.json` (adding a `repld` entry if one isn't present) and the CLAUDE.md block. No `.gitignore` writes: nothing repld creates at runtime lands in the project directory.
- `repld help [TOPIC]` — agent-facing docs. Single source of truth shared with the MCP `initialize` `instructions` field (`src/repld/help.py:INSTRUCTIONS`). Never let the two drift.
- `repld exec [CODE]` — human-facing CLI. With no args, interactive REPL over IPC (shared namespace). With a string arg, one-shot execution. Same kernel, same state as the agent.
- `repld gist` — command group: `new <name>` (scaffold `./gists/<name>.py`), `add <name>` (link a registered gist from another project), `rm <name>` / `rm --stale` (unlink), `list` (local + linked + linkable-from-registry). Unknown verbs error (no verb-less scaffold alias). Top-level CLI dispatch is a single `_SUBCOMMANDS` table in `cli.py` driving both dispatch and `--help`.
- `repld browser [ARGS...]` — re-exec `repld ARGS...` under `uv run` with the `browser` extra (`duckdb`+`websockets`), so browser features work without adding `repld-tool` to the project's dependencies. Preserves a local editable checkout (detected via `direct_url.json` distribution metadata) instead of silently falling back to the published package, so dev changes are picked up (`src/repld/relaunch.py`).

Key invariants to preserve when building this out:

- **One process, one asyncio loop.** The kernel owns a single shared loop so `asyncio.create_task(...)` from any exec call survives past the exec return and can push to channel on completion.
- **`exec` returns fast or defers.** If user code finishes within `timeout` (default 2s) return inline; otherwise return `{task_id, done: false}` and push a channel notification on completion. Every cell with output spills to `$XDG_RUNTIME_DIR/repld/{pid}-{tid}.out` from byte 1; the inline response carries a head+tail preview and the absolute spill path. Agents use the standard Read/Grep tools on that path — there is no `read_spill` MCP tool.
- **Stdlib only in core.** Zero required runtime dependencies. Optional extras (`repld[pretty]` for rich-rendered display) gate anything heavier. Don't pull new deps into the base package.
- **Per-cwd, localhost-only.** The IPC socket stays on `127.0.0.1` or a user-only unix socket. This is a dev-time tool; never add anything that would make it safe to expose.
- **Runtime state lives under XDG, never in the project.** `paths.py` is the single source of truth: `$XDG_RUNTIME_DIR/repld/projects/{basename}-{sha256(realpath)[:8]}/`, created 0700. Every file is the socket path with a different suffix (`kernel.sock` → `.lock` / `.flock` / `.dashboard` / `.events`), so `--socket` / `REPLD_SOCKET` moves the whole set coherently. Don't reintroduce a project-directory fallback.
- **One kernel per project, enforced by flock.** `kernel.flock` is opened `O_CREAT` *without* `O_TRUNC` and never replaced — `kernel.lock` is rewritten atomically via `os.replace`, which would move the inode out from under a flock held on it. That's why they're two files.
- **The bridge is a stateful proxy, not a byte-pipe.** It must never close its own stdout (MCP client dispatchers are single-shot and cannot re-handshake), must cache the client's `initialize` and replay it onto every fresh kernel (a respawned kernel is uninitialized and would queue channel pushes forever), must answer orphaned in-flight ids with `-31001`, and must probe liveness *before* forwarding rather than retrying after a failure — `exec` runs arbitrary code and must never run twice. Diagnostics go to stderr only.
- **Targeted push for call-scoped output, broadcast for ambient.** A task carries the `ipc.Session` that started it (`task["origin"]`); `_maybe_push_done` routes its completion there. `@every` errors, console errors, browser connect/disconnect, and bare `notify()` stay broadcast. If the origin disconnected, drop the push — never fall back to broadcast. The local `events.emit(ChannelPush(...))` fires either way, so `repld log` still sees it.

## Design principles (from README)

- **Substrate, not library.** Expose small composable primitives (`notify`, `defer`, `@every`, `@watch`, `@webhook`, `browser.get`) and let the LLM write integration code against live pages/APIs/DBs. Resist adding per-service helpers.
- **Channel push over polling.** Long jobs, file watchers, webhooks, and timers all surface as `<channel>` injections rather than requiring the agent to poll.
- **Shared `__main__` namespace.** Human and agent operate on the same module dict — don't sandbox the agent into an isolated scope.
