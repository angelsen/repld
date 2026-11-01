export const meta = {
  name: 'readability',
  whenToUse: 'args: {repo, items: [{file, phases, only_cases?, big?, extra?}], workers?}. One module per item; phases is the smoketest --phases set that covers the module (e.g. "1-5,17"); only_cases lists phase-6 --only cases for browser files; big marks a file over 1,500 lines; workers (default 3, use 2 for browser items) is how many rewrite at once.',
  description: 'Rewrite each module to the CLAUDE.md house style in its own worktree, judge each cold against the house style and the restraint rubric, integrate accepted diffs serially onto master (no commits)',
  phases: [
    { title: 'Rewrite', detail: 'one agent per module, own worktree, measure first, no commit' },
    { title: 'Judge', detail: 'cold read of before/after: behaviour identical, invariants kept, house style met' },
    { title: 'Integrate', detail: 'apply accepted whole-worktree diffs onto master one at a time, covering phases after each' },
  ],
}

const REPO = args.repo
const ITEMS = args.items
const WORKERS = args.workers || 3

const RESULT = {
  type: 'object',
  required: ['worktree', 'file', 'ok', 'summary', 'deleted', 'renamed', 'checks'],
  properties: {
    worktree: { type: 'string' },
    file: { type: 'string' },
    ok: { type: 'boolean' },
    measure_before: { type: 'object' },
    measure_after: { type: 'object' },
    deleted: { type: 'array', items: { type: 'string' } },
    renamed: { type: 'array', items: { type: 'string' } },
    kept_for_tests: { type: 'array', items: { type: 'string' } },
    checks: { type: 'object' },
    summary: { type: 'string' },
  },
}

const VERDICT = {
  type: 'object',
  required: ['accept', 'behaviour_identical', 'invariants_kept', 'style_met', 'issues', 'summary'],
  properties: {
    accept: { type: 'boolean' },
    behaviour_identical: { type: 'boolean' },
    invariants_kept: { type: 'boolean' },
    style_met: { type: 'boolean' },
    changed_functions: { type: 'array', items: { type: 'string' } },
    issues: { type: 'array', items: { type: 'string' } },
    summary: { type: 'string' },
  },
}

const INTEGRATION = {
  type: 'object',
  required: ['landed', 'dropped', 'final_checks'],
  properties: {
    landed: { type: 'array', items: { type: 'string' } },
    dropped: { type: 'array', items: { type: 'object', properties: { file: { type: 'string' }, reason: { type: 'string' } } } },
    final_checks: { type: 'object' },
  },
}

// "1-5,17" -> Set; the integrator runs the union of a slice's sets.
function parsePhases(spec) {
  const out = new Set()
  for (const part of String(spec).split(',')) {
    const [lo, hi] = part.trim().split('-').map(Number)
    for (let p = lo; p <= (hi ?? lo); p++) out.add(p)
  }
  return out
}
function formatPhases(set) {
  const ps = [...set].sort((a, b) => a - b)
  const runs = []
  for (const p of ps) {
    const last = runs[runs.length - 1]
    if (last && last[1] === p - 1) last[1] = p
    else runs.push([p, p])
  }
  return runs.map(([a, b]) => (a === b ? `${a}` : `${a}-${b}`)).join(',')
}

const LINT = f => `ruff check --fix ${f} && ruff format ${f} && python3 scripts/align-comments.py --fix-all && basedpyright ${f}`
const DOC_RULE = f => `ruff check --select D101,D102,D103 ${f}`
const NEST_RULE = f => `ruff check --preview --select PLR1702 --config 'lint.pylint.max-nested-blocks=4' ${f}`

