# TODO

## Site

- [ ] OG social images — `astro-og-canvas` per-page cards (`OGImageRoute` at
  `src/pages/open-graph/[...path].ts`) + Starlight `routeMiddleware` injection (docs/cards
  lack `og:image`). Terminal-noir bg + repld logo. Meta-tag plumbing already ships with a
  placeholder default in `SEO.astro`.
- [ ] Align TerminalHero breakpoint (640px) to the site-wide 560px convention.
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
exact pattern the gist's own usage docstring recommended). Root cause + fixes traced to
`src/repld/runtime.py:86-92` (`run_cell()`, single choke point: `print(repr(result))` when
`result is not None`).

- [ ] Multi-line `str` results: print verbatim instead of `repr()`-escaping. At
  `runtime.py:87`, special-case `isinstance(result, str) and "\n" in result` → `print(result)`
  instead of `print(repr(result))`. Trivial, single call site.
- [ ] Opt-out sentinel for auto-display — let a function suppress display of its own return
  value while still returning it for programmatic use (the `vps.py` case: print a formatted
  table, return the same string for composition by `db_size`/`db_tables`/etc., don't
  double-display on a bare call). E.g. a `repld.no_display(value)` wrapper or marker class,
  unwrapped before binding `_`/`_N` but skipped in the `print()` branch. No architectural
  changes needed — same single choke point in `run_cell()`.
- [ ] `repld://gists/{name}` signature listing renders `@property` methods with call
  parens — e.g. `devserver.py`'s `DevServer.pid`/`is_running` (real attributes, no parens)
  show up as `.pid() -> int | None` / `.is_running() -> bool`, so an agent reading only the
  listing calls `ds.pid()` and gets `TypeError: 'NoneType' object is not callable`. The
  gist's own hand-written class docstring gets it right (`ds.pid`, no parens) — only the
  auto-generated listing is wrong. Root cause: `gists.py` `_format_class()` (507-526) never
  inspects `item.decorator_list` before calling `_format_function()` (529-550), which
  unconditionally emits `.name(args) -> ret`. Fix: detect `property`/`cached_property` in
  the decorator list, pass an `is_property` flag through, and render `.name -> ret` (no
  parens, no args) for properties.

## MCP tool bugs

- [ ] `browser_fetch` MCP tool fails to submit `application/x-www-form-urlencoded`
  string bodies correctly — reproduced cleanly against a real SvelteKit form action:
  identical `url`/`method`/`headers`/`body` sent via `browser_fetch` gets a validation
  failure (server sees an empty/invalid form field), while the exact same request sent
  via `browser_js` with a raw in-page `fetch()` call succeeds. Ruled out (all clean):
  the tool's JSON-RPC schema (`protocol.py` ~270-290, `body` has no type coercion),
  the dispatcher (`_bh_fetch`, `protocol.py:809-817`, plain passthrough of `args.get("body")`),
  and `Tab.fetch()` itself (`browser/tab.py:1121-1165` — verified the generated JS
  literally in Node: a string body produces byte-identical `opts.body` either way, no
  double-JSON-encoding). Since `_bh_fetch` calls the *same* `tab.fetch()` coroutine as
  the Python-side `tab.fetch()` used inside `exec`, the two paths should be identical —
  so the discrepancy must be somewhere between the MCP transport (arg deserialization
  from the JSON-RPC call) and `_bh_fetch` being invoked, which is outside this repo's
  Python source as far as I could trace. Workaround: use `browser_js` with a manual
  `fetch()` call for POST bodies until someone with visibility into the MCP transport
  layer can dig further.

## Browser

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
- [ ] `tab.scroll(selector, dy)` — sugar over swipe for containers
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
