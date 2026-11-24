---
name: repld
description: Work on repld from a session inside the repo: the commit and land route, the check gate, and where dispatched work is driven from. Use when asked to work on repld, continue repld work, or pick up its backlog.
---

Read `docs/dispatch-playbook.md` FIRST: it is the confirmed-facts file
(check suite, commit conventions, build quirks, deploy rules).

Dispatched rounds are driven from the supervise cairn (`~/supervise`, its
own `repld` routing skill, `supervise arm --project repld`), not from here.
The devstack plugin's `/devstack:dispatch` skills are retired.

A session working in this repo directly commits and lands as CLAUDE.md's
"Committing and landing" states: a worktree under `.claude/worktrees/` on
`agent-<name>`, then the supervise peer's `verdict.commit_aligned`,
`verdict.verify` and `verdict.integrate`. The land passes no `check_argv`:
the cairn's routing skill supplies the check (`tests/smoketest.py --phase
19`), and lint, format and types are the round's own step before the commit
(`make fmt && basedpyright`).

Anything discovered that a future session would need cold goes back into
the playbook, not into this router.
