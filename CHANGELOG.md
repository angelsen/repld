# Changelog

All notable changes to this project will be documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

### Changed

### Fixed

- Kernel boot no longer crashes on a stale or old-format `.pyrepl.dashboard` hint file. Older kernels wrote a bare port int there; the current code expects a JSON object and called `.get()` on it, raising `AttributeError` during startup. The output redirect swallowed the traceback, so the kernel exited 1 with no message. Non-dict hints are now ignored
- `browser.open()` no longer races the tab attach. It now uses the session returned by `attach()` directly (the same way `get()` does), instead of a sync re-lookup that could raise `No attached tab` for a target that was just created
- A malformed `__repld_tools__` in a gist can no longer take down MCP `initialize`. A non-list declaration, or list entries that aren't dicts, are skipped with a warning instead of raising in `tools/list`

### Removed

## [0.0.22] - 2026-06-18

### Added

- Dashboard: browser control panel served on ephemeral HTTP port from the kernel. Tabbed UI (Browser/Targets/Console/Network) for managing Chrome connections, watch patterns, and viewing console/network snapshots — without going through exec or MCP tools. Actions push channel messages so the agent sees state changes
- BrowserPool: multi-browser support — connect to multiple Chrome instances simultaneously. Target IDs route by port prefix (e.g. `42829:abc123`). `watch`/`get`/`tabs`/`pages` fan out across all connected instances. Auto-connects to default port on first use
- Dashboard port reuse across kernel restarts via `.pyrepl.dashboard` hint file
- Hash routing in dashboard (`#browser`, `#targets`, `#console`, `#network`)
- Browser GUIDE: clarified auto-wait / settle flow — MCP mutations settle before returning, so the next call's 2s auto-wait is a safety net


## [0.0.21] - 2026-06-18

### Changed

- Screenshot now resized client-side with a pure-stdlib nearest-neighbor PNG scaler (`struct` + `zlib`, ~40 lines). Captures full-res from CDP (no `clip.scale` race), resizes in Python to the vision API token grid (max 1440px/1716 tokens). No external deps


## [0.0.20] - 2026-06-17

### Changed

- Screenshot switched from JPEG back to PNG — the API tokenizes on pixel count, not file size; PNG is lossless (no artifacts blurring text in screenshots)


## [0.0.19] - 2026-06-17

### Fixed

- Screenshot blank white captures — CDP's `clip.scale` races the compositor (`SetSize` fires before a new frame is composited). Removed client-side downscaling; captures full-res JPEG and lets the API resize server-side. Model dimensions still reported for coordinate mapping


## [0.0.18] - 2026-06-17

### Changed

- Screenshot resize ceiling lowered from 1568px/1568 tokens to 1440px/1716 tokens — targets the recommended 1440x900 computer-use resolution, halving token cost for typical viewports while keeping text readable


## [0.0.17] - 2026-06-17

### Changed

- `tab.screenshot()` now captures JPEG (quality 80) pre-sized for the Anthropic vision API (max 1568px per side) via CDP `clip.scale` — no Python image library needed. Returns `{path, source, target, scale, bytes}` with coordinate mapping info. Algorithm ported from Anthropic's `resize.rs` via the nanokvm client. Previously captured raw PNG which could be rejected by the API at larger viewport sizes


## [0.0.16] - 2026-06-17

### Fixed

- Kernel banner and site docs showed `claude --channels` which errors — corrected to `claude --dangerously-load-development-channels server:repld` (custom MCP servers require the development flag with `server:` prefix)


## [0.0.15] - 2026-06-17

### Added

- `.env` loading at kernel boot — reads `KEY=VALUE` pairs from project root `.env` into `os.environ` (stdlib only, no new deps, does not override existing vars)
- `repld://docs/production` MCP resource — graduation guide: two-layer gist pattern, three tiers (standalone / browser-backed / hybrid), FastMCP + FastAPI wiring examples, `.env` secrets story
- Gist template (`repld gist new`) now scaffolds the two-layer portable pattern: core logic at top, repld wiring at bottom
- Production resource pointer in `_REFERENCE` instructions (agent sees it when relevant)

### Changed

- GUIDE: new "Writing portable gists" subsection with `fetch=` callable pattern and `.env` guidance
- PLAYBOOK: phase 4 now points to `repld://docs/production` for concrete wiring patterns


## [0.0.14] - 2026-06-17

### Added

- `repld://docs/playbook` MCP resource — workflow methodology (interactive → gist → trigger → production) readable by the agent before designing automation
- Playbook one-liner in INSTRUCTIONS (always loaded): sets the agent's default instinct to prototype first, extract later

### Fixed

- Kernel banner: stale `--dangerously-load-development-channels server:repld` → `--channels`

## [0.0.13] - 2026-06-17

### Added

