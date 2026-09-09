---
name: repld
description: Work on repld — dispatching probes, fixes, features, or research into /home/fredrik/Projects/private/repld via the repld agentic workflow. Use when asked to work on repld, dispatch into repld, continue repld work, or pick up its backlog.
---

Read `docs/dispatch-playbook.md` FIRST — before dispatching, not when
hitting an error it already documents. It is the confirmed-facts file; its
`-archive.md` sibling (once one exists) holds the incident trail behind
each rule.

Then run the loop per `/devstack:dispatch` (the recipe) and
`/devstack:dispatch-loop` (invariants and traps), with this project's
values:

```python
REPO = "/home/fredrik/Projects/private/repld"
BASE = "master"
CHECK_ARGV = ["bash", "-c", "ruff check && ruff format --check && basedpyright && uv run tests/smoketest.py --phase 5"]
SETUP = None
ALIGN_SINCE = None
```

`CHECK_ARGV` stops at phase 5 — phase 6+ needs a live Chrome on
`--remote-debugging-port=9222`, not assumed present in a worktree. A
change touching `src/repld/browser/` needs phase 6 run by hand against a
real Chrome; it is not part of the automated gate.

Project-specific verification, gists, and deploy rules: the playbook's own
sections. Anything discovered this session that a future session would
need cold goes back into the playbook (confirmed facts) or its archive
(dated evidence) — not into this router.
