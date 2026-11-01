export const meta = {
  name: 'burndown-complexity',
  whenToUse: 'args: {repo, items: [{file, function, line, self_check, covered_by, extra?}]}. One function per item; self_check lists the commands a worker runs (phase 5, and --only phase 6 cases for browser files); two workers at a time.',
  description: 'Refactor noqa-grandfathered functions in isolated worktrees, judge each diff cold, integrate accepted diffs serially onto master (no commits)',
  phases: [
    { title: 'Refactor', detail: 'one agent per function, own worktree, no commit' },
    { title: 'Judge', detail: 'cold read of each diff: behaviour identical, invariants kept' },
    { title: 'Integrate', detail: 'apply accepted diffs onto master one at a time, fast phases after each' },
  ],
}

const REPO = args.repo
const ITEMS = args.items

const RESULT = {
  type: 'object',
  required: ['worktree', 'file', 'function', 'ok', 'summary'],
  properties: {
    worktree: { type: 'string' },
    file: { type: 'string' },
    function: { type: 'string' },
    ok: { type: 'boolean' },
    lines_before: { type: 'integer' },
    lines_after: { type: 'integer' },
    helpers_added: { type: 'array', items: { type: 'string' } },
    checks: { type: 'object' },
    summary: { type: 'string' },
  },
}

const VERDICT = {
  type: 'object',
  required: ['accept', 'behaviour_identical', 'invariants_kept', 'issues', 'summary'],
  properties: {
    accept: { type: 'boolean' },
    behaviour_identical: { type: 'boolean' },
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
    dropped: { type: 'array', items: { type: 'object', properties: { function: { type: 'string' }, reason: { type: 'string' } } } },
    final_checks: { type: 'object' },
  },
}

function workerPrompt(it) {
  return `You are refactoring ONE function in the repld repository for readability. You are in your own git worktree of ${REPO}; run \`pwd\` first and report it as \`worktree\`. Work only in this worktree.

Target: \`${it.function}\` in \`${it.file}\` (def near line ${it.line}). Its \`def\` line carries a \`# noqa: C901, ...\` marker that grandfathers it past the complexity ratchet in pyproject.toml (C901 complexity 15, PLR0912 branches 15, PLR0915 statements 60).

Goal: remove that noqa marker and make \`ruff check ${it.file}\` pass, with behaviour identical.${it.extra ? '\n\nExtra for this item: ' + it.extra : ''}

Rules, all hard:
1. Behaviour identical: same return values, same exceptions and messages, same order of side effects (writes, prints, sends, pushes). Do not "fix" anything you notice; if you see a bug, mention it in \`summary\` and leave it.
2. Change nothing outside this function except to add private helpers (module-level \`_name\` functions or \`_name\` methods) placed immediately before it. No renames of public names, no signature changes, the docstring stays.
3. Preferred shapes, in order: early returns to flatten nesting; extracting a cohesive block into a named helper whose name says what it decides or produces; a dispatch table only when the branches are a flat switch on one value. Do not extract for its own sake: a helper must be a unit a reader would want a name for.
4. Comments: every existing comment that states an invariant, a workaround, or why something counter-intuitive is correct must survive verbatim, moved with the code it describes. Add no comments that narrate what you did. The project's CLAUDE.md and CLAUDE-ARCHIVE.md name the invariants for this module; read the CLAUDE.md bullets that mention this file before you start.
5. No em dashes anywhere; ASCII only in code and strings.
6. Do NOT run \`git commit\`, \`git stash\` or anything that changes git state. Leave the change in the working tree.

Checks, all required, in this order; a worktree has no venv, so run \`uv sync\` once first:
- \`ruff check --fix ${it.file} && ruff format ${it.file} && python3 scripts/align-comments.py --fix-all && basedpyright ${it.file}\`
- \`ruff check ${it.file}\` must report no C901/PLR0912/PLR0915 for this function with the noqa gone (the ratchet is on in pyproject.toml).
- Each command in this list must succeed: ${it.self_check}. A \`--only <case> --phase 6\` run starts its own throwaway Chrome and runs just that case; run them one after another, never in parallel, and run nothing else from phase 6. Further coverage (${it.covered_by}) runs after integration, not here.

Return the JSON for the schema: worktree path, file, function, ok (true only if every check passed), lines_before and lines_after for the function body, helpers_added (names), checks (a map of each check to its outcome text), and a summary of the shape you chose in three sentences at most.`
}

