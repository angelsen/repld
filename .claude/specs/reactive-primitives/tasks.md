# Implementation Tasks: Pluggable Gate Resolution + Reactive Primitives

**Status:** Not Started
**Spec:** [requirements.md](./requirements.md) | [design.md](./design.md)

## Task Breakdown

### Task 1: Gate queue with pluggable resolution

**Description:** Replace the singleton `_awaiting_gate` model with a queue. Add `_gate_meta` dict and `pending_gates()` to `gates.py`. Refactor `display.py` stdin reader to query `pending_gates()` instead of checking a global. Inject `pending_gates` and `resolve_gate` into `__main__`.

**Files:**
- `src/repld/gates.py` — add `_gate_meta` dict, populate in `_gate()`, add `pending_gates()`, clean up meta in `finally`
- `src/repld/display.py` — delete `_awaiting_gate`/`_awaiting_gate_kind` globals, rewrite `_stdin_reader_loop` to drain `pending_gates()`, rewrite `_render_prompt_open` to not set globals, auto-render next gate after resolution
- `src/repld/kernel.py` — inject `pending_gates` and `resolve_gate` into `__main__` (lines ~450-458)

**Acceptance:**
- [ ] Multiple gates can be pending simultaneously
- [ ] `pending_gates()` returns list of unresolved gates with id/kind/prompt/options/created_at
- [ ] stdin reader resolves oldest pending gate, then shows next
- [ ] `resolve_gate()` first-caller-wins behavior preserved
- [ ] Existing smoketest phases 2-9 still pass

**Dependencies:** None
**Complexity:** Medium

---

### Task 2: `@every` decorator

**Status:** EVOLVED — See: `.claude/specs/every-decorator/` (implemented in `kernel.py`, no separate reactive.py)

**Description:** Create `src/repld/reactive.py` with `Handle` dataclass, registry, `init(loop)`, and the `every()` decorator. Inject into `__main__` from `kernel.py`.

**Files:**
- `src/repld/reactive.py` — new file: `Handle`, `_registry`, `_loop_ref`, `init()`, `every()`
- `src/repld/kernel.py` — import reactive, call `reactive.init(loop)`, inject `every` into `__main__`

**Acceptance:**
- [x] `@every(5)` runs function every 5 seconds on shared loop
- [x] Each tick pushes to channel with `kind=every`
- [x] Supports sync and async decorated functions
- [x] `fn._handle.cancel()` stops the loop
- [x] Errors in the function push to channel with `error=1`, don't kill the loop

**Dependencies:** None
**Complexity:** Low

---

### Task 3: `@watch` decorator

**Description:** Add `watch()` to `reactive.py`. Poll-based: `_snapshot()` via `os.scandir`, `_diff()` returns created/modified/deleted. Supports files and directories.

**Files:**
- `src/repld/reactive.py` — add `watch()`, `_snapshot()`, `_diff()`
- `src/repld/kernel.py` — inject `watch` into `__main__`

**Acceptance:**
- [ ] `@watch("./data")` fires on file create/modify/delete in directory
- [ ] `@watch("./config.json")` fires on single file change
- [ ] Handler receives list of `{"path": str, "event": "created"|"modified"|"deleted"}`
- [ ] Pushes to channel with `kind=watch`
- [ ] Poll interval configurable (default 1s)
- [ ] Cancellable via handle

**Dependencies:** Task 2 (shares Handle/registry/init infrastructure)
**Complexity:** Medium

---

### Task 4: `@webhook` decorator + stdlib HTTP server

**Description:** Add `webhook()` to `reactive.py`. Lazy `asyncio.start_server` on first registration, minimal HTTP/1.1 parsing, localhost-only ephemeral port. Tears down when last route cancelled.

**Files:**
- `src/repld/reactive.py` — add `webhook()`, `_Route`, `_routes`, `_ensure_server()`, `_handle_connection()`, HTTP parsing
- `src/repld/kernel.py` — inject `webhook` into `__main__`

**Acceptance:**
- [ ] `@webhook("/hook")` registers a POST route
- [ ] Server starts lazily on first registration, announces port via channel push
- [ ] Incoming POST body + headers passed to handler
- [ ] Handler return value pushed to channel with `kind=webhook`
- [ ] 404 for unknown routes, 500 for handler errors (also pushed to channel)
- [ ] Server tears down when last route cancelled
- [ ] Localhost-only binding

**Dependencies:** Task 2 (shares Handle/registry/init infrastructure)
**Complexity:** Medium

---

### Task 5: Help docs + smoketest coverage

**Description:** Update `help.py` topics/instructions for gates and reactive primitives. Add smoketest phase 10 covering gate queue, `@every`, `@watch`, `@webhook`.

**Files:**
- `src/repld/help.py` — update `build_instructions()` to mention reactive builtins, add `reactive` topic, update `gates` topic with `pending_gates()`/`resolve_gate()`
- `tests/phases/reactive.py` — new phase module
- `tests/smoketest.py` — register phase 10

**Acceptance:**
- [ ] `repld help reactive` shows @every/@watch/@webhook docs
- [ ] `repld help gates` shows pending_gates()/resolve_gate() docs
- [ ] MCP instructions include reactive primitives when available
- [ ] Smoketest phase 10 exercises gate queue, every tick, watch trigger, webhook POST
- [ ] `--phase 10` passes

**Dependencies:** Tasks 1-4
**Complexity:** Medium

---

## Task Dependencies

```
Task 1 (gate queue)  ──────────────────────────┐
Task 2 (@every)  ──┬── Task 3 (@watch)         ├── Task 5 (docs + tests)
                   └── Task 4 (@webhook)       ┘
```

Tasks 1 and 2 can run in parallel. Tasks 3 and 4 depend on 2 (shared infrastructure). Task 5 depends on all.

## Parallel Tracks

**Track A:** Task 1 (gate queue)
**Track B:** Task 2 (@every) → Task 3 (@watch) + Task 4 (@webhook) in parallel
**Join:** Task 5 (docs + tests)
