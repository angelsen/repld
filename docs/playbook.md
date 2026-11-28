# Working in repld: confirmed, not provisional

Everything in this file is a CONFIRMED fact a future cold-starting session
can act on without re-deriving it. A provisional hypothesis goes elsewhere;
a measurement or a break goes in `RUNLOG.md`. This file states rules.

## Cold-start budget

Read this file in full, then `CLAUDE.md`'s Testing section and `tests/phases/CLAUDE.md` — that's the
whole budget. `CLAUDE.md` itself is ~63KB and is the project's own design
reference; don't re-read it end to end per change, only the sections a
specific change touches.

## Repo facts

- Target repo: `/home/fredrik/Projects/private/repld`
- Trunk branch: `master`
- Check suite: `uv run python tests/smoketest.py --phase 19`, run on a round's tree before it commits. It is phases 1-19, everything but the paid phase 20: phase 6 starts its own headless Chrome (`harness.ThrowawayChrome`), so the machine needs a Chrome 140+ binary and nothing already running.
  - Lint, format and types are not in it: a round runs `make fmt && basedpyright` before it commits. **Never bare `ruff format --check`**: this repo's `align-comments` pass re-widens trailing-`#` comments that `ruff format` collapses, so a plain `--check` fails on an untouched master.
- Setup step (`SETUP`): none for the default gate. `make injected` (rebuilds the vendored Playwright engine) only matters for `src/repld/browser/injected_source.py`-adjacent work and is never run implicitly — hand-edit is refused (see CLAUDE.md).
- Review: `/code-review` over the branch's diff; CLAUDE.md's "Committing and landing" states the commit and land sequence.
- Commit conventions:
  - **Check the live session's attribution system-reminder before every commit.** `feedback_no_coauthored.md` (no trailer) is a fallback only — a session-level reminder saying "this replaces any earlier attribution guidance" always wins. Confirmed to matter: five commits on 2026-09-09 went out without a trailer an in-context reminder had already required from message one.
  - Never commit or push without being asked. Never a direct commit on `master`; the route is CLAUDE.md's "Committing and landing".
  - Commit messages explain *why*, not *what changed* (the diff already shows that).

## Gists

This project's own `./gists/` (`alibaba.py`, `aliexpress.py`, `device.py`,
`finn.py`, `prisjakt.py`, `shopify.py`, `speedybee.py`, `tiktok.py`,
`yr.py`) is Fredrik's personal automation, unrelated to work on repld itself:
don't assume any of those names matter to a fix. `gists/` is gitignored, so
a worktree has none.

## Live verification — the sanctioned path

repld's own test suite already drives real Chrome directly over CDP
(`tests/phases/browser.py`, on a headless Chrome 140+ the suite starts itself) — that IS
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

The release path (`uv version --bump patch` → `uv build` → `git push --tags` → `uv publish`, optionally `make deploy` for the docs site) is entirely manual, no CI — see CLAUDE.md's Releasing section for the exact sequence. `uv publish`, `uv version --bump` and `make deploy` are never run by a session on its own, and a release needs a live human go-ahead. No hook gates them; `~/.local/bin/guards/git` refuses `make deploy`'s forced `gh-pages` push.

## repld itself

- There is no project `repld_init.py`. `.claude/hooks/cc-check-hook` is the hook wrapper run by the `PostToolUse` hook in `.claude/settings.json`; forked from the deprecated `~/supervise/.claude/hooks/cc-check-hook`, it is fixed here and never copied again.
- Narration ratchet (`checks/narration.py`) baselined 2026-09-09 at 41 hits across 26 files — almost entirely legitimate "prior state" design-rationale prose in docstrings (CLAUDE.md and `src/repld/**/*.py` explain *why* by contrasting with what came before, which the project's own Invariant-comments style explicitly permits in design docs). The ratchet only refuses *growth* past that baseline, not the existing prose.
