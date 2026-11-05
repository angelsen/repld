export const meta = {
  name: 'browser-fix',
  whenToUse: 'args: {groups?: string[]} optional subset of group keys (ownership, transport, perf). Fixes the browser-audit findings, test first, one worktree per group.',
  description: 'Fix the browser-audit findings in file-disjoint groups: each worker writes a test that fails on current code, fixes it, a cold judge confirms, accepted diffs land serially on master (no commits)',
  phases: [
    { title: 'Fix', detail: 'one worker per group, own worktree, failing test first, no commit' },
    { title: 'Judge', detail: 'cold read: test fails without the fix, fix is minimal, invariants kept' },
    { title: 'Integrate', detail: 'apply accepted diffs one at a time, run phase 19 once, add CHANGELOG bullets' },
  ],
}

const REPO = '/home/fredrik/Projects/private/repld'
const WORKERS = 2 // each worker runs smoketest kernels and a throwaway Chrome; the machine has 14 GB

const GROUPS = {
  ownership: {
    files: 'src/repld/browser/cdp.py, src/repld/browser/pool.py',
    phases: '1-5',
    only: ['the new cases you add to tests/phases/browser.py or its sibling'],
    items: [
      {
        id: 'OWN-1',
        text: `CDPSession.set_mode (cdp.py ~1098) assigns self.owner only when tasks.current_origin() is non-None. A take() from a ticker (kernel._start_ticker clears both contextvars) or a peer call leaves owner at its old value; on a shared tab that is taken=True, mode="agent", owner=None: route_to returns None (every push dropped), release_owned_by never matches it, and the file-chooser intercept stays armed. Decide the correct rule for a take() with no origin (refuse? stay shared? keep taken but name the cost) from CLAUDE.md's "A tab is agent or shared" and "Browser pushes route" bullets, and state the choice in your summary.`,
      },
      {
        id: 'action-depth-ooo-exit',
        text: `CDPSession.action (cdp.py ~1070-1096): spans nest across sessions (navigate/reload/invoke take no front flock). If the outer span exits before the inner one, "self._action_origin = outer" at depth>0 restores the OUTER origin (None) while the inner span still runs, and the picker intercept toggled by the outer span is never turned off (toggle only reaches the elif at depth 0, and the inner span had toggle=False). Make exit order irrelevant: for instance a stack of (origin, toggle) entries, with the current origin the top live entry and the intercept re-evaluated from \`intercepting\` at depth 0. Keep ACTION_GRACE_S and route_to semantics.`,
      },
      {
        id: 'OWN-3',
        text: `BrowserPool.release_owned_by (pool.py ~221) turns the intercept off but a chooser Chrome already intercepted (_filechooser_pending) stays unresolved, and the push announcing it went to the now-closed owner, so the page's file input is stuck until navigation. Resolve or clear a pending chooser when its owner's tab is released. Also delete the try/except in release_owned_by: set_mode cannot raise (intercept_file_chooser swallows), and CLAUDE.md House style rule 8 says delete, don't guard.`,
      },
      {
        id: 'cleanup-skips-heartbeat-cancel',
        text: `CDPSession.cleanup (cdp.py ~1692) does not cancel _heartbeat_task or the _captcha_watch task, so a destroyed tab's heartbeat keeps calling tab.js() against a dead session for up to 15 s. Cancel both in cleanup. Check how tab.py:474-524 owns the heartbeat before deciding where the handle lives; do not edit tab.py (another worker owns it): if the fix needs tab.py, record that in skipped.`,
      },
      {
        id: 'auth-log-unbounded',
        text: `CDPSession._auth_log (cdp.py ~975, appended ~711) is never cleared, unlike _dialog_log and _filechooser_log which observe.pre_observe clears. observe.py belongs to another worker: bound _auth_log inside cdp.py (a maxlen deque, or trim on append) rather than touching observe.py.`,
      },
    ],
  },
  transport: {
    files: 'src/repld/browser/session.py',
    phases: '1-5',
    only: ['the new cases you add'],
    items: [
      {
        id: 'pending-clear',
        text: `BrowserSession._reconnect_inner (session.py ~365): "self._pending.clear()" drops EVERY in-flight future, on the claim that the caller driving the reconnect will retry them. Only the driver retries; other concurrent execute() callers wait their full timeout (up to 30 s) on orphaned futures and fail with a non-recoverable TimeoutError. Fail the non-driver futures with the recoverable connection error (CLAUDE.md: a lock across a CDP command needs an unlocked core; asyncio.Lock is not reentrant, so do not make the retry path take a lock). Note _teardown_ws's CancelledError bypasses _fail_pending (BaseException). Test with a fake websocket server: three concurrent execute() calls, drop the connection on the first send, all three recover or fail fast.`,
      },
      {
        id: 'stale-session-reattach-reconnect',
        text: `session.py ~594-645 (_reattach_core) and the execute retry (~440-467): a navigation-triggered reattach_session issues Target.attachToTarget via execute(); if the socket dies mid-command, execute triggers _reconnect, which reattaches the same cdp (session B), then the retry attaches again (session C). _sessions then maps both B and C to the cdp: a leaked Chrome session, duplicate events, double store growth. Detach or drop the superseded session id, or make the retry notice the reconnect already reattached. Test by counting _sessions entries per chrome_target_id after recovery. Keep the "reconnect path always calls the core" rule.`,
      },
      {
        id: 'is-recoverable-catches-cdpcommanderror',
        text: `_is_recoverable (session.py ~310-326): CDPCommandError subclasses RuntimeError, so a CDP error whose message contains "not connected" is treated as a transport error and triggers a spurious reconnect. Its docstring says it is not for CDP-level errors; enforce that. While here, is_scope_refusal (session.py ~76) substring-matches str(exc) on any exception type: restrict it to CDPCommandError so page-controlled text from a Runtime.evaluate exception cannot trip it (tab.py:848 is its only caller; do not edit tab.py, the signature stays).`,
      },
      {
        id: 'pattern-owners',
        text: `session.py ~743 and ~1061-1066: _pattern_owners maps a pattern to the LAST Session that registered it. When that session closes, _pattern_owner returns the closed session, route_to falls through, and new matching tabs route nowhere even though an earlier watcher is alive. Keep every live registrant and pick a live one (the most recent live one) at auto-attach.`,
      },
      {
        id: 'is-recoverable-note-and-reconnect-parallel',
        text: `_reconnect_inner re-attaches sessions serially (session.py ~392-406); with 20 tabs over a 50 ms link that is about 1 s. Gather them ONLY if you can show the _reconnect_task guard in execute() and the per-session ordering still hold; otherwise put it in skipped with the reason. This one is optional and last.`,
      },
    ],
  },
  perf: {
    files: 'src/repld/browser/observe.py, src/repld/browser/png.py, src/repld/browser/tab.py',
    phases: '1-5',
    only: ['phase_6_click_round_trips and any typing or screenshot case that exists', 'the new cases you add'],
    items: [
      {
        id: 'seed-screenshot-sync-pil',
        text: `observe._seed_screenshot (observe.py ~894-925) runs synchronous Pillow decode, crop, resize and metadata embed (png.py ~54-98) on the shared loop after the awaited captureScreenshot; the audit estimates 30-80 ms at 2x DPR. MEASURE first (a 2880x1800 PNG, time the synchronous section), then move it to asyncio.to_thread if the number justifies it, and measure after. If it is under about 5 ms, put it in skipped with the number. Keep the output bytes identical.`,
      },
      {
        id: 'type-chars-2n-roundtrips',
        text: `tab._type_chars (tab.py ~1620-1633) sends keyDown+keyUp per character, each awaited, under the front flock: 2N serial round trips, so 100 characters at 25 ms RTT hold the flock for about 5 s and block every other session's input action on that Chrome. Chrome answers a session's commands in order, so sends issued together cost one round trip (CLAUDE.md, resolve_engine_selectors note): send a character's keyDown and keyUp together, and where the call has no per-key delay consider batching further, but keep press-before-release ordering and what type()'s receipt verifies. MEASURE with an injected-latency proxy or a fake session before and after; keep phase_6_click_round_trips unchanged.`,
      },
    ],
  },
}

