# Implementation Tasks: `@every(seconds)` + watchdog escalation

**Status:** Complete
**Spec:** [requirements.md](./requirements.md) | [design.md](./design.md)

## Task Breakdown

### Task 1: `@every` decorator + handle registry
**Completed**

**Acceptance:**
- [x] `@every(5) def check(): return "ok"` runs immediately, then every 5s
- [x] Each non-None return pushes to channel with `kind=every`, `label=check`
- [x] Exception in tick pushes with `error=1`, loop continues
- [x] Supports sync and async decorated functions
- [x] `check.cancel()` stops the ticker
- [x] `every.list()` returns active handles
- [x] `every.cancel_all()` stops all

**Notes:** Implemented `EveryHandle` dataclass, `_every_registry`, `_start_ticker` coroutine, and `_make_every` factory in `kernel.py`. Injected `every` into `__main__` alongside `defer`. atexit cleanup uses direct registry drain (avoids attribute access on untyped function object for basedpyright).

---

### Task 2: Watchdog escalation
**Completed**

**Acceptance:**
- [x] Loop blocked >5s: existing warn behavior preserved
- [x] Loop blocked >30s: longest-running non-internal task cancelled
- [x] Kill pushes channel notification with `kind=loop_kill`
- [x] Internal tasks (names starting with `repld-`) are never killed
- [x] `REPLD_LOOP_KILL_THRESHOLD` env overrides default 30s
- [x] Existing smoketest phases pass

**Notes:** Added `_pick_victim`, updated `_loop_watchdog` signature to accept `kill_threshold`, replaced passive 300s wait with active kill escalation. Banner updated to show kill threshold.

---

### Task 3: Help + instructions + smoketest
**Completed**

**Acceptance:**
- [x] `repld help exec` shows `every` in the reference
- [x] MCP instructions mention `every` alongside `defer`/`notify`
- [x] Smoketest phase 10 verifies: immediate first tick, channel push with `kind=every`, cancel stops ticking, error doesn't kill loop
- [x] `--phase 10` passes

---

### Task 4: Update reactive-primitives spec
**Completed**

**Acceptance:**
- [x] Old spec reflects that `@every` has been extracted

---

## Task Dependencies

```
Task 1 (@every impl)  ──→  Task 3 (help + tests)
Task 2 (watchdog)
Task 4 (old spec cleanup)
```

Tasks 1, 2, and 4 are independent — can run in parallel.
Task 3 depends on Task 1.
