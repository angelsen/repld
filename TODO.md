# TODO

## Code cleanup

- [ ] Split `browser/tab.py` (1383 lines) — extract selector constants + resolution (~300 lines) and Row/Rows types + factory functions (~150 lines) into own modules. Tab class stays, drops to ~900 lines.

## Features (from session 002 backlog)

- [ ] `tab.wait_for_idle()` — network-quiet without full observation pipeline
- [ ] `tab.scroll(selector, dy)` — sugar over swipe for containers
- [ ] Safari/iOS support — WebKit Inspector over usbmuxd (gist, not core)
- [ ] `py-align` as PyPI package — currently `~/.local/bin/` vendored script
- [ ] Vite plugin — auto-inject `data-testid` in dev mode (SvelteKit + Astro)

## Infra

- [ ] CI + lint pass
- [ ] Docs/marketing site (Astro/Starlight)
