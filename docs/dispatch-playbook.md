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
- Check suite: `uv run python tests/smoketest.py --phase 19`, named by the cairn's routing skill for repld (`~/supervise/.claude/skills/repld/SKILL.md`, `check`) and run by `verdict.verify` and by a land, which passes no `check_argv` of its own. It is phases 1-19, everything but the paid phase 20: phase 6 starts its own headless Chrome (`harness.ThrowawayChrome`), so the machine needs a Chrome 140+ binary and nothing already running.
  - Lint, format and types are not in it: a round runs `make fmt && basedpyright` before it commits. **Never bare `ruff format --check`**: this repo's `align-comments` pass re-widens trailing-`#` comments that `ruff format` collapses, so a plain `--check` fails on an untouched master.
- Setup step (`SETUP`): none for the default gate. `make injected` (rebuilds the vendored Playwright engine) only matters for `src/repld/browser/injected_source.py`-adjacent work and is never run implicitly — hand-edit is refused (see CLAUDE.md).
- Review skills (`ALIGN_SINCE`: the branch's base): `trace-align` and `claims-align`, named by `dispatch.align` in `.claude/devstack.json`; CLAUDE.md's "Committing and landing" states the commit and land sequence.
- Commit conventions:
  - **Check the live session's attribution system-reminder before every commit.** `feedback_no_coauthored.md` (no trailer) is a fallback only — a session-level reminder saying "this replaces any earlier attribution guidance" always wins. Confirmed to matter: five commits on 2026-09-09 went out without a trailer an in-context reminder had already required from message one.
  - Never commit or push without being asked. When asked to land work, this project's own pattern (used across five 2026-09-07/09 sessions) is: branch off master, commit, `git checkout master && git merge --ff-only <branch> && git branch -d <branch>`, then push — never a direct commit on `master`. That is the by-hand path; a round run from a kernel commits and lands as CLAUDE.md's "Committing and landing" says.
  - Commit messages explain *why*, not *what changed* (the diff already shows that).

## Landing

`supervise committed --branch agent-<name> --repo <main checkout>` is the door; `verdict.integrate` is what runs behind it, or directly under a grant: `verdict.integrate(repo, branch, name, align_since=<the branch's base>)`, a peer call to the supervise kernel like the commit. It rebases the branch when the trunk has moved and runs the check on that tree before it pushes; the two reviews are reused from the commit while the diff is unchanged, and run again when a rebase changed it. With no `check_argv` the check is the cairn's for repld (`tests/smoketest.py --phase 19`), the one `verdict.verify(worktree)` signs; a receipt names a tree and a check, so the suite reruns whenever either differs (a rebase, another argv). Lint, format and types are in neither: `make fmt && basedpyright` is the round's own step before the commit. A dispatched session is admitted by a `supervise grant <repo> <sha> land --word "<the sentence>"` minted for that sha by Fredrik's shell (which may omit `--word`) or the master seat (a session passes `--word`, his sentence typed into its own conversation), and a grant match admits at once, before any receipt is looked for; or, with the master's lands switch on and no `lands: ask` in the routing skill, when `supervise committed` accepts the branch: an `agent-<name>` branch paired with the reported name (a worktree under `.claude/worktrees/` must carry that name), a clean worktree, the trunk checkout clean and level with origin, and a green `verify` receipt for the head, which `supervise committed` runs itself when there is none (`commit_aligned` writes none, so a direct `integrate` admitted by the lands switch needs `verify` run first; one under a grant does not).

## Gists

This project's own `./gists/` (`alibaba.py`, `aliexpress.py`, `device.py`,
`finn.py`, `prisjakt.py`, `shopify.py`, `speedybee.py`, `tiktok.py`,
`yr.py`) is Fredrik's personal automation, unrelated to dispatch work —
don't assume any of those names matter to a fix. `gists/` is gitignored, so
a worktree has none. `verdict` and `roster` are not importable in this
kernel: they run in the supervise kernel, reached as a peer
(`peers.get("supervise").call("verdict.commit_aligned", ...)` under
`defer()`), as CLAUDE.md's "Committing and landing" states.

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

The release path (`uv version --bump patch` → `uv build` → `git push --tags` → `uv publish`, optionally `make deploy` for the docs site) is entirely manual, no CI — see CLAUDE.md's Releasing section for the exact sequence. `uv publish`, `uv version --bump` and `make deploy` are never run from a dispatched worker's pane, and a release needs a live human go-ahead. No hook gates them; `~/.local/bin/guards/git` refuses `make deploy`'s forced `gh-pages` push.

## repld itself

- The workflow layer lives in supervise. What is left here under the devstack name is `.claude/devstack.json`, which supervise still reads, and nothing else: there is no project `repld_init.py`, and `.claude/hooks/cc-check-hook` is this repo's own copy of the hook wrapper (the cairn's `~/supervise/.claude/hooks/cc-check-hook` is the source).
- Narration ratchet (`checks/narration.py`) baselined 2026-09-09 at 41 hits across 26 files — almost entirely legitimate "prior state" design-rationale prose in docstrings (CLAUDE.md and `src/repld/**/*.py` explain *why* by contrasting with what came before, which the project's own Invariant-comments style explicitly permits in design docs). The ratchet only refuses *growth* past that baseline, not the existing prose.
