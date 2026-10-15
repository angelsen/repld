# TODO

## Site

- [ ] OG social images — `astro-og-canvas` per-page cards (`OGImageRoute` at
  `src/pages/open-graph/[...path].ts`) + Starlight `routeMiddleware` injection (docs/cards
  lack `og:image`). Terminal-noir bg + repld logo. Meta-tag plumbing already ships with a
  placeholder default in `SEO.astro`.

## Gist deps tooling

- [ ] **The pre-versioning shared deps dir is orphaned, and only the changelog says so.**
  `_deps_dir()` is `~/.local/share/repld/deps/py3.X` now; the flat
  `~/.local/share/repld/deps/` it replaced keeps everything ever installed there and nothing
  reads it. The `[Unreleased]` note does tell users it's unused and deletable by hand, which
  is why this is small — but nothing at runtime mentions it, so the only symptom is a re-
  install prompt for deps that look like they're already there. Options: migrate the old
  `.repld-manifest.txt` forward on the first versioned install, or warn once when the flat
  dir is non-empty. Anything that cleans it must skip `py3.*` — the versioned dirs are
  children of it, not siblings. Cleared on this machine 2026-07-31 (518 MB, 259 entries;
  the two `-e` installs there were `mym-shopify` and `partbridge`, both of which will
  re-prompt on their next boot with a terminal).
- [ ] `repld doctor`-style check for shared-gist-deps binary-ABI mismatches
  (the dir is `gist_deps._deps_dir()` now — `~/.local/share/repld/deps/py3.X`, keyed by
  interpreter version, which is itself the fix for most of this class) — came up
  organically in session 018 debugging a `_cffi_backend` `ModuleNotFoundError` three causes
  deep (tool-venv deps installed against the wrong Python ABI after a missing `--python`
  pin, since fixed in `b6b2758e`). A static scan of installed `.so` files' `cpython-NNN`
  tags against the actually-running interpreter's version would catch this class of bug
  before it manifests as a confusing runtime import error for a compiled extension.

## Browser

### Agent-first input/observation frontier (from the 0.3.x Jira torture tests)

The organizing thesis: Playwright serves a script author iterating until green;
repld's caller is an agent that pays per round-trip and is blind between calls.
Ranked by leverage:

- [x] **AX-tree diff in observations** — shipped in 0.3.3: `pre_observe`
  snapshots the renderer's own AX traversal, `post_observe` leads with a
  `changes:` section (`+`/`-`/`~`, aggregated, capped at 12 lines);
  suppressed across navigation and on `browser_open`. Hover-only UI does
  reach the AX tree (verified live — CSS-revealed menus enter on display
  flip), so no DOM-mutation fallback was needed.
- [x] **Atomic gestures: `browser_drag(from, to)` + `browser_hover(sel)`** —
  shipped in 0.3.3. Drag is press → paced `buttons=1` moves → release-in-
  finally, endpoints selectors or `x,y`, with a deferred mid-gesture
  re-resolve for drop zones that mount on dragstart; hit verification runs
  at source (pre-press) and drop point (pre-release), not per waypoint —
  nothing at the intermediate points changes what the receipt should say.
  Native HTML5 `draggable` DnD is out of scope (separate pipeline —
  `Input.dispatchDragEvent` + `setInterceptDrags`, if it ever comes up).
- [x] **Type-to-filter in `select_option`** — shipped in 0.3.3: when the
  opening click focuses an element declaring `aria-autocomplete`, the option
  text is typed as a filter before resolving; receipt says
  `(typed to filter)`.
- [x] **`Tab.keys([...])` sequence form** — shipped alongside the VK-code fix
  (Backspace/arrows edited nothing without `windowsVirtualKeyCode`) and
  Ctrl/Alt/Shift/Cmd combos in `key()`.

Untested torture-test classics once these land: iframes, file upload,
infinite scroll.

- [x] **No public `Tab.close()`.** Added: `async def close(self) -> None` wraps
  `Target.closeTarget({"targetId": self._chrome_target_id})`. Session cleanup follows from
  the resulting `Target.targetDestroyed` event (already handled in `session.py`), so no
  extra bookkeeping was needed. Documented in `BROWSER_GUIDE`, `_TOPICS["browser"]`, and the
  site's browser reference. New `phase_6_tab_close` asserts against Chrome's own
  `/json/list` rather than repld's session bookkeeping, since `detach()` alone leaves the
  target open and a check against `browser.tabs` couldn't tell the two apart.
