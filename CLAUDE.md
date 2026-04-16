# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status

Early scaffolding. `src/repld/__init__.py` is a stub `main()`; the README is the design document and roadmap. When implementing, treat README.md as the spec — especially the **Architecture**, **Tools**, **Helpers**, and **Status** (checklist) sections. Don't drift from the shape described there without discussion.

## Build & run

Python 3.12+, managed with **uv** using the `uv_build` backend (see `pyproject.toml`).

```bash
uv sync                 # install deps into .venv
uv run repld            # runs the `repld:main` entrypoint
uv build                # wheel + sdist via uv_build
```

There are no tests, linters, or CI configured yet. If you add any, update this file.

## Architecture (target shape)

Two CLI entrypoints are planned, both dispatched from `repld:main`:

- `repld` — long-running Python kernel in the project cwd. Writes `./.pyrepl.lock` with `{pid, socket_path}`; listens on a unix-domain socket for IPC.
- `repld bridge` — short-lived stdio MCP subprocess spawned by Claude Code via `.mcp.json`. Inherits cwd, reads the lockfile, proxies stdio MCP ↔ the kernel's IPC socket. Also relays channel notifications (`notifications/claude/channel`) back to the client.

Key invariants to preserve when building this out:

- **One process, one asyncio loop.** The kernel owns a single shared loop so `asyncio.create_task(...)` from any exec call survives past the exec return and can push to channel on completion.
- **`exec` returns fast or defers.** If user code finishes within `timeout` (default 2s) return inline; otherwise return `{task_id, done: false}` and push a channel notification on completion. Every cell with output spills to `$XDG_RUNTIME_DIR/repld/{pid}-{tid}.out` from byte 1; the inline response carries a head+tail preview and the absolute spill path. Agents use the standard Read/Grep tools on that path — there is no `read_spill` MCP tool.
- **Stdlib only in core.** Zero required runtime dependencies. Optional extras (`repld[pretty]` for rich-rendered display, `repld[web]` for the FastAPI example) gate anything heavier. Don't pull new deps into the base package.
- **Per-cwd, localhost-only.** The IPC socket stays on `127.0.0.1` or a user-only unix socket. This is a dev-time tool; never add anything that would make it safe to expose.

## Design principles (from README)

- **Substrate, not library.** Expose small composable primitives (`notify`, `defer`, `@every`, `@watch`, `@webhook`, `browser.find`) and let the LLM write integration code against live pages/APIs/DBs. Resist adding per-service helpers.
- **Channel push over polling.** Long jobs, file watchers, webhooks, and timers all surface as `<channel>` injections rather than requiring the agent to poll.
- **Shared `__main__` namespace.** Human and agent operate on the same module dict — don't sandbox the agent into an isolated scope.
