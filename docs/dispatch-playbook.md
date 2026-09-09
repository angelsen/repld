# Dispatching into repld — confirmed, not provisional

Everything in this file is a CONFIRMED fact a future cold-starting session
can act on without re-deriving it. A provisional hypothesis goes elsewhere;
a dated incident narrative goes in `dispatch-playbook-archive.md` beside
this file, never here — this file states rules, the archive keeps the
evidence.

## Cold-start budget

Read this file in full, then `CLAUDE.md`'s Testing section — that's the
whole budget. `CLAUDE.md` itself is ~70KB and is the project's own design
reference; don't re-read it end to end per dispatch, only the sections a
specific change touches.

## Repo facts

- Target repo: `/home/fredrik/Projects/private/repld`
- Trunk branch: `master`
- Check suite (`CHECK_ARGV`): `["bash", "-c", "ruff check && ruff format --check && basedpyright && uv run tests/smoketest.py --phase 5"]` — lint/format/type gate plus phases 1–5 (core MCP plumbing, channels, lockfile/flock/bootstrap). **Not the full suite**: phase 6+ needs a real Chrome on `--remote-debugging-port=9222`, which a dispatch worktree won't have running by default. A change touching `src/repld/browser/` needs phase 6 run explicitly against a live Chrome — not part of the default gate, verify by hand.
- Setup step (`SETUP`): none for the default gate. `make injected` (rebuilds the vendored Playwright engine) only matters for `src/repld/browser/injected_source.py`-adjacent work and is never run implicitly — hand-edit is refused (see CLAUDE.md).
- Review skills (`ALIGN_SINCE`): none — no `/arch-align`/`/bits-align` equivalent here.
- Commit conventions:
  - **Check the live session's attribution system-reminder before every commit.** `feedback_no_coauthored.md` (no trailer) is a fallback only — a session-level reminder saying "this replaces any earlier attribution guidance" always wins. Confirmed to matter: five commits on 2026-09-09 went out without a trailer an in-context reminder had already required from message one.
  - Never commit or push without being asked. When asked to land work, this project's own pattern (used across five 2026-09-07/09 sessions) is: branch off master, commit, `git checkout master && git merge --ff-only <branch> && git branch -d <branch>`, then push — never a direct commit on `master`.
  - Commit messages explain *why*, not *what changed* (the diff already shows that).

## Gists

This project's own `./gists/` (`alibaba.py`, `aliexpress.py`, `device.py`,
`finn.py`, `prisjakt.py`, `shopify.py`, `speedybee.py`, `tiktok.py`,
`yr.py`) is Fredrik's personal automation, unrelated to dispatch work —
don't assume any of those names matter to a fix. The devstack gists
(dispatch/verdict/transcripts/roster) come from the plugin via
`repld_init.py`, already wired.

## Live verification — the sanctioned path

repld's own test suite already drives real Chrome directly over CDP
(`tests/phases/browser.py`, requires Chrome 140+ on `:9222`) — that IS
this project's live-verification path. No devloop wiring: it would be a
second, redundant mechanism for the same thing. For non-browser work,
`uv run repld` / `repld exec` against a throwaway project dir is the
equivalent of `judge_repros`.

## Skills to survey before calling feature work done

- `.claude/skills/gist/SKILL.md` — repld's own gist-authoring conventions, if a fix touches `gists/` or gist-facing behavior.
- CLAUDE.md's own "Design principles" section (substrate not library, channel push over polling, shared `__main__` namespace) — a feature whose output the agent has to hand-translate before another primitive can use it directly hasn't earned "done" per that section.

## Build quirks

- `uv`-managed venv (`uv_build` backend); tests spawn real kernel + bridge subprocesses via `uv run --project <repo> repld ...`, not in-process.
- `tests/smoketest.py` isolates `XDG_CONFIG_HOME` to a tempdir *before* importing any phase module — `repld.gists._REGISTRY_PATH` binds at import time, so a worktree's test run must not touch `~/.config/repld/gist-registry.json`.
- Runtime state (lockfiles, sockets, spills) lives under `$XDG_RUNTIME_DIR`/`/tmp/repld-{uid}`, never in the project dir — a stale kernel from a prior worktree run doesn't leak into the repo.
- `scripts/align-comments.py --fix-all` runs last in the lint chain on purpose (re-applies column alignment `ruff format` would flatten) — a fresh `ruff format --check` right after it will show drift again; that's expected, not a regression.

## Deploying

The release path (`uv version --bump patch` → `uv build` → `git push --tags` → `uv publish`, optionally `make deploy` for the docs site) is entirely manual, no CI — see CLAUDE.md's Releasing section for the exact sequence. `uv publish`, `uv version --bump`, and `make deploy` are all gated in `.claude/devstack.json`'s `pane_deploy_patterns` — never run from a dispatched worker's pane; a release needs a live human go-ahead.

## repld itself

- `repld_init.py` exists, wired to the devstack dispatch-loop gists (`gists.add_search_dir` → the devstack checkout). devloop's line is present but commented out — see "Live verification" above for why.
- `.claude/devstack.json`: `repld_roster: true` (this repo's kernel is real, already MCP-registered, and has had multiple sessions working against it over time — roster announces each to the others). `protected_repos: []` — repld hasn't dispatched into another repo's checkout yet; fill in if that changes.
- Narration ratchet (`checks/narration.py`) baselined 2026-09-09 at 41 hits across 26 files — almost entirely legitimate "prior state" design-rationale prose in docstrings (CLAUDE.md and `src/repld/**/*.py` explain *why* by contrasting with what came before, which the project's own Invariant-comments style explicitly permits in design docs). The ratchet only refuses *growth* past that baseline, not the existing prose.
