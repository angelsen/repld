# TODO

## Site

- [ ] OG social images — `astro-og-canvas` per-page cards (`OGImageRoute` at
  `src/pages/open-graph/[...path].ts`) + Starlight `routeMiddleware` injection (docs/cards
  lack `og:image`). Terminal-noir bg + repld logo. Meta-tag plumbing already ships with a
  placeholder default in `SEO.astro`.
- [x] Align TerminalHero breakpoint (640px) to the site-wide 560px convention (session 011).
- [x] Deploy the reworked site (`make deploy`) + push `master` (session 010).
- [x] **Mobile rendering pass** — horizontal overflow fixed (`overflow: hidden` on `.hero`,
  `.sect`, `.editorial .hero`); FAB bottom-right nav with slide-up bottom sheet at ≤560px
  replaces wrapping inline nav links. Verified landing + all 4 editorial pages + Starlight
  docs at 390px (session 010).
- [x] **Progression component** — extracted shared `Progression.astro` from landing + playbook
  (timeline left-border, dot nodes, scroll-triggered glow). Replaces landing's disconnected
  `phase-line` divs with the playbook's nicer continuous timeline (session 010).
- [x] SEO infra — `SEO.astro` (OG/Twitter/canonical/theme-color), `robots.txt`,
  `@astrojs/sitemap`, `SoftwareApplication` JSON-LD (session 009).
- [x] Font/prefetch perf — preload only above-the-fold weights (9→3), prefetch
  `viewport`→`hover`; cold LCP 2456→620ms on the local preview (session 009).
- [x] No-flash heading/content reveal + targeted heading-font gate (session 009).

## Code cleanup

- [x] Split `browser/tab.py` (~1400 lines) — extract selector constants + resolution (~300 lines) and Row/Rows types + factory functions (~150 lines) into own modules. Tab class stays, drops to ~900 lines.
- [x] `help.py` — gists topic in `_TOPICS` overlaps GUIDE's gist section despite the "four surfaces, no overlap" principle; trim one side.
- [x] `help.py` — "Multi-tab gists" paragraph duplicated between BROWSER_GUIDE and GUIDE; removed from GUIDE, kept authoritative copy in BROWSER_GUIDE (session 010).
- [x] Malformed `__repld_tools__` / `__repld_deps__` (non-literal expressions) fail `ast.literal_eval` and the tools/deps silently never appear (`gists.py` `_extract_tools` / `scan_deps`). Warn once on stderr at boot — not per `tools/list` scan, which would spam.

## Kernel / exec display

