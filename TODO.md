# TODO

## Site (next up: mobile)

- [ ] **Mobile rendering pass** — verify the reworked site at 375/390px: the new
  conversation cards (`.session-card`), the controls **pipeline timeline** (`.sc-stage`
  grid), the reordered sections, hero, and nav. Check the `@media (max-width: 900px / 560px)`
  blocks in `site/src/pages/index.astro` + `site/src/styles/global.css` and the editorial
  pages. Use the repld browser device emulation + screenshots to iterate.
- [ ] OG social images — `astro-og-canvas` per-page cards (`OGImageRoute` at
  `src/pages/open-graph/[...path].ts`) + Starlight `routeMiddleware` injection (docs/cards
  lack `og:image`). Terminal-noir bg + repld logo. Meta-tag plumbing already ships with a
  placeholder default in `SEO.astro`.
- [ ] Deploy the reworked site (`make deploy`) + push `master`.
- [x] SEO infra — `SEO.astro` (OG/Twitter/canonical/theme-color), `robots.txt`,
  `@astrojs/sitemap`, `SoftwareApplication` JSON-LD (session 009).
- [x] Font/prefetch perf — preload only above-the-fold weights (9→3), prefetch
  `viewport`→`hover`; cold LCP 2456→620ms on the local preview (session 009).
- [x] No-flash heading/content reveal + targeted heading-font gate (session 009).

## Code cleanup

- [x] Split `browser/tab.py` (~1400 lines) — extract selector constants + resolution (~300 lines) and Row/Rows types + factory functions (~150 lines) into own modules. Tab class stays, drops to ~900 lines.
- [x] `help.py` — gists topic in `_TOPICS` overlaps GUIDE's gist section despite the "four surfaces, no overlap" principle; trim one side.
- [x] Malformed `__repld_tools__` / `__repld_deps__` (non-literal expressions) fail `ast.literal_eval` and the tools/deps silently never appear (`gists.py` `_extract_tools` / `scan_deps`). Warn once on stderr at boot — not per `tools/list` scan, which would spam.

## Features (from session 003 backlog)

- [ ] `__repld_tools__` dict shorthand — allow `{"name": {"function": fn_ref, "description": "...", "parameters": {...}}}` and resolve function refs at import time, so gist authors don't need the `_tool_` naming convention

## Screenshot / vision

- [ ] Chunked screenshots — tile full-page and ultrawide captures into overlapping viewport-sized chunks (each ≤1440x900 token budget) instead of scaling down to unreadable sizes. Heuristic: chunk when either dimension would shrink below ~600px. Agent gets an array of images.

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
