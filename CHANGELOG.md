# Changelog

All notable changes to this project will be documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Security

- Dashboard `POST /api` sent `Access-Control-Allow-Origin: *`, letting any webpage open in any local browser drive `browser.connect`/`watch` or read captured network/console data (auth headers, cookies) via CSRF/DNS-rebinding. Now requires a random per-boot bearer token (embedded in the served dashboard page) and echoes CORS headers only for the dashboard's own origin
- The dashboard bearer token was still readable via DNS rebinding: a page on an attacker domain resolving to 127.0.0.1 is same-origin in the browser's eyes, so it could fetch `GET /` (which embeds the token) and then drive `POST /api`. All dashboard requests now require a loopback `Host` header (`127.0.0.1:<port>` / `localhost:<port>`) and 403 otherwise

### Changed

- `Browser.from_profile(path)` replaced by `browser.connect(profile=path)` — the classmethod returned a bare `Browser` outside the pool, so MCP tools and the dashboard never saw its tabs; the pool form resolves the port from `DevToolsActivePort` and connects normally
- `gists.py` split: dependency management (`scan_deps`/`install_deps`) now lives in `gist_deps.py`, cross-project links (`add_link`/`remove_link`/`link_targets`/manifest handling) in `gist_links.py` — import machinery, AST introspection, tool scanning, and the registry stay in `gists.py`
- `ask()` now accepts `tab=` like `confirm()`/`choose()` — routed for symmetry, though the pill UI has no text input so the response is still typed in the terminal (previously passing `tab=` raised TypeError)
- `tab.label` (colored identification bar, survives navigation) is now documented in `repld help browser` and the browser guide
- Internal cleanups from a codebase health pass: `run_kernel` boot sequence split into phase helpers, deduped ready-signal polling and `__repld_usage__` extraction, removed unused parameters and redundant aliases
- More health-pass cleanups: renamed `Dispatcher`'s doc-resource map to stop shadowing the module-level `_DOC_RESOURCES` list, `introspect()` reuses the memoized gist AST parse, first-docstring-line extraction unified in one helper (gist tool descriptions now strip whitespace), `tasks.snapshot()` returns `None` for unknown ids instead of a keyless sentinel dict, `browser.watch()` reuses `_glob_target_id`, `__controls__` prefix shared from one constant, `ask()` typing stub gained the `tab=` parameter it already accepted at runtime
- Third health-pass round: `browser_key`'s tool description now matches its click/type siblings (it runs the same observation pipeline), the browser-availability probe and request-size formatting each live in one helper, and GUIDE's builtins recap is documented as the sanctioned exception to the no-overlap doc-surface rule
- Fourth health-pass round: `kernel.py` reached into `tasks._read_from`/`tasks._make_preview` (both underscore-private) to build the task-done channel push — `tasks.py` now exposes `preview_since(task, offset)` for this; `gists.py`'s `_register()`/`registry()` had duplicated JSON-read-with-fallback logic, extracted into `_read_registry()`

### Fixed