Found while debugging a `vps.py` gist double-print bug in bulletins-chat: methods that
`print(out)` for immediate human-readable output *and* `return out` for programmatic use
get their return value re-displayed by the auto-display hook — as an ugly single-line
`repr()` with escaped `\n`s — whenever the call is the bare last expression in a cell (the
exact pattern the gist's own usage docstring recommended). Root cause traced to
`src/repld/runtime.py` (`run_cell()`, single choke point: `print(repr(result))` when
`result is not None`).

- [x] Multi-line `str` results now print verbatim instead of `repr()`-escaping
  (`runtime.py:run_cell()` special-cases `isinstance(result, str) and "\n" in result`).
- [x] Opt-out sentinel for auto-display — `no_display(value)` wraps a return value so the
  cell-display hook skips the `print()` but still binds `_`/`_N` for programmatic use.
  Injected into `__main__` and `repld` module alongside `notify`/`defer`/etc.
  (`runtime._NoDisplay` + `runtime.no_display()`, kernel.py `_helpers`).
- [x] `repld://gists/{name}` signature listing no longer renders `@property`/
  `@cached_property` methods with call parens — `gists.py` `_format_class()` now inspects
  `decorator_list` (via new `_decorator_names()`), skips `@x.setter`/`@x.deleter` (getter
  already lists the name once), and `_format_function()` renders `.name -> ret` (no parens,
  no args) for properties.

## MCP tool bugs

- [x] `browser_fetch` MCP tool failed to submit `application/x-www-form-urlencoded`
  string bodies correctly. **Root cause found** (previously thought to be outside this
  repo, in the MCP transport): `Tab.fetch()` (`browser/tab.py`) auto-set
  `Content-Type: application/json` for dict bodies but set nothing for string bodies, so
  the browser defaulted to `Content-Type: text/plain;charset=UTF-8` — form-decoding
  servers then see an empty/invalid form. The bridge/ipc/dispatch path (`bridge.py`,
  `ipc.py`, `protocol.py` `_bh_fetch`) is a byte-transparent passthrough and was never
  the problem. Fixed: string bodies now default to
  `Content-Type: application/x-www-form-urlencoded` (curl `-d` semantics); caller
  `headers` still override, matched case-insensitively. Verified live: dict body →
  `application/json`, string body → `application/x-www-form-urlencoded`, explicit
  `headers` override wins in both cases (echo-server round-trip via a real kernel).
  Also found + fixed in the same pass: the `browser_fetch` tool's `body` schema had
  no declared `type`, so an MCP client could silently flatten a dict argument to a
  JSON string instead of sending an object — now `["object", "string"]`. Swept all
  other tool schemas in `protocol.py`; no other property is missing a `type`.

## Browser

- [x] `Tab.fetch()` corrupts binary responses — `browser/tab.py` `fetch()` always did
  `await r.text()`, so any non-text payload (ZIP, PDF, image, protobuf) came back as
  mangled mojibake instead of the actual bytes. Fixed: fetch as `arrayBuffer()`, try
  `TextDecoder('utf-8', {fatal: true}).decode(bytes)` — on failure (invalid UTF-8 ⇒
  binary), base64-encode instead (native `Uint8Array.prototype.toBase64()` when present,
  chunked `btoa` fallback for older Chrome) and return `base64Encoded: true`, matching
  the `{body, base64Encoded}` shape `tab.body()`/`Fetch.getResponseBody` already use.
  Verified live: a binary favicon (ICO, 5430 bytes) round-tripped through the native
  `toBase64()` path with `base64Encoded: true`; a JSON endpoint still auto-parses to a
  dict with `base64Encoded: false` (session 011).
- [x] Console error dedup — cross-tab duplicates within 2s collapsed into one push with count.
  Separate 30s hint window shows `browser.suppress("...")` nudge after 3 occurrences (session 010).
- [x] Console error suppress — `browser.suppress(substring)` mutes matching errors. Persists
  across kernel restarts via `.pyrepl.dashboard` hint file (session 010).

## Features (from session 003 backlog)

- [ ] `__repld_tools__` dict shorthand — allow `{"name": {"function": fn_ref, "description": "...", "parameters": {...}}}` and resolve function refs at import time, so gist authors don't need the `_tool_` naming convention

## Screenshot / vision

- [ ] Chunked screenshots — tile full-page and ultrawide captures into overlapping viewport-sized chunks (each ≤1440x900 token budget) instead of scaling down to unreadable sizes. Heuristic: chunk when either dimension would shrink below ~600px. Agent gets an array of images.
- [ ] Auto-viewport in `tab.screenshot()` — temporarily set `Emulation.setDeviceMetricsOverride`
  with `deviceScaleFactor: 1` and model-optimal dims before capture, then restore. Avoids
  client-side downscale entirely; text rendered at target resolution. Tradeoff: responsive
  breakpoints may trigger on viewport resize.
- [x] PNG unfilter bug — `_resize_png` read filtered scanlines as raw pixel data; Chrome's
  PNG encoder uses Sub/Up/Average/Paeth filters, so every resized screenshot was garbled.
  Added standard unfilter pass before nearest-neighbor sampling (session 010).
- [x] Viewport hint in `browser_screenshot` tool description — suggests
  `Emulation.setDeviceMetricsOverride` at 1440×900 (desktop) or 390×844 (mobile) with
  `deviceScaleFactor: 1` for crisp text (session 010).

## Features (from session 002 backlog)

- [x] `tab.wait_for_idle()` — network-quiet without full observation pipeline (already implemented)
- [x] `tab.scroll(selector, dy=0, dx=0)` — sugar over swipe for containers; resolves
  selector to its center and swipes in the opposite direction (scrollBy semantics:
  positive dy scrolls down, positive dx scrolls right). Verified the coordinate math
  live (stubbed `swipe()` to capture args) — real touch-gesture scrolling doesn't
  register reliably in this headless/CDP setup regardless, an environment quirk
  unrelated to the new code (session 011).
- [ ] Safari/iOS support — WebKit Inspector over usbmuxd (gist, not core)
- [ ] `py-align` as PyPI package — currently `~/.local/bin/` vendored script
- [ ] Vite plugin — auto-inject `data-testid` in dev mode (SvelteKit + Astro)

## UX

- [ ] "Synced to sheet" badge — could link directly to the sheet/row

## Infra

- [ ] CI + lint pass
- [x] Docs/marketing site (Astro/Starlight) — landing + playbook + Starlight scaffold in `site/`
- [ ] GitHub Actions build pipeline for site — add when docs generation from `help.py` lands
- [ ] `scripts/gen-reference.py` — import `_TOPICS` + `GUIDE` from `help.py`, emit Starlight markdown at build time
