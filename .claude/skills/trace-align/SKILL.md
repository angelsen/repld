---
name: trace-align
description: Review a diff of repld's source and tests by tracing it, hold by hold and step by step, against the other writers of the state it touches, against the base it replaces, and against the tests that claim to cover it. Run headless by supervise's verdict.align (dispatch.align in .claude/devstack.json); --since <ref> reviews everything since that ref.
disable-model-invocation: true
---

# Trace alignment review

repld is one kernel process shared by an asyncio loop, one IPC reader
thread per connection and background threads, so a change is right when
every ordering of those is right, not when the reading order is. This
review traces the diff; it does not judge prose (that is `claims-align`).

## Inputs

1. `--since <ref>`: `git diff <ref> --name-only` for the files (committed
   and uncommitted alike) and `git diff <ref>` for the full diff. With no
   ref, the staged diff, falling back to the unstaged one.
2. Scope is `src/`, `tests/`, `gists/` and `scripts/`. A diff that touches
   none of them has nothing to trace: say so in one line and pass.
3. Read the whole of every changed function, then grep each attribute or
   module global it reads or writes and read every other function that
   touches the same state. A finding about a function you did not open is
   a guess.
4. Read the bullets of `CLAUDE.md`'s "Key subsystems" and "Architecture"
   sections that name a module the diff touches. Each bolded sentence
   there is an invariant the diff is held to.
5. Nothing is run. `tests/smoketest.py` starts real kernels and takes
   minutes; the suite is the land's gate, not this review's. Say in the
   report that the findings come from reading.

## Checks

1. **Each piece of shared state has one lock, and one hold per decision.**
   For every attribute the diff touches that a second thread or task also
   touches, name the lock and list the other writers. A finding is any of:
   a value read in one hold and acted on in a later one (a snapshot, then
   a second `with`); two structures that must agree (a map and its
   outbox, a pending entry and a succession) updated in separate holds; a
   helper that takes a `threading.Lock` called from inside a hold of the
   same lock; a callback, a socket write or any blocking call made under
   a lock; a second lock taken in an order the module does not already
   use.
2. **Every window is written out as an interleaving.** For each window
   the diff opens, narrows or says it closes, write the steps in order,
   naming the function each step is in, and the end state: which map
   holds the entry, who drains it, what the caller sees. When a comment
   or docstring says a hold closes a race, check the other party too: a
   counterpart that is itself two holds reopens it.
3. **Against the base, not only against the ideal.** Trace each failing
   interleaving at `<ref>` as well (`git show <ref>:<path>`). Label the
   finding "regression" when the base ended in the right state and the
   diff does not, "same as base" when both lose. A new guard or early
   return gets the same treatment: name one case the base handled that
   the guard now turns away, or say there is none.
4. **The invariants of the modules it touches hold.** From input 4: sync
   I/O that now runs on the asyncio loop, a wait on a `threading.Event`
   from the loop, a task spawned without a kept handle and a literal
   `repld-` name, a function documented as never raising that now can.
   Quote the `CLAUDE.md` sentence with the finding.
5. **What the diff adds has an end.** An entry added to a map, a pending
   or armed record, a held push, a spawned task: name what removes it
   (a consumer, an expiry, a close). One left armed with no consumer is
   a finding, with the later event that can pick it up by mistake.
6. **Each changed source path has a case that fails without it.** For
   every branch the diff changes under `src/`, name the case in
   `tests/phases/` that reaches it, or say that none does. For every case
   the diff adds or edits:
   - it fails on the base's source. Say which assertion would fail and
     why; a case that passes either way is vacuous.
   - order comes from state, not from a sleep. A fixed sleep standing
     for "after the kernel saw X" is a finding; the fix is a loop on
     kernel-side state with its own deadline.
   - a leg that can pass without reaching the code under test (the task
     ended early, the push was already queued) carries an assertion that
     pins the precondition.
   - cleanup cannot mask or leak: an `exec` or `close` in a `finally`
     that can raise replaces the first failure and skips what follows
     it, and a loop left in the shared phase kernel needs a deadline of
     its own.
   - `wait_notification` carries `where=` when more than one task, gate
     or ticker in the phase can push the same `kind=`.
   - it extends a phase through `harness.py`'s `Bridge`; a pure function
     gets its cases in phase 2 (`tests/phases/CLAUDE.md`).

## Severity

- **blocking**: a regression against the base; a deadlock or a re-entry
  on a non-reentrant lock; a `CLAUDE.md` invariant broken; a case that
  cannot fail on the base's source for the path the diff says it covers.
- **should-fix**: a losing interleaving that is the same as the base; a
  changed source path with no case; a sleep standing for an order; an
  entry with no end; cleanup that can mask or leak.
- **note**: anything else worth a human's eye, the checks that held
  included (the lock discipline, the acyclicity argument), one line each.

Only blocking gates.

## Output

Findings grouped by severity, each with its `path:/literal/` anchor (a
substring of the line, never a line number), the interleaving or the
trace, and a concrete fix. End with exactly one sentinel line:

    TRACE_ALIGN: PASS

or

    TRACE_ALIGN: FAIL (<n> blocking)
