export const meta = {
  name: 'browser-audit',
  whenToUse: 'args: {lenses?: string[]} optional subset of lens keys (ownership, transport, longrun, loop, pages). Read-only: reports ranked suspects, changes nothing.',
  description: 'Audit the browser object from the shared, remote, long-lived (Prelo/lend) angle for bugs and performance, one finder and one cold verifier per lens, then one ranked synthesis',
  phases: [
    { title: 'Find', detail: 'one finder per lens, read-only, cites file:line' },
    { title: 'Verify', detail: 'one cold skeptic per lens tries to refute every finding' },
    { title: 'Synthesize', detail: 'dedupe and rank survivors, name a test for each' },
  ],
}

const FINDINGS = {
  type: 'object',
  required: ['findings'],
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        required: ['id', 'kind', 'where', 'claim', 'scenario', 'severity', 'test'],
        properties: {
          id: { type: 'string' },
          kind: { type: 'string', enum: ['bug', 'perf', 'cleanup'] },
          where: { type: 'string' },
          claim: { type: 'string' },
          scenario: { type: 'string' },
          severity: { type: 'string', enum: ['high', 'medium', 'low'] },
          test: { type: 'string' },
        },
      },
    },
  },
}

const VERDICTS = {
  type: 'object',
  required: ['verdicts'],
  properties: {
    verdicts: {
      type: 'array',
      items: {
        type: 'object',
        required: ['id', 'refuted', 'evidence'],
        properties: {
          id: { type: 'string' },
          refuted: { type: 'boolean' },
          evidence: { type: 'string' },
          corrected_where: { type: 'string' },
        },
      },
    },
  },
}

const REPORT = {
  type: 'object',
  required: ['ranked', 'dropped', 'summary'],
  properties: {
    ranked: {
      type: 'array',
      items: {
        type: 'object',
        required: ['id', 'kind', 'where', 'claim', 'severity', 'test'],
        properties: {
          id: { type: 'string' },
          kind: { type: 'string' },
          where: { type: 'string' },
          claim: { type: 'string' },
          severity: { type: 'string' },
          test: { type: 'string' },
        },
      },
    },
    dropped: { type: 'array', items: { type: 'string' } },
    summary: { type: 'string' },
  },
}

const CONTEXT = `Context: repld's browser object (src/repld/browser/) is meant to be lent to other parties and agents (lend broker, Prelo): a remote \`url=\` browser over an unreliable websocket, several Claude sessions or parties sharing one browser, a human in the same tab, days of uptime, and every party's actions on one asyncio loop. The existing suite drives one local headless Chrome, so it barely covers that. CLAUDE.md's browser invariants are in your context: treat each as a rule the code must keep, and look for paths that break one.

You are READ-ONLY. Do not edit, commit, restart the kernel or run tests/smoketest.py. You may read files and run small throwaway scripts in your scratchpad. Report only what you can point at in the code: file:line, a concrete scenario (inputs and state, then wrong outcome), and the test that would pin it. A suspicion you cannot place is not a finding. At most 6 findings, most severe first. kind=perf needs a stated cost (round trips, loop hold time, memory growth), not "could be faster".`

