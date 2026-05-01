#!/usr/bin/env python3
"""Align trailing # comments to the same column within consecutive groups.

Usage:
    align-comments < file.py             # stdout
    align-comments file.py               # stdout
    align-comments --check file.py       # list misaligned groups with IDs
    align-comments --fix file.py         # fix all groups in place
    align-comments --fix file.py 1 3     # fix only groups 1 and 3
    align-comments --tab 10 file.py      # snap to column multiples of 10
"""

import re
import sys

# Matches: code  # comment — but not # inside strings (best-effort)
_COMMENT_RE = re.compile(
    r"""^((?:[^#"']*(?:"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'))*[^#"']*)(\s*#.*)$"""
)


def _find_comment(line: str, in_multiline: bool) -> tuple[str, str] | None:
    """Split line into (code, comment) or None if no trailing comment."""
    if in_multiline:
        return None
    stripped = line.lstrip()
    if not stripped or stripped.startswith("#"):
        return None
    m = _COMMENT_RE.match(line)
    if not m:
        return None
    code = m.group(1).rstrip()
    comment = m.group(2).lstrip()
    return code, comment


def _track_multiline(line: str, in_multiline: bool, delimiter: str) -> tuple[bool, str]:
    """Track triple-quote state. Returns (in_multiline, delimiter)."""
    i = 0
    while i < len(line):
        if in_multiline:
            idx = line.find(delimiter, i)
            if idx == -1:
                return True, delimiter
            i = idx + 3
            in_multiline = False
            delimiter = ""
        else:
            # Skip single-line strings
            if line[i] in ('"', "'"):
                q = line[i]
                if line[i : i + 3] in ('"""', "'''"):
                    # Check if it opens and closes on same line
                    close = line.find(q * 3, i + 3)
                    if close != -1:
                        i = close + 3
                    else:
                        return True, q * 3
                else:
                    # Single-quoted string — skip to closing quote
                    j = i + 1
                    while j < len(line):
                        if line[j] == "\\":
                            j += 2
                        elif line[j] == q:
                            j += 1
                            break
                        else:
                            j += 1
                    i = j
            else:
                i += 1
    return in_multiline, delimiter


Group = list[tuple[int, str, str]]  # [(line_index, code, comment), ...]


def find_groups(lines: list[str]) -> list[Group]:
    """Find all groups of 2+ consecutive lines with trailing comments."""
    groups: list[Group] = []
    current: Group = []
    in_multiline = False
    delimiter = ""

    def flush() -> None:
        if len(current) >= 2:
            groups.append(list(current))
        current.clear()

    for i, line in enumerate(lines):
        in_multiline, delimiter = _track_multiline(line, in_multiline, delimiter)
        parsed = _find_comment(line, in_multiline)
        if parsed:
            current.append((i, parsed[0], parsed[1]))
        else:
            flush()

    flush()
    return groups


def apply_group(lines: list[str], group: Group, tab: int = 0) -> None:
    """Align comments in a single group, mutating lines in place."""
    min_col = max(len(code) for _, code, _ in group) + 2
    if tab > 0:
        col = ((min_col + tab - 1) // tab) * tab
    else:
        col = min_col
    for idx, code, comment in group:
        lines[idx] = code.ljust(col) + comment


def is_misaligned(lines: list[str], group: Group) -> bool:
    """Check if a group's comments are not yet aligned."""
    min_col = max(len(code) for _, code, _ in group) + 2
    for idx, code, comment in group:
        actual = lines[idx]
        expected = code.ljust(min_col) + comment
        if actual != expected:
            return True
    return False


def main() -> int:
    check = "--check" in sys.argv
    fix = "--fix" in sys.argv
    tab = 0
    skip_next = False
    files: list[str] = []
    group_ids: list[int] = []
    for i, a in enumerate(sys.argv[1:]):
        if skip_next:
            skip_next = False
            continue
        if a == "--tab" and i + 1 < len(sys.argv[1:]):
            tab = int(sys.argv[i + 2])
            skip_next = True
        elif a.startswith("--tab="):
            tab = int(a.split("=", 1)[1])
        elif a.startswith("--"):
            continue
        elif a.isdigit() and files:
            group_ids.append(int(a))
        else:
            files.append(a)

    if files:
        text = open(files[0]).read()
    else:
        text = sys.stdin.read()

    lines = text.splitlines()
    groups = find_groups(lines)

    if check:
        found = False
        for gid, group in enumerate(groups, 1):
            if not is_misaligned(lines, group):
                continue
            found = True
            first_line = group[0][0] + 1
            last_line = group[-1][0] + 1
            print(f"  group {gid} (lines {first_line}-{last_line}):")
            for idx, code, comment in group:
                print(f"    {idx + 1}: {lines[idx]}")
            # show what it would look like
            preview = list(lines)
            apply_group(preview, group, tab=tab)
            print("  fixed:")
            for idx, _, _ in group:
                print(f"    {idx + 1}: {preview[idx]}")
            print()
        if found:
            print(
                f"  {len(groups)} group(s) found. Fix with: --fix file.py [group_ids...]"
            )
        return 1 if found else 0

    if fix or not files:
        targets = set(group_ids) if group_ids else None
        for gid, group in enumerate(groups, 1):
            if targets is not None and gid not in targets:
                continue
            apply_group(lines, group, tab=tab)

        if fix:
            if not files:
                print("--fix requires a file argument", file=sys.stderr)
                return 2
            open(files[0], "w").write("\n".join(lines) + "\n")
            return 0

        print("\n".join(lines))
        return 0

    # Default: align all, stdout
    for group in groups:
        apply_group(lines, group, tab=tab)
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