const RESULT = {
  type: 'object',
  required: ['worktree', 'group', 'ok', 'fixed', 'skipped', 'tests', 'checks', 'summary'],
  properties: {
    worktree: { type: 'string' },
    group: { type: 'string' },
    ok: { type: 'boolean' },
    fixed: { type: 'array', items: { type: 'string' } },
    skipped: { type: 'array', items: { type: 'object', properties: { id: { type: 'string' }, reason: { type: 'string' } } } },
    tests: { type: 'array', items: { type: 'object', properties: { id: { type: 'string' }, case: { type: 'string' }, failed_before: { type: 'string' } } } },
    measurements: { type: 'array', items: { type: 'string' } },
    checks: { type: 'object' },
    summary: { type: 'string' },
  },
}

const VERDICT = {
  type: 'object',
  required: ['accept', 'tests_fail_without_fix', 'fix_minimal', 'invariants_kept', 'issues', 'summary'],
  properties: {
    accept: { type: 'boolean' },
    tests_fail_without_fix: { type: 'boolean' },
    fix_minimal: { type: 'boolean' },
    invariants_kept: { type: 'boolean' },
    issues: { type: 'array', items: { type: 'string' } },
    summary: { type: 'string' },
  },
}

const INTEGRATION = {
  type: 'object',
  required: ['landed', 'dropped', 'final_checks'],
  properties: {
    landed: { type: 'array', items: { type: 'string' } },
    dropped: { type: 'array', items: { type: 'object', properties: { group: { type: 'string' }, reason: { type: 'string' } } } },
    final_checks: { type: 'object' },
  },
}

