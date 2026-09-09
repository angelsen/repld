#!/usr/bin/env python3
"""Ratchet on investigation narrative in code comments and the markdown.

    python3 checks/narration.py              # check against the baseline
    python3 checks/narration.py --file F     # the shortlist for one file
    python3 checks/narration.py --update     # re-record the baseline

Ported from slipp2's smoketest/narration.py -- the signatures are that
repo's, measured against real false positives, and should be retuned the
same way: by classifying a file's flagged blocks, never by reading the
class names. A ratchet, not a gate: a file may carry its recorded count
and may not carry more; cleaning a file lowers its number for good.

WHAT IT LOOKS FOR: prose describing THIS repo's own history -- a date, a
prior state, how something was discovered, a pointer at `git log`. Not
length, and not an external dated constraint ("deprecated in Zod 4").

narration: ok -- naming the signature classes is what this file is for.
"""

import argparse
import ast
import io
import json
import pathlib
import re
import subprocess
import sys
import tokenize

# ---- repo config ---------------------------------------------------------

ROOT = pathlib.Path(__file__).resolve().parent.parent
BASELINE = pathlib.Path(__file__).resolve().parent / "narration-baseline.json"
CODE_GLOBS = ["src/**/*.py"]
CODE_EXCLUDE: tuple[str, ...] = ()
# CHANGELOG.md is a dated-evidence home by construction and isn't globbed
# in below (only CLAUDE.md + docs/*.md are); site/src/content/docs is the
# published site source, not this repo's own design reference.
MD_EXCLUDE: set[str] = set()


def markdown_files() -> list[pathlib.Path]:
    fixed = [ROOT / "CLAUDE.md"]
    docs = sorted((ROOT / "docs").glob("*.md"))
    return [
        p
        for p in fixed + docs
        if p.exists() and str(p.relative_to(ROOT)) not in MD_EXCLUDE
    ]


# ---- signatures (slipp2's, verbatim) -------------------------------------

EXEMPT = "narration: ok"

SIGNATURES = {
    "dated": r"\b20\d\d-\d\d-\d\d\b",
    "prior state": r"\bused to\b|\bthere was an?\b|\bthis was\b|\bwas DELETED\b"
    r"|\bbefore this\b|\bpreviously\b|\bformerly\b|\boriginally\b",
    "discovery": r"\bturned out\b|\bwe tried\b|\bafter trying\b|\bfound by\b"
    r"|\bverified (?:live|by|that)\b|\bno offline test\b",
    "history pointer": r"\bgit log\b|\bgit blame\b|\bsee the commit\b",
}

# Held apart from SIGNATURES rather than intersected with it: every
# difference is a measured markdown false positive (see slipp2's original).
MD_SIGNATURES = {
    "dated": r"\b(?:verified|until)\s+20\d\d-\d\d-\d\d\b",
    "prior state": r"\bthis was the\b|\bnow gone\b|\bdeleted with\b"
    r"|\bbefore this\b|\bpreviously\b|\bformerly\b|\boriginally\b",
    "discovery": r"\bturned out\b|\bwe tried\b|\bafter trying\b|\bmeasured:"
    r"|\bverified (?:live|by|that)\b|\bno offline test\b",
    "history pointer": r"\bgit log\b|\bgit blame\b|\bsee the commit\b",
}


def _matcher(sigs: dict[str, str]) -> re.Pattern[str]:
    return re.compile(
        "|".join(f"(?P<{k.replace(' ', '_')}>{v})" for k, v in sigs.items()),
        re.IGNORECASE,
    )


_MATCH = _matcher(SIGNATURES)
_MD_MATCH = _matcher(MD_SIGNATURES)


# ---- extraction ----------------------------------------------------------


def sources() -> list[pathlib.Path]:
    out = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", *CODE_GLOBS],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\n")
    code = [
        ROOT / p
        for p in out
        if p.strip()
        and not any(x in p for x in CODE_EXCLUDE)
        and not p.endswith(".d.ts")
    ]
    return code + markdown_files()