- [x] **`selector.py` has no shadow-DOM fallback.** Fixed: `_deep_qsa()` (new, `selector.py`)
  is a shadow-piercing `querySelectorAll` — a named JS function expression that recurses
  through `el.shadowRoot` — swapped in for the plain `document.querySelectorAll` inside
  all four custom-selector builders (`text=`, `role=`, `label=`, `:has-text`); plain CSS
  selectors are untouched, still resolving via `DOM.querySelector`'s CDP fast path.
  `label=`'s `for=` lookup also switched from `document.getElementById` to
  `lbl.getRootNode().getElementById`, so a label/control pair inside the same shadow root
  resolves too. New `phase_6_shadow_dom_selectors` nests the existing light-DOM selector
  fixture two shadow roots deep. Verified live against the actual repro
  (2026-08-10's `chrome://extensions`, `text=Reload` on a `<cr-icon-button aria-label
  ="Reload">` with no visible text) via `mcp__repld__exec` and `browser_click` end to end.
- [x] **`browser.get()`'s attached-session fast path could read a stale `target_info`
  cache.** Fixed: `Tab._refresh_target_info()` (new, `tab.py`) issues one synchronous
  `Target.getTargetInfo` round trip and updates `self._session.target_info` directly,
  called from both `navigate()` and `reload()` right after `_wait_ready()` — the cache
  no longer depends on the async `Target.targetInfoChanged` broadcast (`session.py`'s
  `_handle_browser_event`) ever being dequeued before the next `browser.get()`, which is
  what `_glob_target_id`'s fast path (`browser.py`) reads. `_get_by_id`/`_attach_racing`
  checked and confirmed unaffected: `_find_by_prefix` matches on `targetId`, which is
  immutable across a navigation, never on `url`. Added `phase_6_navigate_target_info_sync`
  (`tests/phases/browser.py`) — a no-Chrome unit test of `_refresh_target_info()` against
  a fake session (including the empty-reply no-op case), plus a live-Chrome check that
  `browser.get(url, timeout=0)` finds a tab by its post-navigate URL with no sleep.
  Caveat: this closes repld's own event-delivery lag, the mechanism the original two live
  repros (2026-08-10, `chrome://extensions` reload) pointed at; if Chrome's own
  browser-process-side `TargetInfo` update itself lags a renderer's `document.readyState`
  on some page class, a single immediate `getTargetInfo` call could still race it the same
  way the event did — not reproducible on demand to confirm either way, and `chrome://`-page
  reloads are a plausible special case worth re-checking if this resurfaces.
- [ ] `Tab._reattach()` auto-remap across a genuine target swap — currently (session 019) a
  destroyed-and-replaced target (cross-origin/site-isolation process swap) surfaces a clear error
  pointing at `browser_tabs` rather than silently recovering. Considered and deferred:
  re-resolving the same `Tab` handle to a freshly-created target matching its watched glob
  pattern, transparently. Bigger semantic shift than it sounds — changes what a `Tab` *means*
  (pinned to one immutable CDP target → "logically the same tab" across swaps), with more places
  to get subtly wrong (pin state, event bindings, DuckDB event history keyed by the old target
  id). Revisit if a real cross-origin swap actually bites in practice, not preemptively.
- [x] **`browser_tabs`/`pool.snapshot()` don't mark or drop unreachable pool entries at list
  time.** Fixed: both now `await b._ensure_connected()` per browser before trusting its
  `_connected` flag — the same probe `3c90ce3` gave `open()`/`get()` for routing, reused here
  for listing. A dead entry's tabs are omitted rather than shown stale; `format_tabs_nested()`
  (`browser_tabs`'s text output) additionally names the port(s) it skipped. Both methods are
  now `async def`, so their two call sites (`browser_dispatch._bh_tabs`, which now goes
  through `_run_async` like every other Chrome-talking handler instead of
  `_run_sync_on_loop`; `dashboard._collect_state`, now `async def` itself) changed with them.
  New `phase_6_dead_pool_listing` test (pure asyncio, no real Chrome, companion to
  `phase_6_dead_pool_failover`).
- [x] **Browser coexistence with a human on the same Chrome.** Per-tab `mode`
  (`agent`/`shared`) shipped: `open()` tabs are `agent`, everything attached is `shared`,
  `take()`/`release()` switch it. Shared tabs get notify-only file choosers and leave a dialog
  to the human when Chrome has a dialog UI (`hasBrowserHandler`); pushes target `last_caller`.
  Downloads were observe-only already (0.11.6). `phase_6_tab_mode` covers the shared picker and
  dialog live on headless Chrome 153, where `hasBrowserHandler` is true. Still unchecked by
  hand on a headed attached Chrome: Save-As UI, a human actually answering a shared dialog,
  and `downloadProgress.filePath` on Linux.
- [x] **Remaining unrouted browser pushes.** Tab crash, outside close and browser reconnect
  now push (`tab_crashed`, `tab_closed`, `browser_reconnected`; `_tab_lifecycle_pushes`, phase 2).
  Dashboard `browser_*` pushes stay broadcast on purpose: the page is a human click over HTTP,
  with no Claude session behind it to route to.
- [x] Live tests for the lifecycle pushes: `phase_6_tab_lifecycle_pushes` (crash via renderer
  SIGKILL, since `Page.crash` is a no-op in headless), `phase_6_reconnect_push`, and
  `phase_6_download_routing` (main frame and iframe).
- [x] **Crash guard covers five commands.** Fixed (cfcbf013): allowlist inverted, live screenshot/tree test, `post_observe` names the crash. Original note: `_RENDERER_BOUND` (`Input.`, `Runtime.evaluate`,
  `Runtime.callFunctionOn`, `Page.navigate`, `Page.reload`) is all `crash_blocks` and the
  `crashed` fail-fast know, so screenshot, `Accessibility.*` and `DOM.*` on a crashed tab
  probably still ride out 30 s (unconfirmed). Invert it: a crashed tab fails everything except a
  `Target.`/`Browser.`/`Fetch.`/`Inspector.` allowlist and the navigate/reload recovery; add a
  live screenshot/tree test on a killed tab. Also have `post_observe` after a non-navigate
  mutation report "the page crashed during this action" rather than a generic error.
- [x] **Confirm the `[dialog:beforeunload]` broadcast-spam fix on a live kernel.** Done on the 0.12.0
  kernel: an armed `onbeforeunload` on an agent tab, navigated away, shows only the observation's
  `dialog:` line; no channel push, nothing in `repld log`.
- [x] **Docs site is owed a `make deploy`.** Done 2026-10-12 (`gh-pages` `65e2f178`), with the peers
  pages and the 0.13.0 README. Hard-blocked via Bash, so the user runs it.
- [x] **Crash/download edge cases, untested.** (a) ~~A tab closed before `download_done`, and the
  256-entry `_downloads` cap.~~ Done: `phase_6_download_tab_closed` (the owner still gets
  `download_done`, targeted, not a broadcast) and `_download_cap` (an evicted entry's done push
  goes out unnamed).
  (b) ~~A crashed out-of-process iframe: does the parent get `tab_crashed`, and does the guard
  wrongly block the parent's calls? Needs a live look.~~ Done (`phase_6_oopif_crash`, SIGKILL on
  the frame's own renderer, found by CPU spin): one `tab_crashed` naming the iframe target, to the
  frame's watcher; the parent's js/screenshot are untouched; `browser_tree`/`post_observe` on the
  parent used to fail with the child's `TabCrashedError` (fixed: the frame composes as a crashed
  line); the hint now carries `force=true`, which `browser_navigate` needs on an iframe target.
  (c) ~~`post_observe`'s "the page crashed during this action" has no live test.~~ Done
  (`phase_6_crash_mid_action`: a click whose handler makes the test server SIGKILL the renderers).
- [x] **Auto-attach inheritance has no live test.** Done (`phase_6_auto_attach_owner`): it found a
  popup attaching at `''` took its opener's owner and never switched when the URL matched a
  watched pattern; `owner_from_opener` now lets the pattern watcher replace it.
- [x] **`set_files` probe against a real hidden tab.** Done (`phase_6_set_files_hidden`): a merely
  backgrounded tab is not throttled and verifies normally; only `Page.setWebLifecycleState`
  `frozen` stalls the probe, which then returns the 'not verified' note at the 5 s bound.
- [x] **Typed call between project kernels.** Shipped as peers (`peers.get(project).call(...)`,
  `peer/*`, `repld gist stub`); `repld://docs/peers` is the spec and phase 19 the test. The `_tool_*`
  route in the original sketch was dropped: it advertised service internals as MCP tools and could
  not carry a caller or cancel a call.
- [ ] **Cross-kernel event subscription (peers follow-up).** A peer cannot be told when something
  happens in another kernel; today poll with `@every` or have the callee `notify()` its own sessions.
  Context: `claude_code_research/docs/features/phone-channel.md` wants a sibling notified without a
  Claude session. `kernel.events` is a per-project NDJSON log, not a bus, and nothing has checked
  whether its records are structured enough to react to. Design a subscribe call on the same
  per-call-connection model before building (a long-lived stream breaks the one-request-per-connection
  assumption `peer/call` leans on).
- [ ] **Peers live check against a real service.** Phase 19 uses throwaway kernels. Run
  `peers.get("voice-api")` from this project against the real `voice-api` kernel once: a real call,
  a mid-call `cancel`, and `repld gist stub voice-api <gist>`.
- [ ] **Native-prompt backlog, ordered.** (1) ~~`set_files` readability probe hangs on a
  `hidden` tab~~ (shipped). (2) `DeviceAccess` + `BluetoothEmulation` as an agent-mode record/push/select feature, only
  if a hardware-web use appears. Skipped: FedCm (flag-gated, rare); USB/serial/HID have no CDP
  surface, so document the Chrome policies (`SerialAllowAllPortsForUrls`,
  `WebUsbAllowDevicesForUrls`) for a throwaway test Chrome instead of building anything.
- [x] **`set_files` reports success on an unreadable file.** Fixed in 0.11.6: reads one byte of
  each file back and raises naming it; confirmed on a Nothing Phone (`media_rw` file raises
  `NotReadableError`).
- [x] **`set_files` readability probe hangs on a `hidden` tab.** The probe now waits
  `_PROBE_TIMEOUT_S` (5 s) and `set_files` returns with a "readability not verified" note instead
  of a 30 s CDP timeout; covered by `_files_probe_timeout` (phase 2).
- [ ] Body capture wedges an endless non-SSE streaming response: `cdp._STREAMING_MIME_TYPES` exempts only `text/event-stream`, so a page `fetch()` of an endless `application/octet-stream` body (the `liveview` gist's frame stream) sits paused at the response stage until `Fetch.getResponseBody` returns, which it never does, and the page's promise never resolves. Measured 2026-10-15 in a `browser.open` tab (capture on): first load went live, a re-navigation to the same page stalled on "Connecting", and `tab.disable_capture()` cleared it; the server had sent every byte. Fredrik's call: widen the exemption (any response without a `Content-Length` that is still open after a short wait), give the `getResponseBody` wait a cap, or leave it to `browser.no_capture(pattern)`, which already works.
- [ ] Unmeasured: a TeamViewer web-client session (`web.teamviewer.com/Connect`, the remote-screen canvas) streamed through the `liveview` gist (`tab.screencast`). Measured 2026-10-15 against an Android QuickSupport device: frames carry the canvas and the viewer renders it. Not measured: added latency over the native web client, whether forwarded mouse and key input (`Input.dispatch*` from the viewer) is mapped correctly onto the remote screen, and whether the web client's clipboard sync fires when `serve()` calls `tab.front()` (the device showed "Text has been copied to your clipboard" right after; unverified cause, so `serve()` may need a no-focus option for tabs with side effects on focus). `gists/teamviewer.py` (gitignored) wraps the session tab as a computer-use `Device` (screenshot, click, move, drag, scroll, type, key) in screenshot-pixel coordinates over `#remoteScreenCanvas`: the read-only mapping is verified (corners land exactly, 2176x1694 canvas shown at 1635x1273); input verified 2026-10-15 on the Android QuickSupport tablet (click focused the chat field, `type("hi")` appeared and enabled send, two Backspaces cleared it; nothing was sent); pixel actions verified 2026-10-15 on the tablet: `scroll` (negative dy moves the list up), `drag` (swipe), the Android Back soft key by `click`, key combos (`tab.key('Ctrl+A')` drops the modifier: the client wants Control's own keydown first, so the gist's `key()` sends modifiers as separate events and focuses `#remoteScreenCanvas` first, since a toolbar dialog leaves focus on body), non-ASCII typing (the client drops non-ASCII key presses, so `type()` routes it through `send_text` + `Ctrl+V`; the paste lands more than 1.5 s later); click-to-visible-change measured at 0.24-0.38 s (keyboard show/hide, screenshot cost included); Home and Recents soft keys (the blue bar at the bottom) verified: Recents opened the app overview, Home the launcher. DOM-driven, no pixels (verified 2026-10-15): `info()` (Session information: display, Q, route latency/bandwidth), `apps()` (27 installed apps), `send_text()` (device clipboard dialog, opened and cancelled only), `send_file()` (the client calls `showOpenFilePicker()`, which `expect_file_chooser` cannot answer, so the gist arms a page-side stand-in; two files showed in `transfers()` with the right sizes, landing folder on the device unchecked); `send_folder()` (the client calls `showDirectoryPicker()`; a stand-in tree handle worked, overview showed "2 files, 8 Bytes" for a folder with a subfolder, landing path on the device unchecked); Device apps does not launch anything (Fredrik confirmed it too): the play button, a click on the name or icon and a double click on the icon all started nothing, and the click goes over the WASM channel with no HTTP or console trace, so `launch()` is removed from the gist; apps start by `click` on the tablet's own launcher icon after Home; Receive is disabled for QuickSupport. A page screenshot includes the web client's own overlays (the version notice) that would also swallow clicks. Fredrik's call whether this path is worth pursuing; the licensing and terms side is not a repld item.
- [ ] TeamViewer web client structure (measured 2026-10-15, public static assets only, parsed not executed): `X-Frame-Options: SAMEORIGIN` + `COOP: same-origin` + `COEP: require-corp` on `/Connect` and `/remote-support`, so no cross-origin iframe; core is a 32 MB (10 MB gzip) minified Emscripten WASM (30 mangled exports, 459 imports from module `a`, no usable API surface) behind a 342 KB glue file and a 3.2 MB UI bundle; the UI reads server-injected `window.wssvars` (loginToken, machineId, sessionCode, serviceQueueToken, ...) and URL params `uiMode`, `TabMode` (SingleTab / MultiTabBackground / MultiTabUI), `connectionMode`, `machineId`, `embedded` (reads as a preload flag, not iframe embedding). Not tried: a direct deep link `/Connect?machineId=...&connectionMode=RemoteControl&TabMode=SingleTab` in the container browser (opens a second connection to the device). Not pursued by design: hosting or running the WASM outside TeamViewer's own origin, or reading the protocol out of it; the supported route is TeamViewer's partner channel (their KB asks to be contacted). Fredrik's call.
- [ ] TeamViewer computer-use session runs in Fredrik's own Chrome on 9222 (`gists/teamviewer.py`, gitignored). Its side effects there: the web client's version-notice banner lands in every page screenshot and would swallow clicks, and `serve()`'s `tab.front()` may fire the client's clipboard sync (line 190). Move it to a dedicated browser with its own profile and TeamViewer account (a container, `browser.connect(port)`) once the client's WebGL works there; not yet attempted this session. Fredrik's call on timing.
- [ ] Browser as a service (Prelo, `bulletins-chat`): `browser.connect()` takes only a local port (`pool.py:85`, keyed by port; `profile=` reads DevToolsActivePort), so a hosted provider's `wss://` CDP URL cannot be attached (shipped: `connect(url=, headers=)`, keys from 65536, never persisted; phase 6 `connect_url`), and a container needs a published port. Measured 2026-10-15: Kernel's headless image (`kernel-probe`, Chrome 152, port 19222) attaches and renders TeamViewer's sign-in page, but `getContext('webgl'/'webgl2')` is false there (even on `about:blank`). Cause found: its running Chrome is launched with `--disable-gpu` and `--disable-software-rasterizer`; a fresh headless Chrome from the same image defaults to `--use-angle=swiftshader-webgl`. Steel (`steel-probe`, ghcr.io/steel-dev/steel-browser, Chrome 155, host 127.0.0.1:13000 API / 19223 CDP) measured 2026-10-15: WebGL and WebGL2 both work, 1920x993 viewport, and TeamViewer's sign-in page loads there; the renderer string reads `SwiftShader` on `about:blank` but `Mesa Intel UHD Graphics 630` on the TeamViewer page, which looks like Steel's fingerprint spoofing and not a real GPU (unverified), so do not read the string as the actual renderer. `/Connect` tried in Steel 2026-10-15 after Fredrik signed in through the liveview page: the client loads (cross-origin isolated, `SharedArrayBuffer`, WASM initialised, canvas present at 300x150) but the connection to the tablet (by its TeamViewer ID) stalls at "Creating a secure connection" with "Not ready. Please check your connection" for over two minutes, no error dialog, no failed requests in `tab.network()`. Undecided between Steel's network and the tablet's QuickSupport being closed (Fredrik is away from it): next, either confirm the tablet is online or connect to the same ID from Fredrik's own Chrome; the remote canvas is still unverified in either image. A blob `em-pthread` worker logged `importScripts` failures on the way (effect unknown). Steel's `/json/version` advertises `ws://127.0.0.1/...` with no port (a proxy rewrites Host), so `browser.connect(19223)` follows it to port 80 and fails; `connect()` should keep the host:port it dialled when the advertised URL has none. Where the Kernel image sets its flags is unknown (the launcher is a compiled binary), so changing them needs a different image or a second Chrome with a published port, so the TeamViewer `/Connect` canvas is unverified in it. Design call (Fredrik): provider lifecycle (start/stop a podman container, return CDP + live-view URL) as a portable gist per the two-layer pattern in `repld://docs/production`, not core; core only gains what a gist cannot do, i.e. attach by URL.
- [ ] `gists/browsers.py` (gitignored; portable provider core, `attach()` is the repld wiring): `start(kind)` / `stop(name)` / `list_sessions()` over podman for `steel` and `kernel` images, one container per session on free 127.0.0.1 ports only. Verified 2026-10-15: start a Steel, `list_sessions`, `attach` through repld (works via the `connect()` port fix), `browser.open(example.com)` with WebGL2 true, `stop` removes it. Steel's API has no authentication (`/v1/sessions` answered with none), so these ports must never be published: the broker below is a precondition for any non-local use. Open, in order: (1) a Prelo-side broker that authenticates, maps a thread/integration to a session and hands out view/control tokens (design only, nothing built); (2) the Prelo declaration for the image, a slipp2 `image:` service like `livekit` in `bulletins-chat/deploy/prelo.yml` (not drafted, no host touched; `slipp2 render` can check it offline); (3) per-session profile persistence (a TeamViewer login lives in the container and is lost on `stop`); (4) idle reaping and a session cap; (5) `attach` by URL for a remote provider (see the entry above). `kernel` kind unretested through this gist.
- [ ] Surface autonomy modes and data-first rendering (Fredrik's framing 2026-10-15, design only): (a) a surface runs in `act` (today's `tv_*`: input goes straight through), `propose` (the agent's action is drawn as a pending marker with its intent, and a human accepts it, drags it elsewhere or rejects it before it executes; the corrected point is worth logging) or `view`, set by whoever holds the seat; the marker drawing in `gists/teamviewer.py` (`_draw_action`) already renders an action and can render a pending one; the removed `confirm()` gate was per session and blocking, this one is per surface and per action; (b) a "mini app" is a native-looking DOM UI over data a gist returns, not pixels: the TeamViewer client's DOM features (`apps()`, `info()`, `transfers()`) already give that, but the Autel screens are canvas-only, so a data-first Autel view needs text from OCR or from the app's own report exports (Report List, PDF), unmeasured. Both belong to the broker/seat design in the entry above.
- [ ] Browser infra architecture proposal (2026-10-15, from `bulletins-chat/INFRASTRUCTURE.md` only, no Prelo code read; Fredrik's call): five layers, each replaceable. L0 repld core: attach, stream, input, settle (exists). L1 provider: start/stop/list returning a private CDP endpoint (`gists/browsers.py`; podman now, a slipp2 `image:` service or a hosted provider later). L2 surface service: a small Python process running `repld-tool[browser,http]` beside Prelo (the browser-backed tier in `repld://docs/production`), owning the CDP socket, the point/DOM adapters (`teamviewer.py` is one), `propose`/`commit` and the settled frame stream. Language is open (Fredrik's call, revised 2026-10-15 after sizing): repld's `browser/` is 10.6k lines of Python, but 6-7k of that is observation (DuckDB event store, HAR, selector engine, observe) a surface service does not need; the part it needs (attach, screencast, `Input.dispatch*`, captureScreenshot, settle, marks) is roughly 1-1.5k lines, and Prelo's `package.json` already has `ws` and `sharp` (no CDP library). Leaning TypeScript for the production L2, with repld as the research bench and the contract being a written surface spec plus golden-frame conformance fixtures, not shared code. L3 broker in SvelteKit: auth, thread and party mapping, view/control tokens, seat arbitration, audit events (`browser:*` custom event types, scopes `browser:view` / `browser:control`), a WebSocket upgrade beside the debug relay's in `server.js`. L4 clients: a thread side panel, MCP tools through Prelo's `/api/mcp`, and gists. Interface rule: the L2/L3 wire is one small JSON-plus-binary-frame protocol, and tools are named for the surface (`surface_click`), with app-specific skins like `tv_*` as adapters. Build order: surface/adapter contract with propose-commit in the gist, provider interface, L2 locally, a view-only broker in one thread, control and seat, audit, slipp2 deploy.
- [ ] Run-locally-and-exposed-from-a-server requirement (Fredrik 2026-10-15; design only): one artifact, two modes, mirroring how Prelo already runs its web app. The surface service binds 127.0.0.1 only and is declared once in `deploy/prelo.yml` with a real process (server mode, `slipp2 deploy`) and a `dev_port` (laptop mode: `slipp2 expose` points a wg-manage hub name at the laptop, imperative and non-durable, per its own help). It accepts only the broker, with a shared secret the broker sends (an exposed laptop port is reachable over WireGuard); Caddy terminates TLS and the broker authenticates people. The browser need not sit beside the service: a provider returns a private CDP endpoint (local container, a slipp2 `image:`, or the laptop over a tunnel). Hazard to design out: a `local-chrome` provider (Fredrik's own Chrome, 9222) carries his logins, so exposing it through a server needs explicit per-session consent and view-only by default. Open: the shared-secret scheme, whether the service and the provider co-locate by default, `dev_port` and the process stanza (not drafted; `slipp2 render` checks offline).
- [ ] Steel vs Kernel (or neither), brainstorm 2026-10-15, nothing decided: lean, unverified and from feature lists not tests: Kernel for the heavy computer-use tier, Steel for light browsing; treat the provider contract as `cdp` (required) plus advertised capabilities (`viewer`, `os_input`, `clipboard`, `fs`, `record`, `proxy`, `egress`) so the surface service runs on either and on a bare Chrome. Read from the Kernel image's API binary, NOT called and not from its docs: `/computer/{click_mouse,drag_mouse,move_mouse,press_key,scroll,type,screenshot,cursor,batch,clipboard/*}` (OS-level input), `/fs/*`, `/process/{exec,spawn}` + pty, `/recording/{start,stop,mark,list,download}`, `/network/egress-policy`, `/events` telemetry stream, `/chromium/{flags,policies,upload-extensions}`, `/playwright/execute`, `/webmcp/*`. Steel (observed): `/v1/sessions` with proxy, `solveCaptcha`, `userAgent`, `dimensions`, `timeout`, a session viewer and debugger URL, console/request/recording events logged server-side; no authentication. Tests that would decide (tests 2 and 6 RUN 2026-10-15, results in the next sentences, the rest not run): TEST 2, 1280x800 viewport forced on both, a 2D canvas animation, 10 s, one `podman stats` sample: both stream `tab.screencast` at 59.8-59.9 fps (p50 gap 17 ms, p95 20-21 ms), a click shows up in a frame in 58 ms (Kernel) and 56 ms (Steel) median, 12/12 seen on each, max 64/70 ms; CPU: the first run's `podman stats --no-stream` figures (9% Kernel, 29% Steel) were WRONG as a comparison; measured properly (user+system time of the container's process tree from `/proc` over the 10 s stream) both use about the same, Kernel 150% and Steel 137-153% of a core, and click latency varies run to run (Steel 73 then 42 ms, Kernel 63 ms), so treat 40-75 ms as the crun noise band. `podman stats` on a gVisor container prints nonsense (25230%). Their default viewports differ (Kernel 780x437, Steel 1920x993) and that alone changed the first run's numbers, so always force one. WebGL not measured. TEST 6, a host volume on each image's profile dir (Kernel `/home/kernel/user-data` with `:U`; Steel `CHROME_USER_DATA_DIR=/data/profile`), cookie plus localStorage on example.com, 40 s wait, graceful stop, restart on the same volume: both keep both values. Traps: a 3 s stop timeout lost the Kernel cookie (a cookie needs time or a clean exit to reach disk; localStorage survived), Kernel took 12 s to stop and Steel used the whole 20 s (killed, not exited); Steel then refuses to reopen the profile ("The profile appears to be in use by another Chrome process ... on another computer", `process_singleton_posix.cc`) because the new container has a new hostname and the old `SingletonLock/Socket/Cookie` is left behind, retrying forever with 502 on its CDP port, so a Steel restart on a volume must delete `Singleton*` first (or fix the hostname, untested). Without a volume nothing persists on either. `gists/browsers.py` `stop` uses `-t 3` and has no volume option: both need fixing before it carries a login. Steel's own `sessionContext`/`userDataDir`/`persist` API options untested. Also: a `POST /v1/sessions` made as a "probe" replaced `steel-probe`'s session and browser, so its TeamViewer sign-in is gone. Idea, untested (brainstorm 2026-10-15): a custom minimal image (Chrome for Testing, the surface service's own CDP input and clipboard handling, volume profile) may be the default for Prelo shared surfaces, with Kernel/Steel as optional capability providers; reasons: the whole core path (CDP, 60 fps stream, 56-58 ms click, persistence) already works without either, both images are large (Kernel 2.19 GB, Steel 3.41 GB here) and Kernel's ships dockerd/containerd/envoy plus an API with `/process/exec` and `/fs/*` whose auth is unchecked. GVISOR TEST 2026-10-15 (`runsc` release-20261005.0, downloaded as `gvisor.tar.zstd` after the 2026-07 layout change, checksum verified, extracted into the scratchpad only, no sudo and no system install): rootless podman runs it with `--runtime <runsc> --runtime-flag=ignore-cgroups --runtime-flag=network=host` (guest kernel reports `4.19.0-gvisor`). The Steel image under it starts (CDP up in about 10 s versus about 5 s), WebGL2 works, 58.8 fps with p95 gap 19 ms, but click-to-frame is 188 ms median (max 205) against 42-73 ms under crun, and CPU is 358% of a core against 137-153%, for a worst-case page (a full-canvas 2D animation at 60 fps; an idle or mostly static UI will cost less, unmeasured). One run, `systrap` platform, host networking so gVisor's own netstack and egress rules are untested, Chrome ran with `--no-sandbox`. Not tested: a minimal rootfs instead of Steel's image, the KVM platform, per-session memory, whether the latency is syscall cost or the proxy path. Remaining, none run: (1) TeamViewer `/Connect` canvas in each (blocked on the tablet being reachable), (2) screencast frame rate and latency under software rendering (Steel was at 59% of a core with one page), (3) whether Kernel's `/chromium/flags` lifts its `--disable-gpu` (that endpoint is a write, so deliberately not poked), (4) each one's auth story, (5) licences and commercial terms for Kernel's image (unchecked; Steel's is Apache-2.0 per the saved provider note), (6) profile persistence across sessions.
- [ ] Isolation and egress for self-built browser sessions (brainstorm 2026-10-15, nothing decided, Fredrik's call): the threat model decides the tier. Facts: both images run Chrome with `--no-sandbox` (seen in their process args), so the container is the only boundary; this laptop has `/dev/kvm`, bwrap, systemd-run, nft, unshare and qemu but no gVisor or Firecracker, and rootless podman uses pasta networking with crun; the Prelo prod VPS's KVM and nested-virtualization availability is unchecked. Options in rising strength: systemd transient unit with hardening (DynamicUser, PrivateTmp, NoNewPrivileges, MemoryMax, per-unit IPAddressDeny) or bwrap; rootless podman per session with Chrome's own sandbox re-enabled (untested whether it works unprivileged); gVisor; a microVM. Whatever the tier, a browser a user can point anywhere is an SSRF into the host's network (Postgres on 127.0.0.1:5432, LiveKit, WireGuard peers), so egress must deny loopback, RFC1918, link-local and the WireGuard range at the network layer, and pasta's default reach to the host is unchecked. gVisor as the path (Fredrik's lean, untested; from general knowledge, not checked here): `runsc` runs an OCI bundle, so a rootfs plus config is still needed but a container ENGINE is optional; its default `systrap` platform needs no KVM, which would make it the strong tier that also works on a VPS without nested virtualization (unchecked for the Prelo host); open questions to measure: Chrome under it (is `--no-sandbox` then acceptable since gVisor is the boundary, does the 60 fps stream and ~57 ms click hold with SwiftShader), rootless use with podman/crun, its netstack versus host networking for egress rules, memory per session, startup time. "PC as a service" (a full desktop or non-browser apps, e.g. a Windows tool) is a separate provider kind behind the same frames-in/input-out contract (VNC/Xpra/RDP) and a VM, not a bigger browser.
- [ ] Synthesis lean for "a browser object via Prelo" with low-level access (2026-10-15, from the measurements above, nothing decided, Fredrik's call): expose CDP itself as the contract, a per-session authenticated `wss://` endpoint behind the broker (any Playwright/Puppeteer/repld client can attach, and `connect()` needs attach-by-URL), with scopes narrowing it (`browser:view` = screencast and read-only domains, `browser:control` = `Input.*` and `Page.navigate`, `browser:raw` = everything, trusted agents only) and a method allowlist enforced in the broker; the higher-level surface protocol (frames, point input, settle, propose/commit, marks) is a layer on top, not a replacement. Session layer (auth, party/thread mapping, seats, audit, egress) is built by us because neither Steel nor Kernel knows parties and Steel's API has no auth; the image is a custom minimal Chrome by default with Steel/Kernel behind the provider contract as capability providers; runtime is a per-session property (`runc` for trusted, `runsc` for untrusted pages, ~115 ms and ~2.3x CPU on the worst-case page), with egress denial independent of the runtime; profiles on a volume per (party, app) with `Singleton*` cleanup and a 20 s+ graceful stop. Raw CDP is the highest-privilege thing a client can hold (cookies of every tab, any URL the box can reach), so scopes, the allowlist and the egress rules are what make it safe to hand out, and none of them is built or reviewed. Still unmeasured: TeamViewer `/Connect` in any of these, a normal (mostly static) UI's cost under gVisor, the Prelo VPS's capabilities.
- [ ] Same VPS as Prelo or a separate browser host (Fredrik's call; lean: separate for prod, same host only for dev or a capped demo; 2026-10-15). Measured on Steel under crun, `tab.screencast` at 1280 wide, 10 s each, CPU from `/proc`: a full-canvas 60 fps animation costs 149% of a core and 818 kB/s at JPEG q60 (678 kB/s at q35); a page updating once a second 3% of a core and 12 kB/s; a static page 0% and about 1 kB/s. The test pages are synthetic (frames are 10-14 kB), so a media-rich real UI will cost more; memory per session was not measured on a custom image (about 1 GB for Steel's container earlier, `podman stats` memory only). Reasons for separate: memory pressure and the OOM killer next to Postgres and LiveKit, CPU bursts hurting call quality, SSRF into services bound to the host's loopback, independent sizing (more RAM, possibly a dedicated server with KVM), and a clean blast radius; the extra hop is cheap at these bandwidths over the existing WireGuard. Design remote-first regardless: the surface service reaches a CDP endpoint over the network and never assumes loopback, so same-host versus separate is a deployment setting (another `hosts.yml` entry and destination in slipp2). Open: whether viewers stream through Prelo or directly from the browser host with a signed URL, the host class, per-session `MemoryMax`.
- [ ] Self-hosted versus a managed browser service (2026-10-15, from the saved provider note plus Cloudflare's CDP docs, `~/.local/share/docduty-search/memory/cloudflare-browser-run-cdp-endpoint-has-connection-guardrails-readonly-mode-and-a-keep-alive-session-limit-of-20-minutes/NOTE.md`; Fredrik's call): managed services do NOT lack low-level access, they all expose a CDP WebSocket URL (Browserbase, Kernel, Steel, Browserless, Anchor, Hyperbrowser, Browser Use, Cloudflare Browser Run, Bright Data), and Cloudflare even ships a `guardrails` `{"mode":"readonly"}` connection, the same idea as our `browser:view` scope; their session lifetimes are bounded (Cloudflare `keep_alive` 10 s to 20 min; Browserbase free 15 min per session) and the `wss://` URL shape is what `browser.connect()` cannot attach to yet (other tools added remote-WebSocket support for the same reason). So a managed service is one more provider kind behind the same contract (`remote-cdp`: URL plus token), not an alternative architecture. Decision rule to test: sensitivity of what is on screen (customers' vehicle data, logins), session length, and how much control the product needs go to self-hosted; bursty, anonymous or hostile-web browsing can go managed. Re-checked against each vendor's own docs 2026-10-15 (notes in `~/.local/share/docduty-search/memory/`: browserbase-sessions-..., kernel-browsers-..., steel-sessions-...): all three hand out a CDP WebSocket URL and document no unsupported CDP methods (absence in the pages read, not proof). Session limits that matter for long-lived shared surfaces: Browserbase 60 s to 6 h, must connect within 5 min, CDP closed after 10 min without a command (heartbeat), `keepAlive` paid only; Kernel inactivity timeout 10 s to 72 h with standby after 5 s idle, CDP websocket about 1 h then reconnect to the same URL, `cdp_ws_url` carries a JWT in its query; Steel timeout default 5 min, plan caps 15 min / 1 h / 24 h, not extendable once live, profiles are userDataDir snapshots capped at 300 MB and deleted after 30 days. Kernel's documented computer controls match the routes read from the image binary (click_mouse, drag_mouse with a path, type_text, press_key, screenshot, clipboard). Still unverified: whether `Page.startScreencast` and `Input.*` perform over any provider's proxy, the added latency of a WAN hop on the 40-75 ms click path, and CDP method filtering beyond Cloudflare's documented readonly guardrail.
- [ ] Dial-in connector as the shape for every surface (Fredrik's lean 2026-10-15, brainstorm, design only): a server cannot attach to a user's localhost, so a small connector next to the browser dials OUT to the broker over a WebSocket and carries CDP for one shared target; the same connector runs on a laptop (share the browser on a device with a party), on a provider's host, or on a self-hosted browser box, so those are one mechanism in three places and the browser hosts need no inbound ports (also shrinks the egress and SSRF exposure). Precedent in `bulletins-chat`: `debug-relay.js`, where a pilot user's browser dials in over WebSocket and a loopback-only responder drives it, with a token row resolved by `authorizeResponder` on upgrade and on every revalidate tick, an actor on every record, consent as the app-written `permission_reply` (accept, decline, End), a sharer-visible `responderCount`, and `{end: true}` from the sharer; it evals JS inside the page, though, so it has no trusted input or pixels, which is what CDP adds. Open: sharing a real browser must be one tab only (raw CDP on a user's Chrome exposes every tab and cookie), so the connector attaches to a single target and enforces a method allowlist, view-only by default; plain view-only sharing may already be covered by LiveKit screen share (Prelo has LiveKit for calls, screen-share support unchecked), with the CDP path only for control and agents.
- [x] Phase 19 `_death` failure (`SIGTERM count_then_wait` saw `disconnected`, not `call_error`): a race in `ipc.Server.stop`, which sent the goodbye and closed while the reader thread, woken by the shutdown drain's cancel, had not yet written the cancelled reply. `stop` now waits up to `STOP_GRACE_S` (1 s) for in-flight handlers. It reproduced twice on one machine and not at all on later runs of the unfixed code, so the suite alone never proved the cure; phase 2 `_stop_flushes_inflight_reply` does (fails on the old `stop`: only the goodbye arrives).
- [ ] `gists/teamviewer.py` `wait_stable` polls 320 px screenshots (~0.7 s idle floor, ~2 s after a Recents animation). A `tab.screencast` assist is possible but unproven: frames are damage-driven (an idle screen sends none, and there is no first frame), a hidden tab sends none either (it needs `tab.front()`, which raises the user's Chrome tab), and frames carry no dirty rect, so it would only replace the poll with "a frame arrived" as the trigger for the same diff. Measured 2026-10-15: 0 frames in 5 s on the visible, idle tablet session; not measured: frame gaps during a real change, whether the remote cursor blink or clock repaint the canvas. Notes: `~/.local/share/docduty-search/memory/page-startscreencast-sends-no-initial-frame-a-visible-idle-page-delivers-zero-frames/NOTE.md`. Playwright's own stability check polls consecutive screenshots, not the screencast (its `Page.expectScreenshot`).
- [x] `gists/teamviewer.py` MCP tools (`tv_screenshot`, `tv_click`, `tv_drag`, `tv_scroll`, `tv_type`, `tv_key`): verified end to end 2026-10-15 through the tool list on the tablet: `tv_click` on the Settings icon, `tv_click` on its search field, `tv_type "battery"`, each returning the settled screenshot in one call (`waited` 1.1-1.5 s). There is no `confirm()` gate: Fredrik drives his own device and asked for it removed. All six verified 2026-10-15: `tv_scroll` (dy 600 moved the Settings list down), `tv_drag` (a swipe back to the top), `tv_key` (`Ctrl+A` then `Backspace` cleared the search field; `Escape` hid the keyboard like Android Back), each with `waited` 0.6-1.7 s. The action tools now return `path` (the settled screen with the action drawn in red: crosshair and label for click, arrow and start dot for drag, arrow for scroll, a top banner for type and key; regions that changed outlined in green, filled unless they cover over a quarter of the screen) and `raw` (unmarked), plus `changed` ("none" when nothing on screen changed) and `regions`. Verified on the tablet for click, scroll, drag and a no-op key (`changed: "none"`); the type and key banners and the double-click ring were rendered offline only. The before and after frames must be captured at the same size and downscaled by the same PIL resize: a Chrome-scaled 320 px before-frame made unchanged text read as changed. Clock and battery ticks in the status bar can show as a small green region. `serve()` (stdlib, loopback only, Host allowlist, random token on every request; 401 without the token, 403 for a foreign Host) shows each `tv_*` result live: the marked image, a raw toggle, a clickable history of the last 60. Verified with curl and a rendered page 2026-10-15; not covered: results from direct `Device` calls in `exec` are not recorded (only the tools), and nothing persists across a kernel restart. A coordinate bug found and fixed 2026-10-15: `Page.captureScreenshot` multiplies by `devicePixelRatio`, and the browser zoom had moved the session tab to 1.25, so `raw` came out 1600 px wide while clicks are mapped in the 1280 px frame; `_capture` now divides the clip scale by the DPR (checked: 1280 px wide again). The reported `height` can differ from the file by 1 px (996 vs 997, rounding). Explored the Autel MaxiSys app read-only through the tools (main menu, Diagnostics make list, History of 46 stored sessions, one DVI form opened and left empty); no car is connected (VCI2 shows red). Demo findings on the marks (MaxiSys navigation, 2026-10-15), all fixed in the gist: the green fill dimmed the real colors, so it is lighter and dropped when much of the screen changed; a click marker drawn on the previous screen's coordinates sat on unrelated content after a screen change, so a marker inside a large changed region now reads "(previous screen)"; `wait_stable` returned `stable: true` after 0.7 s on a screen caught mid cross-fade (the remote stream stalls mid-transition), so a change over 30% of pixels now waits one more 0.9 s quiet window and recaptures (about 1-3 s extra on transitions only). In-app page counters (`1/42`) and dot indicators were what made an overshoot readable, not the marks. Still open: `stable` stays a heuristic; an agent seeing a ghosted or blended image should re-screenshot. Untried: a longer multi-step agent task (one was started: Settings > About tablet gives model dv2379 and device name MaxiSys MS909S2, but no Android version row on that page). The gist is gitignored, so this entry is its only record.
- [ ] Do the marked screenshots (action in red, changes in green) help an agent? Unmeasured: run the same multi-step task on the tablet twice through the `tv_*` tools, once reading `path` (marked) and once reading only `raw`, and compare steps taken, wrong clicks and recoveries. Until then the structured `changed` and `regions` fields are the safer value; the drawing is a hypothesis. A control needs a switch, e.g. an env var or tool argument that drops the marks while keeping `changed`.


## Testing gaps

- [ ] **`browser/` has no direct coverage.** Phase 6 exercises the tools end to end, but
  `har.py`'s SQL, `capture.py`'s Fetch interception, and `cdp.py`'s reconnect/reattach paths
  are only touched incidentally. Both browser bugs found on 2026-07-31 were in that area,
  and both surfaced from *using* the code rather than from three separate reading passes that
  had all explicitly scoped it out.
- [ ] Phase 6 still needs a live Chrome on `:9222`. It is deterministic now — it opens its own
  `data:` tab, asserts against that alone, and cleans up in `finally` — but it skips silently
  without a browser, so CI would report green having run none of it.
- [ ] `cdp._async_prune` runs its `DELETE` on the loop. Measured at 4.5 ms for 5k of 50k rows,
  which is why it was left alone; the docstring's claim that running as a task "avoids
  blocking recv" is still wrong in kind (a task on the same loop defers *when* it blocks, not
  *whether*). Revisit only if the cap or the row count changes.

## Deferred by design

- [ ] **Dashboard gate surface.** `gates/list` and `gates/resolve` are kernel JSON-RPC methods
  and `gates.open_gates()` returns a JSON-ready shape specifically so a second surface is
  cheap — but only `repld gate` calls them. The dashboard already has the token, the RPC
  dispatch table, and a UI; wiring a pending-gates card is maybe 40 lines. Deliberately not
  MCP tools: an agent able to answer its own `confirm()` defeats the primitive.
- [ ] **Per-parameter descriptions beyond a bare string.** `Annotated[T, "..."]` carries a
  description and nothing else. Enum/min/max would need a dict metadata item; no demand yet,
  and `repld` staying dependency-free rules out borrowing pydantic's `Field`.

## Screenshot / vision

- [ ] Chunked screenshots — tile full-page and ultrawide captures into overlapping viewport-sized chunks (each ≤1440x900 token budget) instead of scaling down to unreadable sizes. Heuristic: chunk when either dimension would shrink below ~600px. Agent gets an array of images.
- [ ] Auto-viewport in `tab.screenshot()` — temporarily set `Emulation.setDeviceMetricsOverride`
  with `deviceScaleFactor: 1` and model-optimal dims before capture, then restore. Avoids
  client-side downscale entirely; text rendered at target resolution. Tradeoff: responsive
  breakpoints may trigger on viewport resize. **Must verify `document.documentElement.clientWidth
  === window.innerWidth` after applying the override, before capturing** — confirmed on real
  hardware that reapplying the override on an already-emulated tab can leave the two
  disagreeing (a self-contradicting state a real browser never produces on a fresh load), which
  silently corrupts the capture. Fall back to a fresh tab/reload if mismatched rather than
  proceeding on bad viewport data (session 011).

## MCP elicitation for human gates

- [ ] Map `ask()`/`confirm()`/`choose()` onto MCP `elicitation/create` (2025-06-18): the three
  form schemas fit exactly (StringSchema / BooleanSchema / single-select EnumSchema), and
  Claude Code renders elicitation as a prompt to the *human*, so the gates-are-not-tools
  invariant survives. Blocked on real bridge work: the bridge only ever forwards
  kernel→client **notifications**; a kernel-initiated *request* would get its client
  response mis-registered in `_inflight` and forwarded to the kernel as a fresh request
  (`bridge._dispatch_client_line`). Needs: (1) `_read_kernel` distinguishing kernel-origin
  requests (method + id) from responses, (2) a kernel-origin id set the response router
  checks before `_inflight`, (3) gate site (`gates._gate`) emitting the request only when
  the client declared the `elicitation` capability, falling back to today's
  `awaiting_human` channel push otherwise (and always for ambient gates with no origin
  session). First-wins with the pane/pill/CLI routes via the existing `resolve_gate` race.
  Deliberately split out of the 2025-06-18 upgrade (0.3.0) to keep the bridge diff isolated.

## Features (from session 002 backlog)

- [ ] Safari/iOS support — WebKit Inspector over usbmuxd (gist, not core)
- [ ] `py-align` as PyPI package — currently `~/.local/bin/` vendored script
- [ ] Vite plugin — auto-inject `data-testid` in dev mode (SvelteKit + Astro)
- [x] Multi-account Google gists (`~/.repld/gists/`: `gapi` auth + Gmail, `gcal`, `gdrive` with Docs/Sheets, `gmore` Tasks/contacts/Search Console/YouTube; gists not core). Live 2026-09-30: one account, 15 scopes, all nine APIs enabled in `oversikt-ai`, read and write paths tested, send/share/delete gated via `gapi.GATED`. Reuses the `repld` Desktop client.
  - [ ] Consent screen is still in *Testing*, so refresh tokens die after 7 days (~2026-10-07). Fix: complete the Branding page and Publish to *In production* (unverified, 100-user cap); needs the user in the console.
  - [x] Second account `bente.simensen@gmail.com` (Fredrik's mother, ultraportalen owner) logged in 2026-09-30 with 13 scopes; mailbox profile and labels read OK. Google stores gmail.com test users dotted (the undotted spelling is rejected as a re-spelling). Her Search Console level on `sc-domain:ultraportalen.no` is `siteUnverifiedUser` and queries return 403; Fredrik's account is `siteOwner` there, so ultraportalen GSC work uses his account.
  - [x] `gapi` inside an ultraportalen session: `paths.gist_dirs()` from that cwd lists `~/.repld/gists`, and the `ultraportalen-dc` session reported (2026-09-30) calling Gmail for Bente through `gapi` and hitting 429s, so the import and its `httpx` dependency work there. Reported by the peer, not run by this session.
  - [ ] Optional: a read-only `_tool_*` layer so the Google gists work as MCP tools, not only from `exec` (Gmail search/read, Calendar agenda/free-busy, Drive search, Search Console query); writes stay in `exec` behind `gapi.GATED`. Not built; `repld://gists/{name}` resources come for free but weren't read this session.
  - [ ] Inbox sorting at scale in `gapi.Gmail`: paged id listing (`nextPageToken`; `search` caps at 500), a real count by paging ids (`resultSizeEstimate` returned 201 for every query at `maxResults=1`, so it is useless there), `modify` chunked to 1000 ids per `batchModify`, throttled header fetches (unbounded `gather` will hit the per-user quota), a top-senders summary, `List-Unsubscribe` header, label rename/delete. Only `modify` (star/unstar) and draft creation are live-tested; archive, trash and filters are written but unrun.
  - [x] `gconsole` gist (`~/.repld/gists/gconsole.py`): API status/enable and OAuth test-user listing/adding, driven through the signed-in Chrome; the console has no public API for consent-screen settings, so this is UI automation. Verified live: `audience()`, API probe/enable, test-user add. Not built: removing a test user (the row's "Delete test user" button plus a Confirm dialog, done by hand once), publishing to In production, Branding fields. Google stores gmail.com test users in the account's dotted form, so re-adding a re-spelling is reported as rejected.
  - [ ] Untested live: `Gmail.send`/`send_draft`/`reply(send=True)` past the gate, `Drive.share`, invites via `Calendar.create(attendees=)`, attachments, `Gmail.attachment`, filters create/delete, Slides (no wrapper; use `Account.get`).
  - [ ] `~/.config/repld/google_token.json` and `gmail_credentials.json` predate `gapi`; the token is dead, delete it when convenient.

## OpenCode channel support

Researched while comparing Claude Code's Channels feature to other coding agents. `push_channel()`
(`kernel.py`) is Claude-Code-specific by construction — it declares `claude/channel` +
`claude/channel/permission` in the MCP `initialize` capabilities and broadcasts
`notifications/claude/channel` over the MCP connection. That's a dead end for OpenCode: its MCP
client (`packages/opencode/src/mcp/index.ts`) only registers handlers for the two standard MCP
notification types (`LoggingMessageNotification`, `ToolListChangedNotification`) — any custom
notification method, including ours, is silently dropped.

- [ ] Add a second delivery path in `push_channel()` for OpenCode targets, over HTTP instead of
  the MCP connection — the two mechanisms are unrelated:
  - Requires the OpenCode instance be launched network-reachable: plain `opencode` (not just
    `opencode serve`) accepts `--port`/`--hostname` (`withNetworkOptions` in `cli/network.ts`);
    without those flags it only talks to itself over an in-process fake URL. Auth is opt-in via
    `OPENCODE_SERVER_PASSWORD`/`_USERNAME` (HTTP Basic) — fine to skip for localhost-only.
  - Two calls needed, not one: `POST /tui/append-prompt` (`{text, workspace}` — routes by
    `directory`/`workspace` query params, no session ID needed) just inserts text into the visible
    input buffer; it does **not** execute. `POST /tui/submit-prompt` (no payload — submits
    whatever's currently in the box) is the separate call that actually runs it. Mirrors
    typing + Enter as two HTTP calls.
  - **Gotcha**: `append-prompt`'s handler does `input.insertText(...)` — inserts at cursor,
    does not replace. No `GET`-style endpoint exists to read the current buffer first. So a push
    landing while the user has an unsent draft typed will splice into it rather than queue
    cleanly or get rejected. Only defensive option is an unconditional `clear-prompt` before
    `append-prompt`, which is safe against garbling but silently destroys any in-progress human
    draft with no way to detect one first.
  - Source refs (OpenCode repo, `github.com/anomalyco/opencode`): `packages/opencode/src/mcp/index.ts`
    (notification handlers), `packages/opencode/src/server/routes/instance/httpapi/groups/tui.ts`
    + `handlers/tui.ts` (route defs), `packages/tui/src/component/prompt/index.tsx:237`
    (client-side append handler), `packages/sdk/js/src/v2/server.ts` (`createOpencodeServer`
    defaults to `127.0.0.1:4096`).

**Alternative to the HTTP workaround above: upstream native support.** OpenCode's MCP client is
built directly on the official `@modelcontextprotocol/sdk` (`Client`/`Protocol` classes, not a
custom implementation), and that SDK's `Protocol` base class already exposes
`fallbackNotificationHandler?: (notification: Notification) => Promise<void>` — a catch-all for
any notification method without a registered schema handler (confirmed in the SDK's own
`shared/protocol.d.ts:265`). OpenCode just doesn't use it. This would be a small, additive PR
rather than an architectural one — the exact pattern already exists twice in the same function.

- [ ] Sketch a PR against `packages/opencode/src/mcp/index.ts`'s `watch(s, name, client, bridge,
  timeout)` function (where the two existing `setNotificationHandler` calls live), adding:
  ```ts
  // alongside the existing setNotificationHandler(LoggingMessageNotificationSchema, ...) call
  client.fallbackNotificationHandler = async (notification) => {
    if (notification.method !== "notifications/claude/channel") return
    // gate on the server having declared the capability, mirroring the existing
    // `if (!client.getServerCapabilities()?.tools) return` pattern below — a server that
    // never declared claude/channel shouldn't be able to inject via this method name
    if (!client.getServerCapabilities()?.experimental?.["claude/channel"]) return
    const { content } = notification.params as { content: string; meta?: Record<string, unknown> }
    await bridge.promise(
      events
        .publish(TuiEvent.PromptAppend, { text: content, workspace: directory })
        .pipe(
          Effect.andThen(events.publish(TuiEvent.CommandExecute, { command: "prompt.submit" })),
          Effect.ignore,
        ),
    )
  }
  ```
  No HTTP round-trip needed at all — `events` (`EventV2Bridge.Service`) and `TuiEvent` are
  already imported in this file and used for the identical push pattern (`TuiEvent.ToastShow`,
  e.g. the "MCP Authentication Required" toasts a few lines above `watch()`). `directory` (the
  workspace this MCP connection belongs to) is already threaded through the enclosing scope via
  `InstanceState.directory` — may need promoting from closure-capture to an explicit `watch()`
  parameter depending on exact call-site scoping, the one part not fully nailed down.
  - **Open design question, not just implementation**: whether to literally adopt Claude Code's
    `claude/channel` capability name + `notifications/claude/channel` method (so repld's
    `push_channel()` works against OpenCode with **zero changes on repld's side**, and any other
    tool built for Claude Code Channels gets OpenCode support for free) vs. inventing an
    OpenCode-native `opencode/channel` equivalent. Adopting Claude's naming is a de facto
    cross-tool standard bet; inventing a new one avoids depending on another vendor's unstable
    (research-preview) contract. Worth raising as the actual PR discussion point, not deciding
    unilaterally in the diff.
  - If this lands upstream, repld's HTTP-bridge item above becomes unnecessary — `push_channel()`
    already speaks the protocol this PR would make OpenCode listen for.

## Observability

- [x] **`loop_blocked` distinguishes OS starvation from a real callback hold** —
  reported live by `claude-code-research-6d` against a wedged kernel under swap
  pressure: the fix's own `asyncio.to_thread()` advice was firing on wedges
  where the loop thread was never scheduled at all. `_is_starved` catches two
  frame-shape signatures (the whole stack is `selectors.*.select`, or a frame
  is `subprocess.py`'s `_execute_child` — fork/exec already on the loop by
  necessity); `_Pressure`/`_read_pressure()` additionally classify by load-
  past-core-count or swap-past-80% for the third case, an ordinary-looking
  frame (a DuckDB insert documented in microseconds) made genuinely slow by
  real thrashing. All three now read "loop thread descheduled by the OS" with
  a load/RAM/swap line instead of advice to rewrite already-async code.
- [x] **`loop_blocked` names the task holding the loop** — requested by
  `claude_code_research` (2026-09-25) after five pushes named bystanders while a gist's
  synchronous 173 MB read held the loop. Shipped: `kernel._loop_holder` (`asyncio.current_task`
  + the loop thread's frames), push routed to the holder's session, one `loop_blocked` per
  wedge closed by `loop_unblocked`, the kill cancels only the holder, and
  `REPLD_LOOP_KILL_THRESHOLD=0` disables it (`inf` used to crash the watchdog thread).

- [x] **`repld tasks`** — per-item listing of in-flight `defer()` tasks and active `@every`
  tickers, requested by the `claude_code_research` session (2026-09-19) for its curses
  inflight-task viewer. Shipped: `tasks_cmd.py`, `dashboard._collect_tasks()`, `started_at`
  on the task dict. See the CLAUDE.md subcommand list.
- [ ] Their optional ask-2: a JSON file beside `kernel.lock` (in the project runtime dir)
  rewritten on task start/finish, so a hook/viewer can read the roster without a process
  spawn + MCP handshake (~100–200ms via `repld exec`). Worth doing only if (1) ships and a
  concrete consumer wants push-free reads — the dashboard RPC above already answers "what's
  running" in one HTTP round trip, so this is a latency optimization on top of it, not a
  separate feature.
- [x] `repld status --json`'s `socket_path` closes the runtime-dir gap — confirmed by
  `claude_code_research-5c`: `dirname(socket_path)` is the runtime dir, so their hand-
  rederivation of `paths.project_slug`'s `{basename}-{sha256(realpath)[:8]}` rule (needed
  since their hook/viewer can't import repld under system python3) stays as-is; no
  `repld paths` command needed.
- [x] **`repld tasks --json` finish time** — shipped: `tasks.py`'s `finalize()` now sets
  `task["finished_at"] = time.time()` (wall-clock, beside the pre-existing monotonic
  `done_at` `_prune_spill_files` already owned), threaded through `snapshot()`,
  `core_schemas.py`'s `get_task` outputSchema, and `dashboard._collect_tasks()` as
  `finished_at` — epoch seconds, `None` while running. Requested by
  `claude_code_research-5c`'s inflight viewer, which had been aging failures out by *start*
  time for lack of one.
- [x] **`repld status --json --counts`** — shipped: an opt-in flag fetches
  `tasks_active`/`tickers` for every sibling too, via `lifecycle_cmd._sibling_counts()`
  reusing `_live_state()`'s dashboard round trip against each sibling's own socket/hint
  pair. Off by default; a sibling with no dashboard or no readable token keeps the keys
  absent, never `0`, so a reader can tell "idle" from "unknown". Requested by the same
  viewer, which Tab-cycles every live kernel (27 on the requester's machine).

## Infra

- [ ] CI + lint pass — `ruff` and `basedpyright` are trivial to wire; the smoketest is the
  question, since phase 16 needs a usable systemd user manager and skips silently without
  one. A CI run that reports green having skipped it is worse than no CI. Phase 6 is closer
  to CI-viable now: `smoketest.py` spawns its own throwaway headless Chrome
  (`harness.ThrowawayChrome`), so a CI image just needs the binary installed, not an
  already-running debug instance — but it still skips silently if that binary is missing,
  same open question as phase 16.
- [ ] GitHub Actions build pipeline for site — add when docs generation from `help.py` lands
- [ ] `scripts/gen-reference.py` — import `_TOPICS` + `GUIDE` from `help.py`, emit Starlight markdown at build time

## Kernel crash hardening

Follow-ups to 0.11.1-0.11.3 (rebind `old_id=`, bridge `kernel_crashed` push, bridge re-attach), from `claude_code_research` panes' OOM and `/clear` incidents on 2026-09-30.

- [x] `OOMPolicy=continue` in `spawn.py`'s `systemd-run` properties, unconditional (unlike `MemoryHigh`/`OOMScoreAdjust`, it overrides no deliberate policy): the default `OOMPolicy=stop` made a killed 6.7 GB child (eslint from a check suite) stop the whole service, taking every task and connection with it. Assert it in the phase-2 argv test. Does not help when the kernel itself is the largest process; the crash push covers that.
- [x] (solved without a `kind`: `rebind_claude_session` tie-breaks by process depth, the pane's bridge being shallowest; a `kind` on the tuple is still open if a hook ever needs to tell them apart) A subagent-connection `kind` on the session tuple (`ipc.Session.claude_session_kind`, `claude_sessions()`): a human-typed `/clear` has no `old_id` at the hook, so an ambiguous pid still raises until `rebind_claude_session` can skip a subagent's connection. First check what env a subagent's MCP bridge gets that its pane's lacks. Peer asks also: a parent session id on the tuple.
- [x] Crash reason in the `kernel_crashed` push (`bridge._announce_crash`): read the unit's `Result` (`oom-kill`) via `systemctl --user show`; needs the unit name from `spawn._systemd_unit_name`.
- [x] `push_delivered` is None both for an inline-answered cell and a parked push (peer finding 3): `push_state` on `get_task` / dashboard rows now names `none`/`broadcast`/`held`/`written`/`failed`; `push_delivered` unchanged.
- [ ] (deferred 2026-09-30: `claude_code_research` now reads the whole tag up to `>`, so the 600-char prefix problem is gone on their side) Channel tag order: `kind=` and `task_id=` before `roster=` (peer finding 4; the roster attribute ran to 621 chars) so fixed-prefix parsers work.

## Channel delivery to `claude --bg` sessions

Peer-reported by `claude_code_research` (2026-10-01, Claude Code v2.1.284, 3 posts per arm; readings in `~/Projects/private/claude_code_research/docs/cross-session-messaging.md` sections 5 and 5a, commits 385b11e and 310d127). Not reproduced here.

- [x] (built 2026-10-01, see CHANGELOG [Unreleased]; real `--bg` worker end to end not yet run) A channel push (`notify()`) does not reach a `claude --bg` session even when `claude_sessions()` lists it with kind `bg` and the worker's argv has the channel flags (0 of 3); an interactive session gets 3 of 3. A JSON line written by the kernel to `/run/user/<uid>/cc-socks/<session pid>.sock` starts a turn within about 1 s in an interactive, a `--bg` and a `claude -p --input-format stream-json` session (3 of 3 each, no `crossSessionInbound` setting). Decision to make: whether repld falls back to the inbox socket for kind `bg` targets, given its limits: text is framed as a peer message (origin `peer`, never the user's), no meta attributes (so `kind`/`task_id` ride in the text), and documented but unmeasured per-sender rate limits, dropped identical repeats and a 50-message queue that chatty kinds (console errors, dev-server events) could hit.
- [x] Bridge env of a `--bg` worker (peer-measured 2026-10-01 on two live workers, v2.1.284, one cold-spawned and one started with the daemon warm): the `repld bridge` child has `CLAUDE_CODE_MESSAGING_SOCKET`, `CLAUDE_CODE_SESSION_ID`, `CLAUDE_JOB_DIR`, `CLAUDE_PID` and the messaging token; `CLAUDE_CODE_SESSION_KIND` and `CLAUDE_CODE_CHILD_SESSION` are absent. `CLAUDE_CODE_MESSAGING_SOCKET` is the worker's own `cc-socks/<worker pid>.sock` (it exists, bridge ppid is the worker). `CLAUDE_PID` is NOT the worker: it is inherited from whatever invoked `claude --bg` (here 3535942, a pane's claude, via the kernel's own environment) and no longer exists once the workers stop, so the fallback must read the socket path from `CLAUDE_CODE_MESSAGING_SOCKET`, never derive it from `CLAUDE_PID` or the bridge's ppid walk alone. Consequence: the bridge can pass the path to the kernel at `initialize` next to the session id, so a `bg`-kind fallback needs no new discovery. Still open is the decision above.
- [x] Client-side channel signal: none. Probe run by `claude_code_research` 2026-10-01 (v2.1.284; interactive with/without `--dangerously-load-development-channels`, `--bg` with/without, `-p` stream-json with `PROBE_DECLARE` 1/0) and the six raw records re-read here: Claude Code's `initialize` params are identical in every arm, `capabilities` = `{"roots": {"listChanged": true}, "elicitation": {"form": {}, "url": {}}}`, `protocolVersion` `2025-11-25`, same `clientInfo`; nothing about channels, and the server's own declaration changes nothing client-side. So channel opt-in is the CLI flag, not a negotiated capability, and the fallback must key on session kind `bg`, not on capability. `CLAUDE_CODE_ENTRYPOINT` is `cli` in interactive and `--bg`, `sdk-cli` in `-p`. Side reading, not investigated: the interactive channels banner said `server:probe - no MCP server configured with that name` for a server defined only via `--mcp-config`.
- [x] `bg` inbox delivery failure now reads False (`ipc.Post` enum). Still open, unmeasured: per-sender rate limits and the 50-message queue under chatty kinds, and a `--bg` session whose process the supervisor stopped after its idle hour (socket gone).
- [x] Fallback run against a real `claude --bg` worker (peer-measured 2026-10-01, 0.11.5, cold-spawned worker, ephemeral bridge): 3 of 3 `notify(kind=...)` broadcasts delivered, each a user record within 0.1 s (origin `peer`, framed with the `[repld push kind="..."]` head) and answered in about 1.4 s. Not covered: a `task_done` push (so `push_state` on a `bg` origin), a held or failed write, rate limits under chatty kinds, a worker the daemon has stopped.
- [ ] `help.py`'s exec text and `core_schemas.py`'s `get_task` description still say a session that ends its turn never sees a later push "(confirmed for `claude --bg`)"; since 0.11.5 the inbox fallback starts a turn in an idle `--bg` worker (3 of 3, `claude_code_research/docs/cross-session-messaging.md` 5c). Also the "idle hour" above: 5d measured the host retiring an idle job at `idle 19m [low memory]`. Reported by `claude_code_research` 2026-10-13.
- [ ] A `--permission-mode bypassPermissions` `--bg` worker holds an inbox line whose sender did not attest a permission mode: worker `5eac4343`'s transcript (2026-10-13, v2.1.296) has two `system` `informational` records, "Held peer message — from an unidentified session [verified pid <kernel>] ... not delivered to Claude (1 held). The sender did not attest its permission mode", one for a `task_done` push and one for a `retrigger`; the kernel's `push_delivered` still read True / `written`. So the 0.11.5 inbox fallback (3 of 3 above, measured on a worker without bypass) starts no turn in a bypass worker, and a bypass `--bg` worker cannot be woken by a push. Unmeasured: whether the inbox JSON line can carry the sender's mode, and whether `crossSessionInbound: accept` on the worker changes it. Reported by `claude_code_research` 2026-10-13 (`docs/cross-session-messaging.md` 5e, commit dbb7fa1).
- [x] `push_state == "broadcast"` covered by phase 15 `_push_state_broadcast` (a `defer()` from `repld_init.py`).

## Gist durable state (open, Fredrik's call)

Peer-raised by `claude_code_research` (2026-10-01/02). `state.sweep_dead_project_dirs` rmtree's a dead kernel's whole project dir at boot, taking any file a gist wrote beside `kernel.sock` (their `roster.json`/`roster.jsonl`) with it; after an OOM-killed kernel `roster.tell()` raises KeyError for every name until each session re-reports. Decided against preserving foreign files in the runtime dir: the whole-dir sweep is the leak backstop, and an allowlist makes the dir's contents a public contract.

- [ ] Sanctioned durable helper `paths.gist_state_dir(name)` under `XDG_STATE_HOME`, never swept. Their one condition: a hook subprocess that cannot import repld must find it without a kernel, so the path also has to come out of `repld status --json` or a `repld paths` subcommand. If not taken, they derive the dir from the slug themselves and keep a copy beside the socket.
- [x] Profile persistence, Steel vs Kernel, through `gists/browsers.py` `start(profile=)` / `stop(cdp_port=)` (2026-10-15, restart-read as the criterion): Kernel keeps cookie and localStorage 3 of 3 when `stop` sends `Browser.close` first (12-15 s stop); without it the cookie is lost after a 10 s wait. Steel keeps localStorage every time but a cookie only 1 of 4 restarts, with `Browser.close` over repld's session (1 of 2), over a raw CDP socket (0 of 2) and with SIGTERM to Chromium's main process (0 of 1): its node supervisor relaunches Chromium as soon as it exits, and a cookie missing from `Cookies` right after a clean stop points at the cookie store, not the stop path; cause not found. Reading the DB on disk is a bad probe: Chrome keeps recent writes in `Cookies-journal`, so copy it too. Steel ignores SIGTERM (stop takes the full timeout, then SIGKILL). Lean: Kernel or a custom image for anything that must carry a login; Steel only for sessions that need no cookies to survive. Untested: Steel's own `sessionContext`/`persist` options.