function workerPrompt(it) {
  const only = (it.only_cases || []).map(c => `\`uv run tests/smoketest.py --only ${c} --phase 6\``).join(', then ')
  return `You are rewriting ONE module of the repld package to its house style. You are in your own git worktree of ${REPO}; run \`pwd\` first and report it as \`worktree\`. Work only in this worktree, and only in this file.

Module: \`${it.file}\`.${it.big ? ' It is over 1,500 lines: work section by section (one class or one group of related functions at a time) and run the lint command after each section, so a mistake is found near where it was made.' : ''}${it.extra ? '\n\nExtra for this module: ' + it.extra : ''}

The style is CLAUDE.md's "House style" section, ten rules; the judge holds you to every one of them, and to the CLAUDE.md "Key subsystems" bullets and CLAUDE-ARCHIVE.md entries that name this file. Read those bullets before you start.

Start with the brief: \`python3 scripts/style-measure.py ${it.file}\` (and \`--json\` for the report you return as measure_before). It names the missing docstrings, the nesting sites, the helper verbs in use and the em dashes. Then rewrite.

You may:
- Add the missing docstrings and fix ones that restate the name or the signature. Prose, first line the contract, single backticks around code (never double: this package has none). Match the module's existing docstring register and length; a docstring longer than the module's typical one is padding, and the judge reads each one.
- Add a missing return or parameter annotation (rule 1); that is housekeeping, not a signature change.
- Keep or add section banners only in the module's existing form (rule 3); that is the one place a double hyphen is allowed.
- Reorder a body into narrative order (rule 4) and the module into reading order (rule 5).
- Rename a private helper to a house verb (rule 7) and move it to sit after its first caller.
- Delete a guard the annotations make impossible and a try/except around a path that cannot raise (rule 8). List every deletion in \`deleted\` with the reason it was type-impossible.
- Flatten nesting past depth 4 with early returns or \`continue\`; extract a helper only for a unit a reader wants a name for.
- Convert every em dash and other non-ASCII punctuation in the module (code, strings, comments, docstrings): reword with a comma, colon, full stop or parentheses; never \`--\`. Keep the wording otherwise; an invariant comment keeps its meaning exactly.

You may not:
- Change behaviour: same return values, same exceptions and messages, same order of side effects (writes, prints, sends, pushes, event emits). If you see a bug, name it in \`summary\` and leave it.
- Change any public name or signature, or any private name referenced outside this module. Before a rename, \`grep -rn '<name>' src tests CLAUDE.md CLAUDE-ARCHIVE.md docs\`; a hit outside the module means the name stays. A string literal a test asserts on keeps its exact text, em dash included; list those in \`kept_for_tests\`.
- Edit any other file. The whole-worktree diff lands as one; \`git status --short\` must show only this file.
- Add structure: no new class, base class, registry, hook, option or parameter. No helper for a block used once unless it is the unit a reader wants a name for.
- Drop or weaken a comment that states an invariant, a workaround or why something counter-intuitive is correct. It moves with its code and keeps its meaning.
- Add comments that narrate what you did, or run \`git commit\`, \`git stash\`, \`git reset\` or anything else that changes git state.

Checks, all required, in this order; a worktree has no venv, so run \`uv sync\` once first:
- \`${LINT(it.file)}\` ends with 0 errors.
- \`${DOC_RULE(it.file)}\` and \`${NEST_RULE(it.file)}\` report nothing for this file.
- \`python3 scripts/style-measure.py --json ${it.file}\` again, returned as measure_after; em_dashes must be 0 unless kept_for_tests says why.
- \`uv run tests/smoketest.py --phases ${it.phases}\` ends with \`== all phases passed ==\`.${only ? `\n- Then ${only}; each starts its own throwaway Chrome, so run them one after another, never in parallel.` : ''}

Return the JSON for the schema: worktree, file, ok (true only if every check passed), measure_before, measure_after, deleted, renamed (old -> new), kept_for_tests, checks (each command to its outcome's last line), and a summary of at most four sentences saying what the module now reads like and what you were unsure of.`
}

function judgePrompt(r, it) {
  return `You are the independent judge of a readability rewrite you did not write. Be adversarial: your job is to find a reason to reject, and to say what was lost, not only what survived.

Repository: ${REPO} (master, untouched). Worktree with the change: ${r.worktree}. Module: \`${r.file}\`.

Read the whole module twice, old and new, not only the diff: \`git -C ${REPO} show HEAD:${r.file}\` and the worktree's copy. Then narrow the behaviour question with the hashes:
\`python3 scripts/style-measure.py --bodies ${r.file}\` run in ${REPO} and in ${r.worktree}, diffed by name. A name with the same hash changed only in docstring, comments or position; a new name with an old hash is a rename; a changed or new hash is code that changed, and every one of those functions you read in full, old against new. Report that list as changed_functions.

The worker's own report of what it deleted and renamed: deleted ${JSON.stringify(r.deleted || [])}; renamed ${JSON.stringify(r.renamed || [])}; kept for tests ${JSON.stringify(r.kept_for_tests || [])}.

Decide three things:
1. behaviour_identical: for every changed function, the same return value, exception type and message, and side effects in the same order. Look especially at: an early return that skips a cleanup, a log line, a push or an emit the original always reached; a helper that captures a variable by value where the original read it later after mutation; a loop whose break or continue moved; an exception handler whose scope widened or narrowed; a deleted guard that was not type-impossible (a value that can be None at runtime despite the annotation, a dict key, a subprocess result, anything from the wire); a deleted try/except around a path that can raise. Each deletion in the worker's list must be type-impossible by the annotations and the callers you can see; otherwise reject.
2. invariants_kept: every comment in the original that states a constraint, a workaround or a non-obvious reason still exists with the same meaning, moved with its code; no guard documented by such a comment was removed or reordered. Check the CLAUDE.md "Key subsystems" bullets and the CLAUDE-ARCHIVE.md entries that name this file for rules the code must still honour, and that the names those bullets use still exist.
3. style_met, against CLAUDE.md "House style" and the restraint rubric: measure first, then match. Compare \`python3 scripts/style-measure.py ${r.file}\` in both trees. Reject if: \`#\` comment density moved outside half to double the module's own baseline (docstring density is expected to rise where docstrings were missing; judge each docstring, not the count); a new docstring restates the signature or the name instead of stating the contract, or is padded past the module's typical docstring length; double backticks or a \`--\` outside a section banner appear; a body cannot be narrated by a reader without opening its helpers (read-aloud test: state in one sentence what each changed function does from its body alone; if you cannot, say which); a helper was added for a block with no name a reader wants; a nesting site at depth 4 or more remains; an em dash or other non-ASCII punctuation remains outside kept_for_tests; a private name that is referenced outside the module was renamed (grep src tests CLAUDE.md CLAUDE-ARCHIVE.md docs yourself); a public name or signature changed; structure was added; a comment narrates the change.

Also reject if \`git -C ${r.worktree} status --short\` shows any file other than ${r.file}.

Confirm the lint with your own runs in the worktree, not the worker's: \`ruff check ${r.file}\`, \`${DOC_RULE(r.file)}\`, \`${NEST_RULE(r.file)}\`, \`basedpyright ${r.file}\`.

accept is true only if all three answers are true and no rejection condition holds. Put every concrete problem in issues with a file:line. Summary in three sentences: what the module reads like now, what was lost if anything, and the one thing a second pass should fix.`
}

