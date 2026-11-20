---
name: claims-align
description: Review a repld diff's prose against the code, every changed sentence in CLAUDE.md, CHANGELOG.md, help.py, the docs, docstrings and comments checked for what it claims, how far it claims it and what it leaves unsaid, plus the House style rules on the added lines. Run headless by supervise's verdict.align (dispatch.align in .claude/devstack.json); --since <ref> reviews everything since that ref.
disable-model-invocation: true
---

# Claims alignment review

repld's docs are read by agents as the contract: `CLAUDE.md` for the
invariants, `src/repld/help.py` and the MCP instructions for the API,
`CHANGELOG.md` for what a release changed. A sentence is right when the
code does what it says, no more and no less. This review checks the
diff's sentences; it does not trace interleavings (that is `trace-align`),
it takes the code at HEAD as given.

## Inputs

1. `--since <ref>`: `git diff <ref> --name-only` for the files (committed
   and uncommitted alike) and `git diff <ref>` for the full diff. With no
   ref, the staged diff, falling back to the unstaged one.
2. Every added or rewritten sentence is in scope: markdown, docstrings,
   comments, error and help strings. Read the whole paragraph or function
   around each, and the code it describes.
3. Read `CLAUDE.md`'s "House style" section; its ten rules are check 5.

## Checks

1. **Each claim is checked, not just found.** For every sentence that
   names a function, parameter, flag, file, phase or condition, open the
   code and confirm the claim itself: that condition selects that branch,
   that default is that value, that caller passes that argument. A
   sentence about another repo (supervise's `verdict`, Claude Code) is
   checked in that repo under `~/Projects` when it is there, and reported
   as unverifiable when it is not.
2. **A claim is as wide as the code, no wider.** Compare the sentence's
   condition with the code's: "refuses a repeat" against the exact test
   that refuses, "on every path" against each path. Absolutes ("never",
   "always", "only", "for good") and closed counts ("one ordering",
   "three paths", "exactly five callers") are verified by enumerating
   with grep, or reported. A count is recounted.
3. **What the diff changed is said where its reader looks.** A behaviour
   change under `src/repld/` has a note under `CHANGELOG.md`'s
   `[Unreleased]` (packaged changes only: `gists/`, `tests/` and
   `.claude/` are not in the wheel). A call whose shape changed is
   updated wherever the old shape is shown: grep `src/repld/help.py`,
   `README.md`, `docs/` and `site/src/content/docs/` for it. A case added
   to a phase is named in that phase's line under `CLAUDE.md`'s
   "Testing". A condition or outcome the diff adds that no changed
   sentence states is an omission.
4. **The past is told where it belongs.** A comment, docstring or
   `CLAUDE.md` sentence that says what the code used to do, what was
   tried or when it changed is narration; `python3 checks/narration.py
   --file <path>` lists a file's candidates. `CHANGELOG.md` and the
   commit message are where it goes. A sentence describing the failure
   a change fixes is still a claim about code and is checked against
   the base (`git show <ref>:<path>`).
5. **House style on the added lines.** The ten rules as `CLAUDE.md`
   states them, on what the diff adds: typed signatures, a contract-first
   docstring on every function, comments that state an invariant or a
   why and name the symbols they mean (a comment pointing at an order or
   a branch the code no longer has is a finding), a helper after its
   first caller, house verbs, no guard for an impossible state, ASCII
   only.
6. **Generated and operating files.** `src/repld/browser/injected_source.py`
   changes only together with `scripts/build_injected.py`. A changed
   `.claude/skills/*/SKILL.md` or `.claude/devstack.json` is read as a
   doc under checks 1 and 2: each command it names exists as written.

## Severity

- **blocking**: a sentence that is false about what the code at HEAD
  does; a wrong count; a hand-edited generated file; a doc that still
  shows a call the diff removed or made wrong.
- **should-fix**: a true claim wider or narrower than the code; an
  absolute or closed count nothing enumerates; an omission under check
  3; a false sentence about the base or the past; narration; a House
  style breach.
- **note**: anything else worth a human's eye, the claims that checked
  out included, one line each.

Only blocking gates.

## Output

Findings grouped by severity, each quoting the sentence with its
`path:/literal/` anchor (a substring of the line, never a line number)
and giving the evidence: the code line it was checked against, the grep,
the recount. End with exactly one sentinel line:

    CLAIMS_ALIGN: PASS

or

    CLAIMS_ALIGN: FAIL (<n> blocking)
