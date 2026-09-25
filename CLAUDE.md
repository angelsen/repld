# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status

Research preview. The kernel, bridge, MCP protocol (exec / get_task / cancel), human gates, channel infrastructure, and the gist commands are live. When implementing, treat `docs/ARCHITECTURE.md` as the design spec (architecture, status checklist, design principles) and this file for subsystem details and invariants. README.md is user-facing only. Don't drift from the shape described here without discussion.

## Build & run

Python 3.12+, managed with **uv** using the `uv_build` backend (see `pyproject.toml`).

```bash
uv sync                                                                                         # install deps into .venv
uv run repld                                                                                    # runs the `repld:main` entrypoint
uv build                                                                                        # wheel + sdist via uv_build
uv run tests/smoketest.py --phase 12                                                            # end-to-end smoketest
ruff check --fix && ruff format && python3 scripts/align-comments.py --fix-all && basedpyright  # lint / format / align / type-check
make injected                                                                                   # regenerate src/repld/browser/injected_source.py
```

`scripts/align-comments.py --fix-all` runs last on purpose — it re-applies
column alignment to trailing `#` comments and `sig  → type` doc markers that
`ruff format` would otherwise collapse to a flat 2-space gap on every `.py`
file. This means a fresh `ruff format --check` run right after this command
reports drift again on those files — expected, not a bug; alignment is
deliberately the last word. Markdown is excluded from `ruff format`'s scope
entirely (`pyproject.toml`'s `extend-exclude`) for the same reason — ruff
0.16+ formats embedded Python code blocks in `.md` by default, which
collapsed the same alignment in README/docs/site examples.

`injected_source.py` is generated — never hand-edit it. `make injected` rebuilds
it from the pinned microsoft/playwright clone via `scripts/build_injected.py`
(node/esbuild needed at dev time only; the wheel ships the .py). Bumping the
engine means updating the clone, changing `PLAYWRIGHT_COMMIT` in the script,
rebuilding, and re-running the phase-6 browser tests.

No CI configured yet. If you add any, update this file.

## Releasing

Published to PyPI as `repld-tool`. Manual, no CI. The local `uv` is a wrapper
(`~/.local/bin/wrappers/uv`) that automates most of it — raw uv is `@ uv`.

```bash
# 1. Accrue changelog notes under CHANGELOG.md [Unreleased] as you work, and COMMIT them.
#    The bump needs a clean tree and promotes [Unreleased] → [X.Y.Z].
uv version --bump patch   # bumps pyproject + uv.lock, promotes changelog, commits "release repld-tool X.Y.Z", tags vX.Y.Z
rm -f dist/* && uv build  # clean stale artifacts first — publish refuses mixed versions
git push origin master --tags
uv publish                    # prints a review summary + a confirmation token (10-min TTL)
uv publish --confirm <token>  # GPG prompt for the PyPI token (from `pass pypi/uv-publish`), then uploads
```

Gotchas the wrapper enforces: clean working tree before `version --bump`; only
the target version in `dist/` before publish (it blocks on leftovers from a
prior release). Verify with the simple index (fast) — the JSON API lags:
`curl -s https://pypi.org/simple/repld-tool/ | grep X.Y.Z`. CHANGELOG covers
*packaged* changes only — `gists/` is not in the wheel. The docs site is a
separate pipeline nothing else prompts: if `site/src/content/docs/**` or
README changed since `gh-pages`' last deploy, finish the release with
`make deploy`.

## Testing

`tests/smoketest.py` is the entire test suite — no pytest setup. It starts a real kernel + bridge subprocess and drives MCP JSON-RPC over stdio. `--phase N` runs phases 1..N (default 3, current ceiling 18). When you add a feature, extend a phase rather than introducing a separate harness. Each phase lives in its own file under `tests/phases/` (e.g. `core.py`, `channels.py`, `defer.py`), as a list of per-subject functions off one `phase_N_*` entry point sharing one `Bridge`. Use `harness.py`'s `Bridge.exec()`/`content_text()` for exec cells rather than another hand-built `tools/call exec` dict, and `wait_notification(..., where=...)` to narrow past the notification stash whenever more than one ticker/gate/task in the phase could push the same `kind=` — an unfiltered wait can silently return an earlier scenario's push instead of the one under test.

Phases:
- **2:** Pure logic called directly, no kernel (`tests/phases/pure.py`) — gate coercion, `_split_answer`, `_make_preview`, `render.py`, `_format_args`. New pure functions get their cases here, not an end-to-end route.
- **3:** Core MCP plumbing — initialize (version negotiation), ping, tools/list, sync exec, deferred exec, get_task polling (incl. `structuredContent`/`outputSchema` conformance), plus three guards on input nobody writes on purpose: bridge/gist argv validation, a malformed gist-registry JSON root, an AST sweep for monkeypatched names nobody resolves through.
- **4:** Channel notifications — task_done push, notify() from user code, pre-gate queuing.
- **5:** Single-kernel flock mutex, `repld_init.py` bootstrap (hand-started kernel, `repld restart`, both lazy-spawn waiters), runtime-state permissions, spill eviction, boot sweep reclaiming a dead kernel's whole project dir, `gists.add_search_dir()` (bootstrap-wired third gist tier).
- **6:** Tool registration, gist auto-reload, browser integration (Chrome 140+; `smoketest.py` spawns a throwaway headless instance via `harness.ThrowawayChrome`, `REPLD_CHROME_PORT` falls back to the developer's own debug Chrome on 9222 if none was found), in `tests/phases/browser.py` — PNG/HAR/selector/injected-engine/Chrome-backed coverage; full case-by-case rationale in `CLAUDE-ARCHIVE.md`.
- **7:** `defer()` — fire-and-forget with channel push on completion; the coroutine's return value recoverable both via the push and `get_task`'s `result`; `no_display()` suppresses the print but not the recovery.
- **8:** Gist resources — `resources/list`/`resources/read repld://gists/{name}`; two AST-only doc-drift guards (agent-facing `help.py` API surface, hand-written docs' API usage).
- **9:** Gist-registered MCP tools — `_tool_*` discovery, schema inference, dispatch, auto-reload, error handling, stale `__repld_tools__` reading as inert.
- **10:** `@every(seconds)` decorator — periodic ticker, immediate first tick, `delay=`, error survival, `cancel()`/`cancel_all()`, exec-cell ticker attribution, `every(tab=)` refusing registration with no browser builtin.
- **11:** Graceful shutdown — `_shutdown` drains `@every` + `defer()` `try/finally` blocks within a 2s budget.
- **12:** Cross-project gist links — `add_link` (registry + AST sibling co-link), `./gists/.links` manifest, stale-entry skip/prune, co-link refusal, registry `(already here)` marking, `gist lint`'s `deps` rule recognizing linked siblings, `add_link`'s kernel-only-import warning.
- **13:** Session registry — boot writes `sessions/<pid>.json`, `list_sessions()`, removal on SIGTERM.
- **14:** Dashboard HTTP — header-count bound, `Connection: close`, Host allowlist (DNS-rebinding guard), `GET /` auth + token non-leak, per-port cookie, `POST /api` Bearer-only, per-session sessions-RPC tokens.
- **15:** Headless kernel — discovery spawns nothing, lazy spawn + heal across kernel death, MCP discovery from `kernel.cache`, targeted vs. broadcast push (incl. `push_delivered`), event log/status/tasks/unknown-method handling, restart refusing reattach, `ipc.rebind_claude_session` (process-tree match, survives respawn), `--project`/`--project-git` (worktree joins the main checkout's kernel, refusals, restart from elsewhere, lookups creating no dir), `repld start` (cold spawn, idempotent), racing boots collapsing to one kernel.
- **16:** Project-venv binding — `./.venv` detection, version-matched `adopt()`, cross-version refusal, `REPLD_BOUND` sentinel, systemd-unit spawn (cgroup/reparenting/racing boot).
- **17:** Human gates on a headless kernel — `awaiting_human` push + answer command, `repld gate --json`, answer/reject/choose/ask semantics, unknown-id error, `ask` on a pinned tab.
- **18:** `repld tasks wait`/`cancel` — blocking wait on an already-done and a still-running task, exit code + exception text on a failed task, unknown-id error, cancel of a running vs. already-done task, `cancel` actually stopping the task, plain listing unaffected.

Full per-case rationale for every phase (why each specific assertion exists, not just what it covers) lives in `CLAUDE-ARCHIVE.md` — read it before extending a phase, not every session.

## Key subsystems

All source lives under `src/repld/`. Individual files are self-describing; what matters is how they connect. Each rule below states the invariant only — what went wrong, the measurements, and the rejected alternatives are in `CLAUDE-ARCHIVE.md` under the same heading. Read that entry before changing code a rule names.

**Threading model:** The kernel runs the asyncio loop on a daemon thread (`run_forever`); the main thread runs the display consumer (or parks on a stop event in `--no-display` mode). IPC accept runs on its own thread, spawning per-connection reader threads that call into the dispatcher. User code in `exec` runs on the loop — a blocking call stalls the kernel.

**Nothing repld itself runs may block the shared loop.** Every cell, ticker and deferred task is on it. The question for any coroutine the kernel runs is not "is this fast" but "is this on the loop"; "how long does it hold it" is the answer that decides.
- The browser observation pipeline runs on the loop per tab *and* per iframe child around every mutation. `pre_observe` reads its cutoff from `MAX(rowid) FROM events`, never the `har_entries`/`console_entries` views (a whole CTE chain). Anything that genuinely needs a view — `post_observe`'s `network_delta`, the dashboard's Console/Network RPCs — goes through `asyncio.to_thread`; `CDPSession.query` opens a per-call cursor so that is safe.
- The deliberate counter-example: `CDPSession._async_prune` deletes *on* the loop — a ~7 ms rowid-range op at most once per `PRUNE_CHECK_INTERVAL` events. Moving it would add a second writer to the event store.
- A wedged loop is reported: a watchdog thread pushes `loop_blocked` past `REPLD_LOOP_BLOCK_THRESHOLD` (5 s), naming the task holding the loop (`asyncio.current_task`) and its stack, routed to that task's session. Past `REPLD_LOOP_KILL_THRESHOLD` (30 s; `0` disables) it cancels that holder and nothing else — never a bystander, never a `repld-` task. One wedge is one `loop_blocked`, closed by `loop_unblocked` with its duration. `ipc.Server` accepts and reads on its own threads, so `repld status`/`log`/`gate` still answer a wedged kernel.

**Request flow:** Claude Code spawns `bridge.py` (stdio MCP). If a kernel for this project is running, the bridge attaches (`ipc.connect_to_kernel`) and forwards everything. If not, MCP discovery (`initialize`, `tools/list`, `resources/list`, static docs) is answered from `kernel.cache` (or a static fallback) and no kernel spawns. The first request needing live state (a real `tools/call`, a gist/browser `resources/read`) spawns a headless kernel via `spawn.py`; from there the bridge proxies JSON-RPC over the unix socket (`ipc.py`) → `protocol.py` dispatches to `exec`, `get_task`, `cancel`, or browser tools → `runtime.py` runs code in `__main__`. Channel notifications (`events.py`) flow kernel → bridge → Claude Code.

**State-file layering:** `paths.py` decides *where* state lives (cwd derivation, `--socket`/`REPLD_SOCKET`). `state.py` decides *how* it's written and trusted (`atomic_write_json`, `open_private`, `read_lock`, `pid_alive`, `acquire_lock`, `sweep_dead_pid_files`) and imports nothing from repld. `ipc.py` is the socket layer only (`Session`, `Server`, `connect_to_kernel`). `spawn.py` holds the one copy of the headless-kernel spawn, shared by `bridge.py` and `repld restart`, so the bridge never imports `lifecycle_cmd`.

- **`spawn.py` decides where the kernel lands.** On systemd: a transient user *service* (`repld-<slug>-<hash>.service`), never `--scope` — `start_new_session` alone neither reparents the process nor leaves the spawner's cgroup. The unit name keys off the *resolved socket path*, not cwd, or two `--socket` overrides sharing a cwd collide. Any systemd failure falls back to plain `Popen`. `REPLD_MEMORY_HIGH`/`REPLD_OOM_SCORE_ADJUST` are opt-in only.
- **`channel.py`** owns `push_channel`/`push_kind` and needs only `ipc` + `events`, so pushers import it at module level rather than function-locally importing `kernel`. **`render.py`** holds the ANSI palette and event formatters shared by `display.py` and `log_cmd.py`, which must render identically.
- **`bg.py` is `spawn(coro)` and nothing else.** asyncio holds running tasks weakly, so every fire-and-forget in the kernel goes through it. `Fetch.requestPaused`'s handler is the sharp case: it's the only thing that resumes a paused request. It deliberately doesn't touch exceptions.
- **The dispatch must be reachable too.** `CDPSession._handle_event` guards the event store separately from the dispatch of `Fetch.requestPaused`/`Runtime.bindingCalled` — a raise from the store must not skip the dispatch.
- **Off-loop callers pass `loop=` to `spawn()`, and that path must never call `loop.create_task`** — from a foreign thread it doesn't wake an idle loop. `spawn()` hops via `call_soon_threadsafe` and creates the task there; not `run_coroutine_threadsafe`, which leaves the task unnamed, and `kernel._watch_block` spares only `repld-`-named holders from cancellation.

**Builtins injected into `__main__`:** `notify(content, **meta)`, `defer(coro, label)`, `every(seconds, delay=0)` (decorator), `ask(prompt)`, `confirm(prompt)`, `choose(prompt, options)`, `no_display(value)`, `browser` (lazy descriptor, only with `repld[browser]`). Plus `help` — a `pydoc.Helper` bound to `sys.stdout`, because pydoc's default pager forks `less(1)` on the kernel tty, bypassing the `_Tee` and deadlocking the loop.

**Doc system (`help.py`):** Agent-facing docs are split across non-overlapping surfaces. Keep them in sync:
1. **INSTRUCTIONS** (dynamic) — composed at MCP init by `build_instructions()`: exec model always; browser model only when `browser` exists in `__main__`; gist signatures via AST. Terse; always loaded.
2. **Tool descriptions** — per-tool what + gotchas, in `protocol.py`.
3. **Topics** — API reference for `repld help <topic>`, `_TOPICS` in `help.py`. `_reference()` derives its topic list from `_TOPICS`; never hand-write one. `migration` is the one explanatory topic, and it expires — delete it once 0.1.x is not a version anyone upgrades from.
4. **MCP resources** — `repld://docs/*`, each a constant in `help.py`: `GUIDE`, `BROWSER_GUIDE`, `PLAYBOOK`, `PRODUCTION`. Registered in `protocol.py`'s resource list; read on demand.

**Browser (`browser/`):** layout, consumer-first: `pool.py` (`BrowserPool`, `LazyBrowser`) → `browser.py` (`Browser`) → `session.py` (WebSocket, sessionId multiplexing) → `cdp.py` (`CDPSession`, per-target DuckDB) → `tab.py` (JS/DOM facade, with `tab_query.py`). `target.py` is the leaf everything can import. `__init__.py` is re-exports only. 29 MCP tools are registered by `protocol.py` only when `browser` exists in `__main__`. See `docs/browser.md` for design rationale.

- **Monkeypatching:** `BrowserPool` resolves `Browser` through `pool.py`'s namespace, not the package re-export — patch the name where it is resolved.
- **Selector resolution is engine-only.** `selector.py` translates repld's grammar to Playwright selector strings; `inject.py` resolves them through the vendored `InjectedScript` (`injected_source.py` — generated, never hand-edit), in an isolated world, falling back to main world, then a loud `EngineUnavailable`. There is no legacy fallback. Visibility ranks, never filters: a lone hidden match is the target; several matches with no single visible winner error with a candidate digest. `internal:text`/`internal:label` spellings are load-bearing. A dropped engine handle kills its aria-refs — the error tells the caller to re-snapshot.
- **Fetch interception is enabled on `get()`/`open()` tabs; `watch()` tabs attach lightweight.** Interception registers on `*`; `capture._should_capture_body` decides what is captured, and skips assets by `har_entries`' own `is_asset` derivation so capture and `tab.network()` agree. `fetch_body` falls back to `Network.getResponseBody`, so skipping loses nothing.
- **`since=` on the four query methods is epoch seconds.** `tab_query` converts all three source clocks (`wallTime` seconds, `Runtime.Timestamp` ms, `Network.MonotonicTime`) to that base before comparing — never compare a caller's `since=` against a raw column.
- **Settle counts `CDPSession.inflight_count()`, never `len()`.** Streamed responses leave `_inflight` when their headers land; `_INFLIGHT_MAX_AGE` (60 s) is the backstop; `reattach_session` clears the map.
- **Loop-owned state is a `loopguard.LoopOwned`, and the type carries the rule.** `_sessions`, `_browsers` and `_inflight` are read mostly off-loop (`browser_dispatch` on the IPC thread, sync exec cells in `asyncio.to_thread`), so every iteration of a `LoopOwned` is a snapshot and every mutation off the owning loop is refused — no `list(...)` to remember, no dependence on a walk never awaiting. State a mapping can't wrap (`_event_count`) gets `@loop_only` on its writers (`store_event`, `_reset_counters`). `REPLD_LOOP_GUARD` is `warn` by default and `raise` under the smoketest. New loop-owned state uses one of the two; a sync method that must write it hops with `call_soon_threadsafe` (`clear_events`) or `_run_sync_on_loop`. A snapshot fixes the crash, not the semantics — a browser connecting mid-fan-out misses that sweep, accepted. `BrowserPool.disconnect` removes snapshotted keys individually, never `clear()`.
- **The DuckDB close is loop-owned too, and no snapshot helps it.** `_db_lock` spans cursor-acquisition-through-close on the read side and the close in `CDPSession.cleanup()`, so a `Target.targetDestroyed` can't close the connection under a live cursor.
- **Anything registered per-session — `addBinding`, `addScriptToEvaluateOnNewDocument`, `Fetch.enable` — is replayed in `_reattach_core`, not at its call sites.** Chrome drops all three when the session detaches. `inject.invalidate(cdp)` lives there too: the engine handle's objectId dies with the sessionId.
- **Pin/gate bridge:** `tab.pin(reason)` injects a pill via `Runtime.evaluate` + a `beforeunload` guard; `tab.confirm()`/`tab.choose()` route gates to it; clicks return via `Runtime.bindingCalled` → `resolve_gate()`.
- **`pin.py`'s two injection routes differ on `document.body`.** `_PIN_JS` runs on a live page. `_LABEL_JS` runs at document *start*, where `document.body` is null — anything registered that way defers to `DOMContentLoaded`. `runImmediately: True` masks the bug in manual testing.

**Dashboard (`dashboard.py`, `dashboard_html.py`):** pure-stdlib async HTTP server on an ephemeral port — an inline control panel (GET /) and a JSON-RPC API (POST /api). `dashboard_html.py` is markup only (`PAGE`/`UNAUTHORIZED_PAGE`), a module rather than package data so it ships with no `pyproject.toml` entry. Port, token and browser restore state live in the hint file (`kernel.dashboard`). `save_hint()` **merges**, rewriting the browser keys only when `browser.peek()` returns a pool — the boot-time call has none and must not blank the previous kernel's restorable session.

- **`GET /` is authenticated, because the page carries the token.** It accepts `?token=` or a per-port `repld_token_<port>` cookie. `POST /api` stays **Bearer-only** — a cookie rides along on requests the user didn't initiate. Sibling-dashboard links get their tokens from `_sessions_with_tokens()` in `dashboard.py`, never from `sessions.register`.

**Gist system (`gists.py`, `gist_api.py`, `gist_deps.py`, `gist_links.py`):** an import hook (`_GistFinder` + `_GistImportHook`) wraps `builtins.__import__`, tracks mtimes, and evicts stale modules on re-import. Module docstring first line → MCP instructions (override with `__repld_help__`); constructor signatures come from AST.

- **MCP tools.** `scan_tools()` discovers typed `_tool_*` functions and infers each schema from the signature plus the docstring's first line (`Annotated[T, "..."]` describes a parameter); `resolve_tool(name)` imports the owner. `__repld_tools__` is removed and ignored; only `gist_lint`'s `legacy` rule reports it.
- **Dependencies.** `__repld_deps__` is AST-scanned at boot (`gist_deps.scan_deps()`); `install_deps()` prompts and installs via `uv pip install --target` into a shared, interpreter-versioned dir (`~/.local/share/repld/deps/py3.12`), never the active venv. `ensure_deps_on_path()` *appends*, so the project's packages shadow a gist dep. Imports are recorded in `~/.config/repld/gist-registry.json`.
- **Cross-project links (`gist_links.py`).** `repld gist add <name>` records absolute paths (the gist plus AST-followed siblings) in a committed `./gists/.links` — no copy. `_load_links()` fills the `_linked` overlay, consulted *after* local dirs so local gists shadow; stale entries are skipped, never auto-rewritten.
- **`repld gist fetch` is `new`'s sibling, not `add`'s:** it *copies*, with a `# source:` header, so `rm` can't undo it. It enforces all three collision scopes (local, global, already-linked), does *not* install the fetched file's `__repld_deps__`, and accepts only a gist id or a `_GIST_HOSTS` URL.
- **The modules share helpers through an intentional call-time-attribute import cycle** (`gists._parse`, `gists.registry`, `gist_links._linked`). `gist_api.py` sits *under* the cycle — AST in, text out, no repld imports. `gists` re-exports `_parse`/`_dunder_value`; patch through the namespace that resolves the name (`phase_3_patch_targets` checks).

## Architecture (target shape)

Thirteen CLI subcommands, dispatched from `repld:main` via the single `_SUBCOMMANDS` table in `cli.py`:

- `repld` — long-running kernel for the cwd with the live TUI. Takes `kernel.flock`, writes `kernel.lock` (`{pid, socket_path, cwd, started_at, dashboard_port}`), listens on a unix socket. Losing the flock prints a note and exits 0 — the incumbent is adopted, never raced.
- `repld bridge` — the stdio MCP subprocess Claude Code spawns. Attaches to a running kernel or lazily spawns one, proxies MCP ↔ IPC, relays `notifications/claude/channel`. `--ephemeral` inverts both halves: eager spawn at a private socket outside `PROJECTS_DIR`, killed and its directory reclaimed when stdin closes. It still spawns with `cwd=os.getcwd()`, so `./gists`, `repld_init.py`, `.env` and `.venv` resolve as usual. Mutually exclusive with `--socket`. The bridge exports `REPLD_OWNER_PID` and the kernel's `_watch_owner` stops it once that pid (checked with its `/proc` start time) is gone, because `_teardown_ephemeral` only runs on a clean bridge exit. `_reclaim_ephemeral_dir` is registered first so `atexit` runs it last.
- `repld log` / `status` / `start` / `stop` / `restart` / `dashboard` — observe and control a kernel this terminal never started. `start` is the one explicit eager spawn outside `--ephemeral`: a no-op with a kernel up, for hooks (SessionStart) that run before any bridge could spawn lazily. It spawns through `restart`'s `_spawn_headless`, and binds like it (`BINDING_COMMANDS`). `status --json --counts` extends the per-sibling listing with `tasks_active`/`tickers` too — one dashboard round trip per sibling with a reachable dashboard, off by default; a sibling with no dashboard or no readable token keeps the keys absent, never `0`.
- `repld tasks` — in-flight `defer()` tasks and `@every` tickers; `--json` for detail. A dashboard `POST /api {"method": "tasks"}` round trip, its own method so the page's state poll stays a bare count. `started_at` is null on a task a pre-upgrade kernel already had in flight; `finished_at` (epoch seconds, null while running) is set by `tasks.finalize()` alongside the pre-existing monotonic `done_at` that spill eviction already owns. `repld tasks wait <id>` / `repld tasks cancel <id>` are the CLI face of `get_task`/`cancel`, over the kernel IPC socket instead — `tasks/wait` blocks the connection's own reader thread on the task's `done_event`, `tasks/cancel` calls `ctx.cancel_task`. Exit 0 on success, 1 on exception/no-op/unknown id.
- `repld gate` — list pending gates; `repld gate answer <id> <value>` resolves one. **Its argv has a region no flag parser may enter:** `_split_answer` cuts at `answer <gate_id>` before `resolve_socket_path`, `--json` filtering or `wants_help` run; everything after is the answer verbatim, and the command's own flags go *before* the verb.
- **`--project DIR` / `--project-git` are global options, before the subcommand** (env `REPLD_PROJECT` / `REPLD_PROJECT_GIT`). `cli._apply_project` chdirs there *before* `bind.rebind_exec`, so identity, binding, spawn cwd, gists and `repld_init.py` all follow from cwd with no parameter threaded through. `--project-git` is `git worktree list`'s first entry, the main checkout; a kernel never takes a worktree as its cwd. Leading-only keeps it out of `gate answer`'s verbatim region; the `--socket` conflict is refused in `paths.resolve_socket_path` for the same reason. The env vars are popped once applied — cwd carries them to re-execs and spawned kernels.
- `repld help [TOPIC]` — agent-facing docs, one source of truth with the MCP `instructions` field (`help.build_instructions()`).
- `repld exec [CODE]` — human CLI: REPL over IPC with no args, one-shot with a string. Same kernel and namespace as the agent.
- `repld gist` — `new`, `fetch`, `add`, `rm <name>` / `rm --stale`, `list`, `lint [--local] [name...]` (`gist_lint.py`; suppress with `# gistlint: ignore=<rule>`). Lint's default scope is everything a kernel here would import, private `_` files included. `--local` narrows to `./gists` and resolves names there first, not through `_find_gist`'s global-first precedence. Lint is read-only: `gists.install(..., create=False)`. Unknown verbs error.
- **Argument validation lives once, in `cli_args.py`.** `check_args` refuses unknown flags and surplus positionals; `wants_help` scans *every* argument. A verb whose flags take a value consumes them *before* `check_args`. `check_args` rejects only surplus positionals, so a verb requiring one needs its own `if not argv`. `log_cmd` stays out — `-n N` needs a real parser loop.
- `repld browser [ARGS...]` — re-exec under `uv run` with the `browser` + `http` extras (`duckdb`, `websockets`, `pillow`, `httpx`). All three browser deps are imported eagerly; missing one reads as the extra being absent. Preserves a local editable checkout (`relaunch.py`).

Key invariants to preserve:

- **One process, one asyncio loop**, so a `create_task` from any exec call survives the exec return and can push on completion.
- **A lock taken across a CDP command must have an unlocked core.** `BrowserSession.execute` reconnects and retries on the *same task*, and `_reconnect` → `_reattach_core` re-enters; `asyncio.Lock` is not reentrant and deadlocks silently. `reattach_session` wraps `_reattach_core`, `enable_fetch` wraps `_enable_fetch_core`, and the reconnect path always calls the core.
- **`exec` returns fast or defers, decided once.** Within `timeout` (default 2 s) it returns inline; otherwise `{task_id, done: false}` plus a push on completion. `tasks.mark_nudged` takes the promise of a push under `_tasks_lock` and refuses it if the cell already finished; `_exec` answers with the result when refused; `claim_done_push` is the paired read on the finalize side. Every cell with output spills to `$XDG_RUNTIME_DIR/repld/{pid}-{tid}.out` from byte 1; the response carries a head+tail preview and the path. There is no `read_spill` tool.
- **Stdlib only in core.** Extras (`repld[pretty]`, `repld[http]`, `repld[browser]`) gate anything heavier. The bar for a new extra is kernel coupling, not reuse frequency; anything baking in policy stays a gist.
- **Per-cwd, localhost-only — and localhost is not a uid boundary.** The unix socket is uid-protected by its mode; the dashboard's TCP port is reachable by anything on the box, hence its token. For a new surface, ask which of the two protections it actually has. Never add anything that would make this safe to expose.
- **Runtime state lives under XDG, never in the project.** `paths.py` is the single source of truth, the gist dirs included (`global_gists_dir()`/`local_gists_dir()`/`gist_dirs()`): `$XDG_RUNTIME_DIR/repld/projects/{basename}-{sha256(realpath)[:8]}/`, 0700. Every state file is the socket path with another suffix (`.lock`/`.flock`/`.dashboard`/`.events`/`.cache`), so `--socket` moves the set coherently. `kernel.cache` is written at boot and never cleaned up — it answers the next bridge's discovery. Per-process scratch (spills, screenshots) sits directly in `RUNTIME_DIR`, named `{pid}-…` — keep the prefix.
- **Scratch cleanup has two rules, no third.** The boot sweep reclaims *dead* pids (`state.sweep_dead_pid_files`); a live kernel ages out its own — task spills by registry entry (`tasks._prune_spill_files`, `_EVICT_AGE`), everything else by mtime (`state.sweep_own_stale_files` via `tasks._sweep_orphans`).
- **Nothing under `RUNTIME_DIR` is readable by other users.** The fallback root is `/tmp/repld-{uid}`. `paths.ensure_runtime_dir()` is the only creator of the root and runs before any write beneath it (`kernel._claim_project` calls it first) — `mkdir(parents=True, mode=0o700)` applies the mode to the leaf only. State files are created 0600 up front (`state.open_private`, `atomic_write_json(chmod=0o600)`), never `open()` then chmod.
- **One kernel per project, enforced by flock.** `kernel.flock` is opened `O_CREAT` without `O_TRUNC` and never replaced; `kernel.lock` is rewritten via `os.replace`, which would move the inode out from under a flock — hence two files.
- **The kernel binds to the project's interpreter and is never spliced across versions.** `bind.py` is the single answer. `repld`, `bridge` and `restart` call `bind.rebind_exec()` first; `exec` deliberately doesn't. `REPLD_BOUND` stops re-entry; `is_bound()` checks `sys.path`, not `sys.prefix`. Only a version-matched venv may be `addsitedir`'d (`bind.adopt`); a mismatch is refused and `gists._explain_missing` enriches the `ModuleNotFoundError`. `project_venv()` prefers `./.venv` over `$VIRTUAL_ENV`.
- **Bridge-served tools are a narrow set.** A tool belongs in `bridge_tools.py` *only* if it must work while the kernel is dead or it kills the kernel (`repld_restart`). The kernel advertises `bridge_tools.SCHEMAS`; the bridge intercepts the matching `tools/call` on the *request* side and never rewrites a response. `protocol._tools_call` errors by name if one reaches the kernel.
- **Anything both sides of the socket answer lives in `core_schemas.py`** (pure stdlib data — the bridge can't import `protocol.py`): tool/resource lists, `CAPABILITIES`, `PROTOCOL_VERSION`, `negotiate_version`, the envelope builders. `initialize` has three authors (bridge, kernel, `repld exec`). `negotiate_version` echoes a supported requested version, else our latest; the bridge never reuses the cached kernel answer for it. 2025-06-18 is the deliberate ceiling; 2026-07-28 drops the handshake and is a bridge rearchitecture. `get_task`'s `outputSchema` binds `tasks.snapshot()` — add a field to both. `build_discovery_cache()` excludes `CAPABILITIES` so a warm cache can't hide drift. `static_instructions()` is `build_instructions()` minus what needs a live kernel, from the same constants in the same order.
- **A connection's Claude Code id can change under it.** `/clear` keeps the bridge process, so `ipc.rebind_claude_session` re-keys by process tree (`Session.peer_pid` via `SO_PEERCRED`, `/proc` ancestry) and sends the bridge `BRIDGE_REBIND_METHOD`, which the bridge consumes and never relays. It re-stamps every later `initialize` with the new id.
- **The bridge is a stateful proxy, not a byte-pipe.** It never closes its own stdout, caches the client's `initialize` and replays it onto every fresh kernel, answers orphaned in-flight ids with `-31001`, and probes liveness *before* forwarding — `exec` must never run twice. Diagnostics go to stderr only.
- **Kernel spawn is lazy, and only when there's nothing to attach to.** Order: `_try_attach_existing` (no-spawn connect) → `_try_bridge_intercept` (discovery from `kernel.cache`) → `_ensure_kernel`, reached only by `_NEEDS_KERNEL` methods. Anything else the intercept declines is `-32601`; an unparseable line is dropped. `notifications/initialized` is tracked (`_client_initialized`), never replayed early. `--ephemeral` is the one eager exception inside the bridge; `repld start` is the explicit one outside it.
- **The project bootstrap is a file, not a flag.** `kernel.INIT_FILENAME` (`repld_init.py`) is auto-detected in the cwd and executed into `__main__` at boot. It runs **after** `_start_services` binds the socket (the bridge waits only 5 s for a spawn), and `_run_init_file` must **never raise**, file read included. It pushes `init_loaded` or `init_error`. `./.env` is read once at boot, never reloaded; `load_dotenv` never overrides an existing var.
- **Every lazy-spawn trigger waits on the bootstrap.** `_run_cell` awaits `_init_done` (the bootstrap cell passes `wait_ready=False`); `protocol._tools_call` blocks on `_Context.wait_ready()` for gist/browser tools — on the IPC thread, which is why `exec` must not come through it. Both are bounded by `_INIT_WAIT_SECONDS`, and the flag is set unconditionally.
- **A human gate must stay answerable on a kernel with no pane.** `gates.resolve_gate` has exactly three callers: the TUI's stdin reader, a pinned tab's pill, and `repld gate answer` (kernel-side `gates/list`/`gates/resolve`). They race, first wins. Those two are JSON-RPC methods, never MCP tools — an agent answering its own `confirm()` defeats the primitive. Coercion lives once, in `gates.parse_response`. With no terminal and no pill in play, the `awaiting_human` push carries the literal answer command — and the pill has no text input, so an `ask` on a pinned tab counts as no pill.
- **A gate that ends unanswered is announced.** `_gate`'s `finally` emits `HumanPromptClosed(gate_id, reason)` — surfaces keeping their own mirror (`display._open_gates`) learn only from it. `_Gate.answered` tells the endings apart, and `resolve_gate` claims it *before* `set_result`, releasing on `InvalidStateError`.
- **The pane's stdin has one reader, so `gates.tty_prompt` is boot-only.** Past boot `display._stdin_reader_loop` owns `sys.__stdin__`; `_stdin_owned` makes `tty_prompt` return `None`, and is released when the reader ends. Anything new that reads stdin asks `stdin_owned()` first. The two boot prompts share `gates.yes_by_default`.
- **Targeted push for call-scoped output, broadcast for ambient.** `_maybe_push_done` routes a completion to `task["origin"]`; if the origin disconnected the push is dropped, never broadcast (`events.emit` still fires, so `repld log` sees it). The outcome is recorded as `task["push_delivered"]`, which `get_task` exposes so a later session can find undelivered results. `@every` errors, browser connect/disconnect and bare `notify()` broadcast. Console errors/exceptions and controls observations go to the tab's `CDPSession.last_caller` with `fallback_broadcast=True` — a best guess, not a request, so a miss broadcasts rather than drops.
- **Anything that outlives its cell is ambient and must clear `tasks._current_task`.** `_task_scope` binds it for `_run_cell`/`_run_deferred`, which end; `_start_ticker` calls `tasks.set_current_task(None)`, or a ticker's output is charged to a finished cell's 4 KB budget and dropped.

## Design principles (from README)

- **Substrate, not library.** Expose small composable primitives (`notify`, `defer`, `@every`, `browser.watch`, `browser.get`) and let the LLM write integration code against live pages/APIs/DBs. Resist adding per-service helpers. Before calling a new primitive done, check that its output plugs straight into the others — a ref usable by `click`/`type` directly, not descriptive text the agent has to hand-translate into a selector first.
- **Channel push over polling.** Long jobs, file watchers, webhooks, and timers all surface as `<channel>` injections rather than requiring the agent to poll.
- **Shared `__main__` namespace.** Human and agent operate on the same module dict — don't sandbox the agent into an isolated scope.