function judgePrompt(r) {
  return `You are the independent judge of a refactor you did not write. Be adversarial: your job is to find a reason to reject.

Repository: ${REPO} (master, untouched). Worktree with the change: ${r.worktree}. Function: \`${r.function}\` in \`${r.file}\`.

Get the diff with \`git -C ${r.worktree} diff -- ${r.file}\`. Read the original function on master (\`git -C ${REPO} show HEAD:${r.file}\`) and the new one in the worktree in full, not only the hunks.

Decide two things and report both:
1. behaviour_identical: for every input path, the same return value, the same exception type and message, the same side effects in the same order. Look especially at: an early return that skips a cleanup or a log line the original always reached; a helper that captures a variable by value where the original read it later after mutation; a loop whose break/continue changed; exception handlers whose scope widened or narrowed; a default argument evaluated once that used to be evaluated per call.
2. invariants_kept: every comment in the original that states a constraint, a workaround or a non-obvious reason still exists, verbatim or moved with its code; no guard documented by such a comment was removed or reordered. Check CLAUDE.md's bullets for this file and the matching CLAUDE-ARCHIVE.md entry for rules the code must still honour.

Also reject if: the noqa marker is still present; any public name or signature changed; code outside this function and its new helpers changed; a helper's name does not say what it does; em dashes were introduced.

Confirm the claims with your own runs, not the worker's: \`ruff check ${r.file}\` in the worktree, and \`basedpyright ${r.file}\`.

accept is true only if both answers are true and no rejection condition holds. Put every concrete problem in issues with a file:line. Summary in two sentences.`
}

function integratePrompt(accepted) {
  const list = accepted.map(a => `- ${a.function} in ${a.file}: worktree ${a.worktree}`).join('\n')
  return `You integrate accepted refactor diffs onto the master working tree of ${REPO}, one at a time, in this order:
${list}

For each item:
1. Save the current state: \`git -C ${REPO} diff > /tmp/repld-integrate-before.patch\` (an empty file is fine).
2. Apply the worktree's whole diff, tests included: \`git -C <worktree> diff | git -C ${REPO} apply --3way\`. If apply fails, drop the item with the apply error as the reason and continue.
3. Check, from ${REPO}: \`ruff check --fix && ruff format . gists && python3 scripts/align-comments.py --fix-all && basedpyright src\` must end with 0 errors (a pre-existing SIM202 in tests/phases/pure.py and errors under gists/ are known and not yours), then \`uv run tests/smoketest.py --phase 5\` must end with \`== all phases passed ==\`.
4. On any failure: restore with \`git -C ${REPO} checkout -- . && git -C ${REPO} apply --allow-empty /tmp/repld-integrate-before.patch\` (skip the apply if the saved patch is empty), drop the item with the failing check's last 20 lines as the reason, and continue.

Never run \`git commit\`, \`git stash\`, \`git reset\` or \`git clean\`. Do not edit any file by hand; if a diff does not apply cleanly it is dropped, not repaired. Do not run phase 6 or above.

Finish with \`git -C ${REPO} status --short\` and \`ruff check ${REPO}/src\` and report both under final_checks. Return landed (function names in order), dropped (function + reason), final_checks.`
}

phase('Refactor')
// Two workers at a time: each phase 6 case starts a Chrome and the machine has 14 GB.
const chunks = []
for (let i = 0; i < ITEMS.length; i += 2) chunks.push(ITEMS.slice(i, i + 2))
const judged = []
for (const chunk of chunks) judged.push(...await pipeline(
  chunk,
  it => agent(workerPrompt(it), { label: `refactor:${it.function}`, phase: 'Refactor', isolation: 'worktree', schema: RESULT, model: 'opus' }),
  (r, it) => {
    if (!r) return { file: it.file, function: it.function, verdict: { accept: false, summary: 'worker produced no result' } }
    if (!r.ok) return { ...r, verdict: { accept: false, summary: 'worker reported a failed check: ' + r.summary } }
    return agent(judgePrompt(r), { label: `judge:${it.function}`, phase: 'Judge', schema: VERDICT, effort: 'high', model: 'opus' })
      .then(v => ({ ...r, verdict: v || { accept: false, summary: 'judge produced no verdict' } }))
  },
))

const accepted = judged.filter(Boolean).filter(j => j.verdict && j.verdict.accept)
const rejected = judged.filter(Boolean).filter(j => !(j.verdict && j.verdict.accept))
log(`${accepted.length} accepted, ${rejected.length} rejected`)

phase('Integrate')
const integration = accepted.length
  ? await agent(integratePrompt(accepted), { label: 'integrate', phase: 'Integrate', schema: INTEGRATION, effort: 'medium', model: 'sonnet' })
  : null

return { accepted, rejected, integration }