- Cross-process JSON files (lockfile, dashboard hint, session registry entries, gist registry, link manifest) were written with plain `write_text`, so a crash mid-write could leave a truncated file for concurrent readers (the bridge and `repld exec` read the lockfile/hint on every connect). All five now go through one tmp-then-`os.replace` helper; the dashboard hint's `0600` mode also lands before the file becomes visible instead of just after
- Dev-checkout kernels prepended the tool-venv gist-deps dir (`~/.local/share/repld/deps`) to `sys.path`, shadowing the project venv's site-packages with extension modules built for a different interpreter — a stale Pillow there silently disabled the `browser` builtin at boot. The dir is now only added when actually running from the uv tool venv, matching `install_deps`'s gating
- `browser.connect()` with no port ignored `REPLD_CHROME_PORT` and always defaulted to 9222; it now honors the env var like the rest of the browser layer
- The per-tab DuckDB event store was read (`tab.network()`/`console()`/`body()` MCP tools) from IPC reader threads while the kernel loop wrote events through the same connection object — DuckDB connections aren't thread-safe. Reads and `clear()` now run on a per-call cursor (duplicated connection, DuckDB's sanctioned cross-thread pattern)
- `get_task` with an unknown `task_id` returned a *successful* response whose `_meta` carried only `{task_id, error}` — no `done`/`text` fields — instead of a JSON-RPC error like `cancel` does for bad input
- `repld help browser` listed `tab.controls()` and `tab.invoke()` under the sync DuckDB-query heading; both are async (calling them bare returns a coroutine). The browser guide's `controls()` entry also didn't say it's async
- `tab.capture_bodies` / `tab.label` setters fell back to the deprecated `asyncio.get_event_loop()` when the session had no loop — now raise a clear `RuntimeError` instead (the fallback was unreachable in practice; the loop is always set at attach)
- `__repld_deps__` requirements with extras (e.g. `httpx[http2]>=0.27`) were falsely flagged as missing on every boot — the parsed package name kept the `[...]` suffix, which `importlib.util.find_spec` can never resolve
- Cell execution counter (`_N`/`_` history) could race and hand out duplicate numbers when two MCP sessions called `exec` concurrently, since the increment wasn't atomic across IPC reader threads
- `repld gist add <name>` failed for gists that are only ever invoked as MCP tools (never `import`ed by user code) — they were never written to the cross-project gist registry
- `browser_screenshot` silently returned the untouched, full-size PNG (with no error) for any screenshot that wasn't 8-bit RGB/RGBA — while still reporting the *intended* downscaled dimensions in the response metadata, so callers had no way to tell the image didn't actually match what was reported
- `Tab.sse()` / `Tab.lifecycle()` queried with `LIMIT 500` but no `ORDER BY`, while their backing views (`sse_entries`, `lifecycle_entries`) are defined oldest-first. Once a session passed 500 SSE messages or lifecycle events, these methods silently returned the *oldest* entries instead of the most recent — the opposite of every sibling query method (`network()`, `console()`)
- Browser doc drift: the `repld help browser` topic documented `tab.type_text(..., enter=)` (the real parameter is `press_enter=`) and neither it nor `docs/browser` listed `tab.key()`; the `confirm()`/`choose()` type stubs were missing the `tab=` parameter that routes gates to a pinned tab's pill UI
- `BrowserSession.connect()` fetched `/json/version` with a synchronous `urllib.request.urlopen(timeout=5)` on the kernel's shared asyncio loop — every connect *and* auto-reconnect could stall the whole kernel for up to 5s when Chrome was slow or unreachable. Now runs in a thread via `asyncio.to_thread`
- `tab.body()` / `row.body()` called from `exec` code (which runs *on* the kernel loop) deadlocked for 10s and then errored whenever the body wasn't in the capture store — the sync CDP fallback blocked the very loop it needed to run its coroutine on. Now fails fast with guidance to use `await tab.cdp('Network.getResponseBody', ...)` instead; the thread-side (MCP tool) path is unchanged
- `browser_screenshot` wrote the PNG to disk synchronously on the kernel loop; the write now runs in a thread executor like the resize step
- Tab label state (`tab.label`) lived on the ephemeral `Tab` wrapper, so re-labelling a re-fetched tab orphaned the previous label's injected script and DOM bar instead of replacing it. Label state now lives on `CDPSession`, matching pin state
- Re-attach with a CSS `ready=` selector always timed out: the ready poll held a `DOM.getDocument` nodeId from before the page settled, which goes stale when the document is replaced mid-load and then silently never matches. The poll now evaluates `document.querySelector(...)` via `Runtime.evaluate`
- `_parse_pkg_name` split multi-clause requirements (`foo>=1.0,!=1.2`) at whichever version specifier set iteration found first, sometimes producing a mangled package name and a spurious install prompt. Now splits at the earliest-occurring specifier
- Channel-kinds doc in `repld help exec` was missing `pin_lost` and `browser_disconnect`
- A gist declaring a dep whose dotted parent package is missing (e.g. `__repld_deps__ = ["ruamel.yaml"]`) crashed kernel boot — `importlib.util.find_spec` raises `ModuleNotFoundError` for such names instead of returning `None`; now treated as "not importable"
- `resources/read` truncated every resource to the 4KB exec-output preview, so `repld://docs/*` never returned the actual docs to MCP clients (only a head/tail preview plus a local spill path). Resources now return full text up to 64KB — only the unbounded browser network/console dumps can still spill — and honor each resource's declared mimeType (`repld://browser/controls` was declared `application/json` but always served `text/plain`)
- The dashboard's per-tab Detach button never worked: `BrowserPool.snapshot()` built target IDs without lowercasing while the detach lookup compares lowercased, so every click returned "Target not found"
- HMR/navigation re-attach built a brand-new CDP session, silently discarding the tab's event history, body-capture state, and pin/label state, and leaking the old session's DuckDB connection. Re-attach now preserves the session in place (same mechanism as Chrome-restart reconnect) and re-registers the label script
- `tap`/`swipe` and `browser_tree` bypassed the session-gone recovery wrapper, so touch input and tree reads failed permanently after an HMR reload while clicks recovered
- Every browser mutation tool polled a heavyweight DuckDB HAR view at 20Hz *synchronously on the kernel loop* while waiting for network idle; settle now reads an in-memory in-flight counter maintained on the event path
- `repld gist new -h` / `add -h` / `rm -h` treated `-h` as a gist name (or exited non-zero); they now print usage and exit 0
- Scaffolded gists (`repld gist new`) shipped an unused `import os` that lint flags in the user's project
- Doc drift: `repld help browser` claimed CSS selectors use `DOM.getBoxModel` (the code uses `DOM.getContentQuads`) and understated `ready=` as CSS-only (it also accepts JS expressions)
- Missing `task_id` on `get_task`/`cancel` returned a generic internal error (`-32603 KeyError`) instead of the proper invalid-params error (`-32602`)