const LINT = 'ruff check --fix && ruff format . gists && python3 scripts/align-comments.py --fix-all && basedpyright src'

function workerPrompt(key, g) {
  const items = g.items.map(i => `### ${i.id}\n${i.text}`).join('\n\n')
  return `You are fixing a set of audited bugs in the repld browser object. You are in your own git worktree of ${REPO}; run \`pwd\` first and report it as \`worktree\`. Work only in this worktree.

Group: ${key}. You own these source files and no others: ${g.files}. Other workers own the other browser files at the same time; if a fix needs a file you do not own, put that item in \`skipped\` with the reason. Tests may go in tests/ (read CLAUDE-ARCHIVE.md's phase 6 and 14 entries and the existing cases first; add your cases in a new function or file registered with the smallest possible edit to the phase entry, since other workers register theirs in the same place). A case that is not imported in tests/smoketest.py and called inside its phase's lambda never runs: wire every new case there, and confirm it with a plain \`--phase 6\` listing or run, not only \`--only\`. Do not let \`ruff --fix\` change a file outside your scope: check \`git status --short\` and \`git diff --stat\` before returning and revert stray hunks).

The findings come from an audit by agents that read the code; none was run. Treat each as a hypothesis. For EACH item, in order:
1. Re-read the code and confirm the failure scenario. If it is not real, put it in \`skipped\` with the evidence; do not fix what you cannot reproduce.
2. Write a test that FAILS on the current code and record the failing output's last lines as \`failed_before\`. Real Chrome or a real fake websocket server, never a mock of the thing under test (CLAUDE.md Testing; the project doctrine is real targets). Use a throwaway \`REPLD_LOOP_GUARD=raise\` kernel as the smoketest does.
3. Make the smallest fix that passes it. Follow CLAUDE.md's House style and the invariants in its Key subsystems and Architecture sections, and the matching CLAUDE-ARCHIVE.md entries before changing code a rule names. Comments state only invariants (no "fixed", no history). ASCII only.
4. Do not change any public name or signature. Do not add structure for a use case that has not arrived.

${items}

Checks, all required, in this order; a worktree has no venv, so run \`uv sync\` first:
- \`${LINT}\` ends with 0 errors (known pre-existing: SIM202 in tests/phases/pure.py, errors under gists/).
- \`python3 checks/narration.py\` reports no file above baseline.
- \`uv run tests/smoketest.py --phases ${g.phases}\` ends with \`== all phases passed ==\`.
- Each new case, and the existing cases for what you touched: ${g.only.join('; ')}. Run them one after another with \`--only <case> --phase 6\` (or the phase that owns them), never in parallel; each starts its own Chrome.
Never run \`git commit\`, \`git stash\`, \`git reset\` or anything else that changes git state.

Return the schema: worktree, group, ok (true only if every check passed), fixed (item ids), skipped (id + reason), tests (id, case name, failed_before), measurements (any before/after numbers, with units), checks (command to last line), and a summary of at most four sentences: what changed, any rule you had to choose (state it), and what you are unsure of.`
}

function judgePrompt(r, g) {
  return `You are the independent judge of a bug-fix set you did not write. Be adversarial: look for a reason to reject, and for what the fix broke.

Repository: ${REPO} (master, untouched). Worktree with the change: ${r.worktree}. Group: ${r.group}; files in scope: ${g.files} plus tests. The worker reports fixed ${JSON.stringify(r.fixed)}, skipped ${JSON.stringify(r.skipped)}, tests ${JSON.stringify(r.tests)}, measurements ${JSON.stringify(r.measurements || [])}.

Decide:
1. tests_fail_without_fix: for each fixed item, the new test fails on the old code. Do not trust failed_before: in ${r.worktree} run \`git diff -- src > /tmp/${r.group}-src.patch\`, \`git apply -R /tmp/${r.group}-src.patch\`, run the new case and see it fail, then \`git apply /tmp/${r.group}-src.patch\` to restore and confirm \`git diff --stat\` matches again. A test that passes on the old code is a rejection for that item.
2. fix_minimal (also: every new case is imported in tests/smoketest.py and called in its phase's lambda, and no hunk outside the scope or tests, such as a removed noqa, appears in the diff): the diff touches only files in scope and tests (\`git -C ${r.worktree} status --short\`); no public name or signature changed; no structure added for a use case that has not arrived; no new guard for a state the annotations make impossible; every comment states an invariant, none narrates the change; ASCII only. For any perf item, the measurement is real and the claimed gain is the measured one, and the output is unchanged.
3. invariants_kept: re-read the CLAUDE.md Key subsystems bullets and CLAUDE-ARCHIVE.md entries naming the changed functions. In particular: a lock taken across a CDP command keeps an unlocked core; reconnect always calls _reattach_core, not reattach_session; everything registered per session is still replayed in _reattach_core; route_to's order (owner, action in flight, task claim, grace tail) is unchanged; LoopOwned state is still mutated on its loop; nothing new blocks the loop; no await was added inside a section that depended on running without yielding.
Also reproduce the worker's checks in the worktree yourself: \`ruff check\`, \`basedpyright src\`, \`python3 checks/narration.py\`, and \`uv run tests/smoketest.py --phases ${g.phases}\`.

accept is true only if all three answers are true. Reject the whole group only for a problem that cannot be separated; otherwise list the offending item id in issues and accept=false so the owner redoes it. Put every concrete problem in issues with a file:line. Summary in three sentences.`
}

