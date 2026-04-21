# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status

Research preview. The kernel, bridge, MCP protocol (exec / get_task / cancel), human gates, channel infrastructure, and scaffolding commands are live; the README is the design document and roadmap. When implementing, treat README.md as the spec — especially the **Architecture**, **Tools**, **Helpers**, and **Status** (checklist) sections. Don't drift from the shape described there without discussion.

## Build & run

Python 3.12+, managed with **uv** using the `uv_build` backend (see `pyproject.toml`).

```bash
uv sync                                 # install deps into .venv
uv run repld                            # runs the `repld:main` entrypoint
uv build                                # wheel + sdist via uv_build
uv run python tests/smoketest.py --phase 8   # end-to-end smoketest
ruff check --fix && ruff format && basedpyright   # lint / format / type-check
```

No CI configured yet. If you add any, update this file.

## Testing

`tests/smoketest.py` is the entire test suite — no pytest setup. It starts a real kernel + bridge subprocess and drives MCP JSON-RPC over stdio. Phases grow with implementation; `--phase N` runs phases 1..N (default 3, current ceiling 8). When you add a feature, extend a phase rather than introducing a separate harness.

## Source layout

- `cli.py` — arg parsing + subcommand dispatch for `repld:main`.
- `kernel.py` — long-running process: asyncio loop, IPC server, display thread, watchdog.
- `bridge.py` — stdio MCP subprocess; reads `.pyrepl.lock`, proxies to the kernel socket, relays channel notifications.
- `ipc.py` / `protocol.py` — unix-socket framing and MCP JSON-RPC shapes + tool dispatch.
- `runtime.py` — `compile()` + `eval()` with top-level await, AST-split last-expression display, `_`/`_N` history.
- `tasks.py` — task registry, spill files, head/tail previews, `get_task`/`cancel`.
- `gates.py` — `ask` / `confirm` / `choose` (human input routed through the kernel pane).
- `events.py` / `display.py` — channel event plumbing and terminal rendering.
- `help.py` — `build_instructions()` composes dynamic MCP instructions at init time (exec model + browser model if active + available gists). Also serves `repld help <topic>`.
- `gists.py` — gist discovery (`scan`), AST introspection (`introspect`), auto-reload import hook.
- `scaffold.py` — `repld init` logic (`.mcp.json` merge + `.gitignore` append).
- `exec_cmd.py` — `repld exec` interactive REPL + one-shot CLI over IPC.
- `browser/` — CDP integration package:
  - `__init__.py` — `Browser` class, `LazyBrowser` descriptor, `make_target()`.
  - `session.py` — WebSocket multiplexer, target discovery, pattern-based auto-attach.
  - `tab.py` — `Tab` facade (js, click, type_text, fetch, navigate, network, console queries).
  - `cdp.py` — `CDPSession` per-target command router + DuckDB event store.
  - `observe.py` — observation pipeline (pre/post_observe, accessibility tree, settle, deltas, parent dialog detection).
  - `capture.py` — Fetch domain interception for request/response body capture.
  - `har.py` — DuckDB schema + HAR/console views.

## Architecture (target shape)

Five CLI subcommands, all dispatched from `repld:main`:

- `repld` — long-running Python kernel in the project cwd. Writes `./.pyrepl.lock` with `{pid, socket_path}`; listens on a unix-domain socket for IPC.
- `repld bridge` — short-lived stdio MCP subprocess spawned by Claude Code via `.mcp.json`. Inherits cwd, reads the lockfile, proxies stdio MCP ↔ the kernel's IPC socket. Also relays channel notifications (`notifications/claude/channel`) back to the client.
- `repld init` — idempotent project scaffold: writes `.mcp.json` (adding a `repld` entry if one isn't present) and appends `.pyrepl.lock` / `.pyrepl.sock` to `.gitignore`.
- `repld help [TOPIC]` — agent-facing docs. Single source of truth shared with the MCP `initialize` `instructions` field (`src/repld/help.py:INSTRUCTIONS`). Never let the two drift.
- `repld exec [CODE]` — human-facing CLI. With no args, interactive REPL over IPC (shared namespace). With a string arg, one-shot execution. Same kernel, same state as the agent.

Key invariants to preserve when building this out:

- **One process, one asyncio loop.** The kernel owns a single shared loop so `asyncio.create_task(...)` from any exec call survives past the exec return and can push to channel on completion.
- **`exec` returns fast or defers.** If user code finishes within `timeout` (default 2s) return inline; otherwise return `{task_id, done: false}` and push a channel notification on completion. Every cell with output spills to `$XDG_RUNTIME_DIR/repld/{pid}-{tid}.out` from byte 1; the inline response carries a head+tail preview and the absolute spill path. Agents use the standard Read/Grep tools on that path — there is no `read_spill` MCP tool.
- **Stdlib only in core.** Zero required runtime dependencies. Optional extras (`repld[pretty]` for rich-rendered display, `repld[web]` for the FastAPI example) gate anything heavier. Don't pull new deps into the base package.
- **Per-cwd, localhost-only.** The IPC socket stays on `127.0.0.1` or a user-only unix socket. This is a dev-time tool; never add anything that would make it safe to expose.

## Design principles (from README)

- **Substrate, not library.** Expose small composable primitives (`notify`, `defer`, `@every`, `@watch`, `@webhook`, `browser.find`) and let the LLM write integration code against live pages/APIs/DBs. Resist adding per-service helpers.
- **Channel push over polling.** Long jobs, file watchers, webhooks, and timers all surface as `<channel>` injections rather than requiring the agent to poll.
- **Shared `__main__` namespace.** Human and agent operate on the same module dict — don't sandbox the agent into an isolated scope.