### Changed

- `BrowserSession.attach()` accepts the caller's target-info dict and skips a redundant `Target.getTargets` round-trip — `watch()` on N matching tabs no longer issues N full target listings
- Gist AST parsing is memoized on file mtime — a single MCP `initialize` previously re-parsed every gist file four times (docstring scan, signature, usage, tools)
- `repld exec` consumes every `--socket` flag occurrence (first value wins) instead of only stripping the first `--socket PATH` pair
- Gist registry writes are skipped after the first import of a given gist per kernel process (previously a full read-parse-write of the registry JSON on every re-import)
- Importing `repld.display` no longer asserts on `sys.__stdout__`/`sys.__stdin__` at import time, so `--no-display` works in stdio-less environments; display mode checks at startup instead
- `browser_screenshot`'s resize step now uses Pillow instead of a hand-rolled PNG decoder (new `pillow` dependency in the `browser` extra) — handles every PNG variant Chrome can emit and runs in a thread executor instead of blocking the kernel's shared asyncio loop for the ~150-200ms a resize takes on realistic screenshot sizes

### Added

- Type-hint gist tool registration: `_tool_*` functions with type hints are auto-discovered as MCP tools — schema inferred from parameter annotations/defaults and the first docstring line. Replaces the two-piece `__repld_tools__` + `_tool_*(args: dict)` convention with a single typed function declaration
- Session registry: every kernel writes `$XDG_RUNTIME_DIR/repld/sessions/<pid>.json` on boot and removes it on shutdown, so any repld instance (or its dashboard) can enumerate live siblings. `repld.sessions.list_sessions()` prunes dead PIDs lazily
- Graceful browser disconnect: `browser.disconnect(port=)` now unpins tabs (removes pill + beforeunload guard + heartbeat) before closing the WebSocket, and returns a summary string. `browser_detach` MCP tool gains `target` (detach one tab) and `port` (disconnect a whole Chrome instance) params alongside the existing `pattern`
- Dashboard sidebar: left rail listing all live repld sessions (project name, uptime, status dot), with the current session highlighted and siblings linking to their own dashboard. New "Connections" tab shows per-port browser connections, expandable to individual targets, with disconnect/detach buttons
- `no_display(value)` builtin: return a value from a cell without the auto-display hook re-printing it, while still binding `_`/`_N` and surviving direct assignment (`x = await foo()`) for programmatic use — for functions that already print their own output
- `repld browser` subcommand: re-execs `repld` under `uv run` with the `browser` extra (`duckdb`+`websockets`), so browser tools work in any project without adding `repld-tool` as a dependency. Detects and preserves a local editable checkout (via `direct_url.json` distribution metadata) instead of silently swapping to the published package