def py_blocks(src: str):
    """Every comment and docstring, as (start_line, line_count)."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return
    holds = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if isinstance(node, holds) and ast.get_docstring(node, clean=False) is not None:
            e = node.body[0]
            assert (
                e.end_lineno is not None
            )  # real parsed source, never a synthetic node
            yield e.lineno, e.end_lineno - e.lineno + 1

    # tokenize, not a line scan: a scan counts `#` inside string literals.
    try:
        lines = src.split("\n")
        own = [
            t.start[0]
            for t in tokenize.generate_tokens(io.StringIO(src).readline)
            if t.type == tokenize.COMMENT
            and not lines[t.start[0] - 1][: t.start[1]].strip()
        ]
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return
    yield from _group(own)


def js_blocks(src: str):
    """Comment-only lines (//, /* */, <!-- -->) grouped into blocks.

    Trailing same-line comments after code are skipped on purpose --
    precision over recall, and a line scan cannot tell them from strings.
    """
    own: list[int] = []
    in_block: str | None = None
    for i, raw in enumerate(src.split("\n"), 1):
        s = raw.strip()
        if in_block:
            own.append(i)
            if ("*/" if in_block == "/*" else "-->") in s:
                in_block = None
        elif s.startswith("//"):
            own.append(i)
        elif s.startswith("/*"):
            own.append(i)
            if "*/" not in s:
                in_block = "/*"
        elif s.startswith("<!--"):
            own.append(i)
            if "-->" not in s:
                in_block = "<!--"
    yield from _group(own)


def _group(rows: list[int]):
    # start and prev are always both None or both set -- real to the algorithm,
    # invisible to the checker, so the None checks stay paired at each use.
    start: int | None = None
    prev: int | None = None
    for row in rows:
        if start is None or prev is None or row != prev + 1:
            if start is not None and prev is not None:
                yield start, prev - start + 1
            start = row
        prev = row
    if start is not None and prev is not None:
        yield start, prev - start + 1


_FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)
_MD_SPLIT = re.compile(r"\n\s*\n|\n(?=\s*(?:[-*+]\s|\d+\.\s))")


def markdown_extents(path: pathlib.Path) -> list[tuple[int, int, str]]:
    """Every prose paragraph and list item, as (line, line_count, text).
    Fenced code blocks are blanked, not deleted, keeping line numbers honest."""
    text = path.read_text()
    text = _FENCE.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    units, offset = [], 0
    for para in _MD_SPLIT.split(text):
        start = text.index(para, offset)
        offset = start + len(para)
        if para.strip():
            units.append(
                (
                    text[:start].count("\n") + 1,
                    para.count("\n") + 1,
                    " ".join(para.split()),
                )
            )
    return units


def units(path: pathlib.Path):
    """(start_line, line_count, text) for every prose unit in `path`."""
    if path.suffix == ".md":
        yield from markdown_extents(path)
        return
    src = path.read_text(encoding="utf-8", errors="replace")
    lines = src.split("\n")
    blocks = py_blocks if path.suffix == ".py" else js_blocks
    for line, count in blocks(src):
        yield line, count, "\n".join(lines[line - 1 : line - 1 + count])


def hits(path: pathlib.Path):
    """(line, count, [signature names]) for each unit carrying narration."""
    match = _MD_MATCH if path.suffix == ".md" else _MATCH
    for line, count, text in units(path):
        if EXEMPT in text:
            continue
        names = {
            mg.replace("_", " ") for m in match.finditer(text) if (mg := m.lastgroup)
        }
        if names:
            yield line, count, sorted(names)


def counts() -> dict[str, int]:
    return {
        str(p.relative_to(ROOT)): n for p in sources() if (n := sum(1 for _ in hits(p)))
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file")
    ap.add_argument("--update", action="store_true")
    ap.add_argument(
        "--blocks",
        type=int,
        metavar="N",
        help="with --file: every comment or docstring of N lines or more",
    )
    args = ap.parse_args()

    if args.file:
        p = (ROOT / args.file).resolve()
        if args.blocks:
            found = sorted((n, ln) for ln, n, _ in units(p) if n >= args.blocks)
            for count, line in sorted(found, key=lambda b: b[1]):
                print(f"L{line} ({count} lines)")
            if not found:
                print(f"no block of {args.blocks}+ lines in {args.file}")
            return 0
        found = list(hits(p))
        for line, count, names in found:
            print(f"L{line} ({count} lines) -- {', '.join(names)}")
        if not found:
            print(f"no narration signatures in {args.file}")
        return 0

    now = counts()
    if args.update:
        BASELINE.write_text(json.dumps(dict(sorted(now.items())), indent=2) + "\n")
        print(f"baseline recorded: {sum(now.values())} hits across {len(now)} files")
        return 0

    base = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}
    over = {f: (n, base.get(f, 0)) for f, n in now.items() if n > base.get(f, 0)}
    total, btotal = sum(now.values()), sum(base.values())

    if over:
        print(
            f"NARRATION: {len(over)} file(s) above baseline ({total} hits, baseline {btotal})\n"
        )
        for f, (n, b) in sorted(over.items()):
            print(f"{f}  {b} -> {n}")
            for line, count, names in hits(ROOT / f):
                print(f"    L{line} ({count} lines) -- {', '.join(names)}")
        print("\nState the invariant and let `git log` hold how it was found.")
        print(f"Deliberate? Mark the block '{EXEMPT}' or re-record with --update.")
        return 1

    gained = btotal - total
    print(
        f"NARRATION: {total} hits across {len(now)} files, none above baseline"
        + (f" ({gained} fewer than recorded)" if gained > 0 else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