function integratePrompt(accepted, phases) {
  const list = accepted.map(a => `- ${a.file}: worktree ${a.worktree}`).join('\n')
  return `You integrate accepted readability rewrites onto the master working tree of ${REPO}, one at a time, in this order:
${list}

For each item:
1. Save the current state: \`git -C ${REPO} diff > /tmp/repld-integrate-before.patch\` (an empty file is fine).
2. Apply the worktree's whole diff: \`git -C <worktree> diff | git -C ${REPO} apply --3way\`. If apply fails, drop the item with the apply error as the reason and continue.
3. Check, from ${REPO}: \`ruff check --fix && ruff format . gists && python3 scripts/align-comments.py --fix-all && basedpyright src\` must end with 0 errors (a pre-existing SIM202 in tests/phases/pure.py and errors under gists/ are known and not yours), then \`uv run tests/smoketest.py --phases ${phases}\` must end with \`== all phases passed ==\`.
4. On any failure: restore with \`git -C ${REPO} checkout -- . && git -C ${REPO} apply --allow-empty /tmp/repld-integrate-before.patch\` (skip the apply if the saved patch is empty), drop the item with the failing check's last 20 lines as the reason, and continue.

Never run \`git commit\`, \`git stash\`, \`git reset\` or \`git clean\`. Do not edit any file by hand; if a diff does not apply cleanly it is dropped, not repaired. Do not run phase 6 or any --only case.

Finish with \`git -C ${REPO} status --short\` and \`ruff check ${REPO}/src\` and report both under final_checks. Return landed (files in order), dropped (file + reason), final_checks.`
}

phase('Rewrite')
// WORKERS at a time: each phase run spawns kernels, each --only case a Chrome, and the machine has 14 GB.
const chunks = []
for (let i = 0; i < ITEMS.length; i += WORKERS) chunks.push(ITEMS.slice(i, i + WORKERS))
const judged = []
for (const chunk of chunks) judged.push(...await pipeline(
  chunk,
  it => agent(workerPrompt(it), { label: `rewrite:${it.file}`, phase: 'Rewrite', isolation: 'worktree', schema: RESULT, model: 'opus' }),
  (r, it) => {
    if (!r) return { file: it.file, verdict: { accept: false, summary: 'worker produced no result' } }
    if (!r.ok) return { ...r, verdict: { accept: false, summary: 'worker reported a failed check: ' + r.summary } }
    return agent(judgePrompt(r, it), { label: `judge:${it.file}`, phase: 'Judge', schema: VERDICT, effort: 'high', model: 'opus' })
      .then(v => ({ ...r, verdict: v || { accept: false, summary: 'judge produced no verdict' } }))
  },
))

const accepted = judged.filter(Boolean).filter(j => j.verdict && j.verdict.accept)
const rejected = judged.filter(Boolean).filter(j => !(j.verdict && j.verdict.accept))
log(`${accepted.length} accepted, ${rejected.length} rejected`)

phase('Integrate')
const union = new Set()
for (const it of ITEMS) for (const p of parsePhases(it.phases)) union.add(p)
const integration = accepted.length
  ? await agent(integratePrompt(accepted, formatPhases(union)), { label: 'integrate', phase: 'Integrate', schema: INTEGRATION, effort: 'medium', model: 'sonnet' })
  : null

return { accepted, rejected, integration }
