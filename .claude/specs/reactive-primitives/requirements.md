# Feature: Pluggable Gate Resolution + Reactive Primitives

## Overview

Two tightly coupled changes that make the kernel reactive:

1. **Gate queue with pluggable resolution** — Replace the singleton `_awaiting_gate` model with a queue of pending gates. Any resolver (stdin, agent via MCP, Telegram bot, web form) can call `resolve_gate(id, value)` — first caller wins. The kernel doesn't care who answers.

2. **Reactive decorators** — `@every(seconds)`, `@watch("/path")`, `@webhook("/path")`. All stdlib, all in core (no `repld[web]` extra). All push to channel on event. Same weight, same pattern.

These ship together because the reactive decorators will fire gates from concurrent handlers, and the current one-at-a-time gate model can't support that.

## What It Does

### Gate Queue

- Multiple gates can be pending simultaneously. Each has a unique id, kind (ask/confirm/choose), prompt, options, timeout, and created_at timestamp.
- `pending_gates()` returns the list of unresolved gates — any resolver can read this.
- `resolve_gate(id, value)` resolves a gate. First caller wins (future settles, subsequent calls are no-ops). Race condition is a feature.
- Channel notification is emitted when a gate is added (already happens today — no change).
- The kernel stdin reader becomes one resolver among many: it reads `pending_gates()`, displays the oldest unresolved gate, resolves it on input, then shows the next.
- `ask()`, `confirm()`, `choose()` public API is unchanged.

### @every(seconds)

- Decorator. Registers a function to run periodically on the shared asyncio loop.
- Each invocation pushes the return value (or output) to channel with `kind=every`.
- Returns a handle the user can cancel.
- The decorated function lives in `__main__` — shared namespace, can access anything.

### @watch(path)

- Decorator. Registers a function to run when a file or directory changes.
- Stdlib-only: poll-based (`os.stat` mtime comparison), no inotify/watchdog dependency.
- Fires on create, modify, delete within the watched path.
- Pushes to channel with `kind=watch` and the changed path in meta.
- Returns a cancellable handle.

### @webhook(path)

- Decorator. Registers an HTTP route on a lightweight stdlib asyncio HTTP server.
- The server starts lazily on first `@webhook` registration (localhost-only, ephemeral port or configurable).
- Incoming POST/PUT body is passed to the decorated function. GET requests get a simple 200.
- Handler return value (or output) pushed to channel with `kind=webhook`.
- Returns a cancellable handle.
- No FastAPI, no uvicorn — raw `asyncio.start_server` with minimal HTTP parsing, or `aiohttp`-style handler on the existing loop.

### Cancellation

- All three decorators return a handle with a `.cancel()` method.
- `cancel()` removes the registration and stops the underlying task.
- Cancelling an `@webhook` route that was the last route tears down the HTTP server.

## Constraints

- Stdlib only. No new dependencies in core.
- One asyncio loop — all reactive tasks run on the kernel's existing loop.
- `push_channel()` is the single emission point (already exists, no new channel mechanism).
- The HTTP server for `@webhook` is localhost-only, consistent with the per-cwd security model.

## Out of Scope

- `notify_on_logs` (stdlib logging hook) — separate, simpler feature.
- Remote-ask protocol extension (MCP-level gate routing) — future layer on top of the pluggable resolution.
- Telegram/web UI gate resolvers — future consumers of `pending_gates()` + `resolve_gate()`.
- Framework presets (`--preset fastapi`, etc.).
- inotify/kqueue native watcher — poll is sufficient; can be swapped later without API change.