const LENSES = {
  ownership: {
    files: 'browser/cdp.py (route_to, set_mode, owner/last_caller, action()), browser/pool.py (release_owned_by), browser/tab.py (take/release, _ensure_front), kernel.py _release_taken_tabs, ipc.py on_session_closed',
    brief: `Ownership and routing with several sessions on one browser. Pushes to the wrong party or dropped; a taken tab or file-chooser intercept outliving its holder; claim state surviving what it should not. Known suspects to confirm or kill first: (1) CDPSession.set_mode only reassigns owner when tasks.current_origin() is non-None, so a take() from a ticker or peer call leaves a previous live session as owner of a taken tab; (2) release_owned_by wraps set_mode in a try/except that cannot raise because intercept_file_chooser swallows. Then look for more.`,
  },
  transport: {
    files: 'browser/session.py, browser/browser.py, browser/pool.py (connect, redial, _note_redial_failed, _reconnect, _stranded, _reattach_core), browser/cdp.py (reattach_session, enable_fetch)',
    brief: `An unreliable remote websocket. Reconnect and redial paths: an action executed twice or lost across a reconnect, per-session registrations not replayed (addBinding, addScriptToEvaluateOnNewDocument, Fetch.enable, screencast), history lost or stranded tabs leaking, the retire rules (4410, 401/403/404/410, URL_UNREACHABLE_RETIRE_S), a lock held across a CDP command with no unlocked core, pending futures never resolved when the socket dies. Also is_scope_refusal in session.py: it substring-matches str(exc) on any exception type and is read at tab.py (_ensure_front); decide whether page-controlled text can trip it.`,
  },
  longrun: {
    files: 'browser/cdp.py (event store, _async_prune, _inflight, _db_lock, cleanup), browser/capture.py, browser/screencast.py, bg.py, tasks.py ticker and claim state, browser/pin.py',
    brief: `Days of uptime. Unbounded growth (DuckDB size and prune cadence, _inflight, _stranded, screencast frames, per-tab dicts keyed by ids that never get removed, bodies captured and never evicted), leaked tasks or tickers after a tab or browser goes away, handles kept after navigation (engine objectId, aria-refs), LoopOwned violations, DuckDB close racing a live cursor. Give every growth claim a stated bound or lack of one.`,
  },
  loop: {
    files: 'browser/cdp.py (pre_observe, post_observe, _handle_event), browser/tab_query.py, browser/tab.py (click/type path, _heartbeat_loop, drag), browser/inject.py, browser/capture.py, dashboard.py browser RPCs',
    brief: `Time spent ON the shared asyncio loop, and round trips. Anything on the loop that scales with page size, event count or number of tabs: a view-based query on the loop (pre_observe must read MAX(rowid) FROM events, never the views), synchronous JSON or image work (screenshots, AX trees, HAR assembly, PIL), per-event work in _handle_event, serial awaits that could be sent together (Chrome answers a session in order), N-tab fan-outs that run serially. Estimate the cost with N parties or 50 tabs. Keep phase_6_click_round_trips (six awaited hops) as the baseline and do not propose removing a dependency CLAUDE.md names as real.`,
  },
  pages: {
    files: 'browser/inject.py, browser/selector.py, browser/captcha.py, browser/tab.py (type, select, hover, set_files, dialogs), browser/cdp.py (dialog and auth handling), browser/target.py',
    brief: `Real vendor pages: iframes (same-process and out-of-process), shadow DOM, window.open popups, native dialogs, HTTP auth, file pickers, SPA navigation replacing the document mid-action, virtualized lists. Stale handles after a document swap, an action landing on the wrong frame or an occluded element, a wait that never ends or a timeout that hides the real error (RendererBusyError vs TimeoutError), type receipts claiming success when the value did not land. Name which real-world page shape triggers each finding.`,
  },
}

const KEYS = (args && args.lenses) || Object.keys(LENSES)
log(`browser-audit lenses: ${KEYS.join(', ')}`)

const results = await pipeline(
  KEYS,
  key =>
    agent(
      `${CONTEXT}\n\nLens: ${key}. Start from: ${LENSES[key].files}. Follow calls out of those files as needed.\n\n${LENSES[key].brief}`,
      { label: `find:${key}`, phase: 'Find', model: 'opus', schema: FINDINGS },
    ),
  (found, key) => {
    if (!found || !found.findings.length) {
      log(`${key}: no findings`)
      return { key, findings: [], verdicts: [] }
    }
    return agent(
      `You are a skeptic. A finder produced the findings below for the "${key}" lens of repld's browser object. Try to REFUTE each one by reading the code yourself: find the guard, the caller invariant, the test, or the ordering that makes the scenario impossible, or show the cost claim is wrong. Do not trust the finder's file:line; re-open it. Default to refuted=true when you cannot reproduce the reasoning from the code. If a finding is real but its location is wrong, keep it and give corrected_where. Read-only: no edits, no smoketest.\n\n${JSON.stringify(found.findings, null, 2)}`,
      { label: `verify:${key}`, phase: 'Verify', model: 'opus', schema: VERDICTS },
    ).then(v => ({ key, findings: found.findings, verdicts: (v && v.verdicts) || [] }))
  },
)

const survivors = []
const refuted = []
for (const r of results.filter(Boolean)) {
  for (const f of r.findings) {
    const v = r.verdicts.find(x => x.id === f.id)
    if (!v) log(`${r.key}/${f.id}: no verdict, kept as unverified`)
    if (v && v.refuted) refuted.push(`${r.key}/${f.id}: ${v.evidence}`)
    else survivors.push({ ...f, lens: r.key, unverified: !v, where: (v && v.corrected_where) || f.where, evidence: v && v.evidence })
  }
}
log(`${survivors.length} survived, ${refuted.length} refuted`)

phase('Synthesize')
const report = await agent(
  `Rank these verified findings on repld's browser object, most worth fixing first, for a user whose goal is reasonable, performant code that holds up when the browser is lent to other parties (Prelo). Merge duplicates across lenses. Prefer correctness bugs a real shared or remote browser would hit over speculative perf. Keep each finding's id, where, claim and test; add nothing you cannot see in the input. In summary, give the three to fix first and one sentence why. List refuted or merged ids in dropped.\n\nSurvivors:\n${JSON.stringify(survivors, null, 2)}\n\nRefuted (for dedupe only):\n${refuted.join('\n')}`,
  { label: 'synthesize', phase: 'Synthesize', model: 'opus', schema: REPORT },
)

return { report, unverified: survivors.filter(s => s.unverified).map(s => `${s.lens}/${s.id}`) }