function integratePrompt(accepted) {
  const list = accepted.map(a => `- ${a.group}: worktree ${a.worktree}; fixed ${a.fixed.join(', ')}`).join('\n')
  return `You integrate accepted bug-fix diffs onto the master working tree of ${REPO}, one group at a time, in this order:
${list}

For each group:
1. Save the current state: \`git -C ${REPO} diff > /tmp/repld-fix-before.patch\` (empty is fine).
2. Apply the worktree's whole diff, new files included: \`git -C <worktree> add -N . && git -C <worktree> diff | git -C ${REPO} apply --3way\`. The groups register their tests in the same phase entry; if the only conflict is two lines registering different new cases, resolve it by keeping both. Any other conflict drops the group with the error as the reason.
3. Check from ${REPO}: \`${LINT}\` ends with 0 errors (known pre-existing: SIM202 in tests/phases/pure.py, errors under gists/), then \`uv run tests/smoketest.py --phases 1-5\` ends with \`== all phases passed ==\`.
4. On failure: \`git -C ${REPO} checkout -- .\`, re-apply the saved patch if it is not empty, drop the group with the failing check's last 20 lines, and continue.

After the last group lands, run \`uv run tests/smoketest.py --phase 19\` once from ${REPO} (it starts a Chrome and several kernels; run nothing else beside it) and report its last line. If it fails, report which phase and the last 30 lines; do not revert landed groups on your own.

Then add one CHANGELOG.md bullet per landed fix under [Unreleased], in the file's existing voice, user-visible effect first, no item ids. Packaged changes only.

Never run \`git commit\`, \`git stash\`, \`git reset\` or \`git clean\`. Do not edit source by hand; a diff that does not apply is dropped, not repaired. Finish with \`git -C ${REPO} status --short\` and report it under final_checks along with the phase 19 result. Return landed, dropped (group + reason), final_checks.`
}

phase('Fix')
const KEYS = (args && args.groups) || Object.keys(GROUPS)
const chunks = []
for (let i = 0; i < KEYS.length; i += WORKERS) chunks.push(KEYS.slice(i, i + WORKERS))
const judged = []
for (const chunk of chunks) judged.push(...await pipeline(
  chunk,
  key => agent(workerPrompt(key, GROUPS[key]), { label: `fix:${key}`, phase: 'Fix', isolation: 'worktree', schema: RESULT, model: 'opus' }),
  (r, key) => {
    if (!r) return { group: key, fixed: [], verdict: { accept: false, summary: 'worker produced no result' } }
    if (!r.ok) return { ...r, verdict: { accept: false, summary: 'worker reported a failed check: ' + r.summary } }
    return agent(judgePrompt(r, GROUPS[key]), { label: `judge:${key}`, phase: 'Judge', schema: VERDICT, effort: 'high', model: 'opus' })
      .then(v => ({ ...r, verdict: v || { accept: false, summary: 'judge produced no verdict' } }))
  },
))

const accepted = judged.filter(Boolean).filter(j => j.verdict && j.verdict.accept)
const rejected = judged.filter(Boolean).filter(j => !(j.verdict && j.verdict.accept))
log(`${accepted.length} accepted, ${rejected.length} rejected: ${rejected.map(r => r.group).join(', ')}`)

phase('Integrate')
const integration = accepted.length
  ? await agent(integratePrompt(accepted), { label: 'integrate', phase: 'Integrate', schema: INTEGRATION, effort: 'medium', model: 'sonnet' })
  : null

return { accepted, rejected, integration }
