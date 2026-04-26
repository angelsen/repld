# Feature: `@every(seconds)` — periodic decorator

## Overview

A decorator that schedules a function (sync or async) to run repeatedly on
the kernel's shared asyncio loop. Each tick can push to channel via the
function's return value. Lives in `__main__` next to `defer`, `notify`,
`ask`, `confirm`, `choose`.

This is the missing primitive that turns one-shot work into continuous
automation: the agent observes a manual workflow, wraps it in `@every(60)`,
and the kernel takes over the cadence.

## Specification Heritage

- **Evolved from:** `.claude/specs/reactive-primitives/` (Task 2 of that bundle)
- **Changed:**
  - Extracted as a standalone spec — gate queue, `@watch`, `@webhook` stay parked.
  - Drops the `reactive.init(loop)` module-level state in favor of the
    factory-closure pattern that `_make_defer(loop)` already uses in
    `kernel.py`. No `_loop_ref` global.
  - Returns the original function with `.cancel()` / `.handle` attached
    so `@every` is decorator-shaped (the function name still binds to
    something callable). The handle also lives in a registry for
    `every.list()` introspection.
  - Runs the first tick **immediately**, then every `seconds`. The
    original spec implied sleep-first; immediate-first is more useful
    for repld's "see it work, then leave it running" flow.

## What It Does

- `@every(seconds)` decorates a function (sync or async, zero args). The
  function runs immediately, then repeatedly every `seconds` on the
  kernel's shared loop.
- The decorator returns the original function so the name still binds.
  A `.handle` attribute (and `.cancel()` shortcut) lets the user stop it.
- If the function returns a non-`None` value, that value is pushed to
  channel with `kind=every`, `label=<fn name or explicit label>`. If it
  returns `None`, the tick is silent.
- If the function raises, the exception is caught, pushed to channel
  with `kind=every`, `label=...`, `error=1`, and the loop continues.
  One bad tick does not stop the schedule.
- `every.list()` returns the active handles. `every.cancel_all()` stops
  all of them. Useful in `repl.py` reload cycles.
- All scheduled tasks cancel cleanly on kernel shutdown.

## Watchdog Escalation

The existing loop watchdog detects blocks >5s and warns via channel. This
spec adds a kill threshold: after `REPLD_LOOP_KILL_THRESHOLD` seconds
(default 30), the watchdog cancels the longest-running asyncio task. This
directly benefits `@every` — a stuck tick gets killed, the decorator
catches `CancelledError`, and the next tick proceeds normally.

## Constraints

- Stdlib only.
- Runs on the kernel's existing shared loop — no new thread, no new loop.
- Routes through the existing `push_channel()`. No new emission path.
- Decoration from the exec thread schedules onto the loop via
  `asyncio.run_coroutine_threadsafe`, matching `defer()`.

## Out of Scope

- `@watch("/path")` — separate, parked in `reactive-primitives`.
- `@webhook("/path")` — separate, parked in `reactive-primitives`.
- Gate queue / pluggable resolution — separate, parked in `reactive-primitives`.
- Drift correction (catch-up after slow ticks). Naive `await sleep(seconds)`
  after each run is fine; if precise scheduling is needed later, swap to
  `loop.call_later`.
- Per-tick timeout. If a tick hangs, it hangs — the user can `cancel()`.
- Configurable first-tick delay. If someone needs it, they sleep at the
  top of their function.
