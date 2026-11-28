---
name: repld
description: Work on repld from a session inside the repo: the branch, check and land route, and where the plan lives. Use when asked to work on repld, continue repld work, or pick up its backlog.
---

Read `RUNLOG.md` first: its Plan says which session of the current effort is next, and its
rows say how each fact is known. Then `docs/playbook.md` for the check suite, commit
conventions and build quirks.

Commit and land as CLAUDE.md's "Committing and landing" states: a worktree under
`.claude/worktrees/` on `agent-<name>`, the smoketest and `make fmt && basedpyright`, then
`/code-review` over the diff, then a fast-forward merge to master when Fredrik says so.

Anything a future session would need cold goes back into the playbook or `RUNLOG.md`, not into
this router.
