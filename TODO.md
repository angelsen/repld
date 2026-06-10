# TODO

## Code cleanup

- [ ] Split `browser/tab.py` (1383 lines) — extract selector constants + resolution (~300 lines) and Row/Rows types + factory functions (~150 lines) into own modules. Tab class stays, drops to ~900 lines.
- [ ] `protocol.py` — add a `_response(rid, result)` helper next to `_error()`; the `{"jsonrpc": "2.0", "id": rid, "result": ...}` envelope is hand-built in ~12 handlers.
- [ ] `ipc.py:90–121` — extract a `_write_msg()` helper; write-JSON-line-and-flush pattern appears 3× in `Session`.
- [ ] `browser/tab.py:1308–1365` — `network()` and `console()` share an identical SQL conditions/bind-params/WHERE builder; factor out (fold into the tab.py split above).
- [ ] `browser/observe.py:400–415` — merge twin `snapshot_har_ids`/`snapshot_console_ids` (differ only by table name); drop the `_tab_key()` no-op alias (observe.py:396) while there.
- [ ] `help.py` — gists topic in `_TOPICS` overlaps GUIDE's gist section despite the "four surfaces, no overlap" principle; trim one side.
- [ ] Lockfile read+validate duplicated 3× — `ipc.py:35–68`, `kernel.py:49–63`, `help.py:820–841` all do read_text → json.loads → pid-alive check with the same exception handling; extract one `load_lock(path)` helper.
- [ ] `protocol.py:743–824` — mutation handlers (`_bh_navigate`/`_bh_key`/`_bh_click`/`_bh_type`) repeat the pre_observe → mutate → post_observe sandwich; factor an `_observed_mutation(...)` helper.
- [ ] `protocol.py:755–763` — `_bh_navigate` iframe block returns `{"error": ...}` as a *successful* result while every other handler raises → `_error()`; pick one error-signaling convention (keep the guidance text, route it consistently).
- [ ] `gists.py` — `try: ast.parse(path.read_text(...)) except: return default` appears 7× (lines 163, 355, 373, 425, 551, 573, 680); a `_parse(path) -> ast.Module | None` helper would collapse it.
- [ ] `gists.py:723–794` — `install_deps()` mixes prompt UI, choice parsing, and subprocess invocation in one 72-line function; extract choice parsing at least.
- [ ] `display.py:47` — `_YELLOW` constant is unused; delete.

## Features (from session 002 backlog)

- [ ] `tab.wait_for_idle()` — network-quiet without full observation pipeline
- [ ] `tab.scroll(selector, dy)` — sugar over swipe for containers
- [ ] Safari/iOS support — WebKit Inspector over usbmuxd (gist, not core)
- [ ] `py-align` as PyPI package — currently `~/.local/bin/` vendored script
- [ ] Vite plugin — auto-inject `data-testid` in dev mode (SvelteKit + Astro)

## Infra

- [ ] CI + lint pass
- [ ] Docs/marketing site (Astro/Starlight)