- `Browser.from_profile(path)` — connect to Chrome by user-data-dir instead of requiring a port number
- `tab.lifecycle()` — query `Page.lifecycleEvent` entries (DOMContentLoaded, load, networkIdle, etc.) via DuckDB
- `tab.label` — human-readable tab identifier (title truncated, with target ID)
- SSE (Server-Sent Events) capture — event stream messages stored in DuckDB, queryable alongside network/console rows
- `repld://docs/browser` MCP resource — full browser API reference, internals, and workflow patterns; agent reads on demand before writing browser code
- `"."` gist self-dep — `__repld_deps__ = ["."]` installs the gist's own project as editable into the tool venv (target-based install for tool mode)

### Changed

- `browser_js` / `tab.js()` now evaluates with REPL semantics (`replMode` + `awaitPromise`): top-level `await` works in multi-statement code, promise results resolve to their value instead of `{}`, `let`/`const` can be redeclared across calls. Code is never re-evaluated — the old auto-detect path re-ran side effects on every promise retry
- `browser_navigate` on an iframe target (without `force`) returns a proper MCP error instead of `{"error": ...}` JSON
- Fetch interception is now lazy (enabled per-tab on first body access) and fire-and-forget for domain enablement — faster attach, no blocking on slow tabs
- Capture overhaul: body capture is selective (API calls only, not assets), CDP commands use `send_nowait` for non-blocking event acknowledgment
- Dropped response-stage Fetch interception — request-stage only, simpler and avoids double-pause edge cases
- Console row repr: 200-char budget (was 60) with source URL and line number appended
- Gist introspection renders default values, `*args`/`**kwargs`, and bare `*` in AST signatures
- Gist link resolution probes every resolved dir for unregistered siblings

### Fixed

- Settle loop correctness — selective body capture avoids hanging on asset-heavy pages
- `tab.fetch()` body parsing — response bodies now decode correctly
- Corrupt `.links` manifest and gone registry paths fail loudly instead of silently skipping
- Malformed gist declarations (`__repld_tools__` / `__repld_deps__`) error at boot instead of silently hiding the gist
- Redundant `Path` import in `help.py` shadowing the module-level binding
- `_get_tab` helper extracted in protocol.py — missing `target` param now returns a clear MCP error instead of a KeyError

## [0.0.12] - 2026-06-10

### Changed

- `repld gist list` now shows a third section — linkable gists registered in other projects (not already local/linked) — so `gist add <name>` targets are discoverable from the terminal, not just the `repld://gists/_registry` MCP resource

### Fixed

- `repld gist <unknown>` now errors with the usage list instead of silently scaffolding a gist by that name. A typo like `repld gist lis` no longer creates `lis.py`

### Removed

- Verb-less `repld gist <name>` scaffold alias — use `repld gist new <name>`. The alias turned typo'd subcommands into stray gist files

## [0.0.11] - 2026-06-10

### Added

- `repld gist` command group: `new` / `add` / `rm` / `list`. `repld gist add <name>` links a gist registered in another project — without copying — by resolving its path through the registry, following same-dir sibling imports, and recording absolute paths in a committed `./gists/.links` manifest. Local `./gists` and `~/.repld/gists` always shadow a linked gist of the same name; stale links are skipped at boot and pruned with `rm --stale`
- `repld://gists/_registry` MCP resource — browse every gist seen across projects, grouped by project (the agent-facing counterpart to `repld gist list`)
- `tab.wait_for_idle(timeout, quiet)` — wait for network idle, returns settle time in ms; replaces the hardcoded 300ms post-navigation settle
- `repld --version` / `-V`

### Changed

- Top-level CLI dispatch unified into a single `_SUBCOMMANDS` table driving both dispatch and `--help`, so subcommands are now listed in `repld --help` and an unknown command shows the list
- Gist discovery unified through one iterator so linked gists appear consistently in instructions, dependency scanning, and resources
- Docs synced across surfaces (INSTRUCTIONS, topics, GUIDE, CLAUDE.md): cross-project linking, gist conventions (normalize returns, document return shapes, subclassing/introspection caveat), and gate `tab=` routing to the browser pin pill
- Interactive REPL banner lists all injected builtins (`defer`, `every`)
- Dev environment installs the `pretty` + `browser` extras via a dev dependency group so the full tree type-checks

### Fixed

- Possibly-unbound `root_id` in `Tab._find_element` on the CSS-selector path


## [0.0.10] - 2026-06-09

### Added

- Gist dependency management: `__repld_deps__ = ["httpx>=0.27"]` — kernel AST-scans at boot, prompts to install missing packages into the tool venv
- Interactive install prompt (pacman-style): Y/n for single dep, pick-by-number for multiple, Enter defaults to install all
- Venv safety guard: refuses to install into system Python, shows manual install instructions

### Changed

- Brreg gist ported to httpx — native async, no `asyncio.to_thread` wrapper
- Browser dispatch refactored from 172-line if/elif chain to dispatch table with 18 individual handler methods
- Docs aligned across all surfaces: `__repld_deps__`, `ready=` signal, session recovery documented in INSTRUCTIONS, topics, GUIDE, ARCHITECTURE.md, CLAUDE.md, and gist scaffold template
- Gist scaffold template (`repld gist`) includes commented `__repld_deps__` example