### Changed

- `repld gist new` template scaffolds the new typed `_tool_*` pattern (no `__repld_tools__`)
- `__repld_tools__` still works as a legacy override for custom schemas, but prints a one-time-per-gist deprecation warning at boot
- Kernel boot no longer silently reconnects Chrome ports and re-watches tab patterns from the previous session's dashboard hint — it now prompts on the terminal (`[Y/n]`, default yes) before restoring. Headless boot (`--no-display`) or non-tty stdin skips the restore entirely rather than blocking on a prompt no one can answer
- Browser tab pin state (`_pinned`, `_pin_reason`, `_pin_origin`, `_heartbeat_task`) now lives on `CDPSession` instead of `Tab` — `Tab` wrappers are recreated on every `get()`/`_iter_tabs()` call, so pin state used to reset whenever a tab was re-fetched. `CDPSession` persists for the life of the attachment, matching the existing pattern for `capture_bodies`
- `Tab.fetch()` / `browser_fetch`: string request bodies now default to `Content-Type: application/x-www-form-urlencoded` (dict bodies unchanged, still `application/json`); caller `headers` override, matched case-insensitively

### Fixed

- Multi-line `str` cell results print verbatim instead of being `repr()`-escaped into an unreadable single line with literal `\n`s
- `repld://gists/{name}` signature listings no longer render `@property`/`@cached_property` methods with call parens (was misleading agents into calling e.g. `.pid()` and getting a `TypeError`)
- `browser_fetch` / `Tab.fetch()` silently sent string bodies with no `Content-Type`, so the browser defaulted to `text/plain` and form-decoding servers saw an empty form — root cause was in `Tab.fetch()`'s header defaulting, not the MCP transport
- `browser_fetch` tool schema: `body` had no declared `type`, so MCP clients could silently flatten a dict argument to a JSON string instead of sending it as an object. Now typed `["object", "string"]`, matching what the handler actually accepts
- `no_display(value)` only unwrapped on a cell's bare last expression — direct assignment (`x = await foo()`) left `x` bound to the internal wrapper object instead of the real value, contradicting its own "still returning it... for programmatic use" contract. `compile_cell()` now also collects top-level simple-assignment targets (`x = ...`, chained `x = y = ...`, annotated `x: T = ...`) and `run_cell()` unwraps `_NoDisplay` off them after they're bound. Doesn't cover tuple/list/starred/attribute/subscript targets or walrus expressions nested in larger expressions
- `browser.watch(pattern)` silently reported "Attached 0 new tab(s)" when a matching target existed but the CDP `Target.attachToTarget` call failed (e.g. another debugger already attached to it) — the exception was swallowed and logged only at `DEBUG` (no logging is configured anywhere in the package, so it was never actually emitted). The returned summary now appends `N attach attempt(s) failed: <target>: <reason>` when any attach errors, instead of looking identical to "nothing matched the pattern"
- `BrowserJSError.stack` was always a copy of `.text` (never an actual stack trace), contradicting the docs' claim of a "preserved stack trace". Now parses CDP `exceptionDetails.stackTrace.callFrames` into a real multi-frame string
- Kernel boot swallowed dashboard-start, Chrome-port-reconnect, and pattern-re-watch failures with a bare `except: pass` — a broken dashboard import or failed session restore was invisible. Now logged to stderr
- `repld exec` had no `--socket` flag despite `repld bridge` supporting one, so pointing `exec` at a kernel on a non-default socket path required setting `REPLD_SOCKET` instead
- Codebase health pass found several unlocked check-then-act races matching bug classes already fixed elsewhere: `_Tee.write`'s lazy spill-file open had no lock, so two threads sharing a `task_id` (a sync cell's `asyncio.to_thread` worker racing the loop thread) could both open the spill file, leaking the first handle and losing its output; `Tab.pin()` checked-then-set `_pinned` across two `await`s, so a concurrent `pin()` call could pass the guard twice and leak a heartbeat task that later reset `_pinned=False` out from under the real one; `CDPSession.enable_fetch()`/`disable_fetch()` had the same check-await-set gap
- `browser.get(<target-id>)` raised `TabNotFoundError` when another `attach()` was already in flight for that exact target, instead of retrying like the glob-based lookup does — extracted the shared poll interval into `_ATTACH_POLL_INTERVAL_S`
- `Browser.detach_target()` let unpin/detach exceptions propagate while `detach()` swallowed them — same operation, inconsistent error handling; both now go through one `_unpin_and_detach()` helper
- `gists.py`'s tool-schema builder silently mapped any unrecognized parameter type (e.g. `bytes`, `Path`, `Optional[str]`) to JSON Schema `"string"` with no warning, unlike every other malformed-input path in the file; `gist_deps.py`'s `__repld_deps__` scanner had the same silent-fallback gap for non-list values

### Removed

## [0.0.24] - 2026-06-24

### Added

- Console error dedup: cross-tab duplicate errors within a 2s window are collapsed into one channel push with a count (`×N tabs`). Reduces noise from extension iframes and noisy pages
- Console error suppress: `browser.suppress(substring)` mutes matching errors entirely. `browser.unsuppress()` removes. `browser.suppressed` lists active patterns. Patterns persist across kernel restarts via `.pyrepl.dashboard` hint file
- Suppress hint: after 3 pushes of the same error within 30s, the channel message appends `browser.suppress("...") to mute` so the agent learns to suppress recurring noise

### Changed

- Test harness `wait_notification` accepts `kind=` filter so console error pushes from Chrome don't race expected notifications in smoketests


## [0.0.23] - 2026-06-19

### Added

- Controls protocol: apps exposing `window.controls` get `browser_controls` (discover) and `browser_invoke` (act) MCP tools. Action observations push as channel messages
- Console errors from watched tabs push as `[console:error]` channel messages automatically
- Browser state persistence: Chrome ports and watch patterns survive kernel restarts via `.pyrepl.dashboard` hint file
- `browser_screenshot` tool description now suggests `Emulation.setDeviceMetricsOverride` at 1440×900 (desktop) or 390×844 (mobile) with `deviceScaleFactor: 1` for crisp text on HiDPI displays

### Changed

- GUIDE and BROWSER_GUIDE doc surfaces deduplicated (multi-tab gists section)

### Fixed

- PNG screenshot resize: `_resize_png` now correctly unfilters scanlines before nearest-neighbor sampling. Chrome's PNG encoder uses Sub/Up/Average/Paeth row filters; the previous code read filtered delta bytes as raw pixel data, producing garbled screenshots on every resize since v0.0.20
- Kernel boot no longer crashes on a stale or old-format `.pyrepl.dashboard` hint file. Older kernels wrote a bare port int there; the current code expects a JSON object and called `.get()` on it, raising `AttributeError` during startup. The output redirect swallowed the traceback, so the kernel exited 1 with no message. Non-dict hints are now ignored
- `browser.open()` no longer races the tab attach. It now uses the session returned by `attach()` directly (the same way `get()` does), instead of a sync re-lookup that could raise `No attached tab` for a target that was just created
- A malformed `__repld_tools__` in a gist can no longer take down MCP `initialize`. A non-list declaration, or list entries that aren't dicts, are skipped with a warning instead of raising in `tools/list`

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