### Fixed

- IPC socket path resolution for relative paths (resolves against kernel cwd from lockfile)

## [0.0.9] - 2026-06-05

### Fixed

- MCP server and exec client now report actual package version instead of hardcoded `0.0.1`
- `__version__` sourced from `importlib.metadata` — stays in sync with pyproject.toml

## [0.0.8] - 2026-06-05

### Added

- `ready=` parameter on `browser.get()` — CSS selector or JS expression as app-readiness contract
- Session recovery: on "session not found" (HMR/navigation), re-attach to same target, wait for ready signal, retry
- `navigate()` and `reload()` wait for ready signal before returning
- 300ms settle delay after ready signal resolves (layout/CSS paint)

### Changed

- GUIDE: updated gist template and conventions to use `import repld` instead of `from __main__`

## [0.0.7] - 2026-06-05

### Added

- `tab.tap(selector_or_x, y)` — touch tap via `Input.dispatchTouchEvent` for mobile Chrome
- `tab.swipe(x1, y1, x2, y2)` — touch swipe for scrolling on mobile
- No-focus-steal element resolution: CSS selectors use `DOM.querySelector` + `DOM.getBoxModel` instead of `Runtime.evaluate`
- `Browser(port=N)` works without a loop arg — standalone instances for ADB-forwarded ports

### Changed

- Documented touch vs mouse, no-focus-steal selectors, `Browser(port=)` in help topics and guide

### Fixed

- `TimeoutError` (inherits `OSError`) no longer triggers CDP reconnect — was causing double hangs
- `CancelledError` in `_execute_once` now cleans up pending futures instead of leaking them
- Touch events use 3s timeout via `_touch()` helper — prevents indefinite hangs on complex apps
- Removed vestigial `loop` parameter from `Browser` and `LazyBrowser`

## [0.0.6] - 2026-05-11

### Added

- Central gist registry (`~/.config/repld/gist-registry.json`) — tracks every gist import across all projects with path, description, project, and last-used timestamp
- Migrated all gists from lazy `from __main__` imports to `import repld`

## [0.0.5] - 2026-05-11

### Added

- Kernel primitives (`notify`, `defer`, `every`, `ask`, `confirm`, `choose`, `browser`) importable via `import repld` — gists no longer need lazy `from __main__` imports
- Type stubs in `__init__.py` for IDE/pyright visibility of kernel primitives
- Gmail gist: `headers=False` for fast snippet-only search
- Gmail gist: OAuth2 with auto-refresh, full CRUD
- Google Messages gist: ADB-first with SMS/MMS/RCS dump to SQLite, web opt-in for writes

### Changed

- Gist docstrings document return shapes

### Fixed

- Browser smoketest skips gracefully when `websockets` extra is not installed
- Trusted Types safe pill injection for Google domains with CSP

## [0.0.4] - 2026-05-03

### Added

- Gist introspection shows `async` prefix on async methods
- NameError hints: suggests `__repld_usage__` when a gist variable name is undefined
- MCP instructions include dependency guidance (uv project vs locked environment)

### Changed

- `tab.screenshot()` saves PNG to spill dir and returns path instead of raw bytes
- `browser_screenshot` MCP tool returns file path instead of base64

### Fixed

- Unawaited coroutine warnings now appear in the cell that caused them, not later cells


## [0.0.3] - 2026-05-01

### Changed

- Expanded GUIDE resource: exec patterns, project context, live introspection with `--init`, `tab.fetch()` return shape, API discovery workflow
- Removed unnecessary `# noqa: S307` from runtime.py

### Fixed

- Aligned trailing comments across source files


## [0.0.2] - 2026-04-30

### Added

- `--socket` flag and `REPLD_SOCKET` env var for custom socket/lock paths (kernel, bridge, exec)
- `repld init` auto-detects uv projects (`uv.lock`) and writes `uv run repld bridge` in `.mcp.json`
- GUIDE MCP resource (`repld://docs/guide`) — working guide for gist patterns and conventions
- Meny.no grocery gist with nutrition/pricing/allergen parsing

### Fixed

- Instagram gist: URL-encode query parameters with `urllib.parse.urlencode`

## [0.0.1] - 2026-04-28

Initial release.

### Added

- Persistent Python kernel with top-level await and shared `__main__` namespace
- MCP stdio bridge with channel push notifications
- Core tools: `exec`, `get_task`, `cancel`
- Human gates: `ask`, `confirm`, `choose`, `notify`
- Background primitives: `defer(coro)`, `@every(seconds)`
- Browser integration (`repld-tool[browser]`): CDP attach, network capture, JS eval, trusted input
- Gist system: auto-reload modules, MCP tool registration, resource templates
- CLI: `repld`, `repld bridge`, `repld exec`, `repld init`, `repld help`, `repld gist`
