#!/usr/bin/env python3
"""Measure a Python module against the house style (CLAUDE.md "House style").

    scripts/style-measure.py FILE...            text report per file
    scripts/style-measure.py --json FILE...     one JSON object per file
    scripts/style-measure.py --bodies FILE      "qualname hash" per function

The report is the brief a rewrite starts from and the baseline a judge holds it
to: comment and docstring density, function lengths, the names lacking a
docstring, nesting sites at depth 4 or more, the leading verbs of private
helpers, and the non-ASCII count. `--bodies` hashes each function's arguments,
decorators and body with the docstring stripped and the name blanked, so a diff
of two runs separates a rename (new key, same hash) and a docstring, comment or
reorder change (nothing) from code that actually changed (new hash).
"""

import argparse
import ast
import hashlib
import io
import json
import statistics
import sys
import tokenize
from collections import Counter
from pathlib import Path

NESTING_REPORT_DEPTH = 4
BLOCK_NODES = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.With,
    ast.AsyncWith,
    ast.Try,
    ast.TryStar,
    ast.Match,
)
FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)
Func = ast.FunctionDef | ast.AsyncFunctionDef


Documented = ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef


def _docstring_node(node: ast.AST) -> ast.Expr | None:
    """The docstring statement of a module, class or function body, if any."""
    if not isinstance(node, Documented) or not node.body:
        return None
    first = node.body[0]
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        return first
    return None


def _qualified_functions(tree: ast.Module) -> list[tuple[str, Func]]:
    """Every function with its dotted name: `Class.method`, `outer.inner`."""
    return [(name, func) for name, func, _ in _functions_with_scope(tree)]


def _functions_with_scope(tree: ast.Module) -> list[tuple[str, Func, bool]]:
    """(qualname, node, closure) for every function; closure marks one defined inside another."""
    found: list[tuple[str, Func, bool]] = []

    def walk(node: ast.AST, prefix: str, inside_function: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, FUNCTION_NODES):
                name = f"{prefix}{child.name}"
                found.append((name, child, inside_function))
                walk(child, name + ".", True)
            elif isinstance(child, ast.ClassDef):
                walk(child, f"{prefix}{child.name}.", inside_function)
            else:
                walk(child, prefix, inside_function)

    walk(tree, "", False)
    return found


def _max_nesting(func: Func) -> int:
    """Deepest chain of nested blocks inside *func*, not descending into nested defs."""

    def depth(node: ast.AST) -> int:
        deepest = 0
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (*FUNCTION_NODES, ast.ClassDef)):
                continue
            here = depth(child) + (1 if isinstance(child, BLOCK_NODES) else 0)
            deepest = max(deepest, here)
        return deepest

    return depth(func)


def _line_classes(source: str, tree: ast.Module) -> tuple[int, int, int]:
    """(code lines, comment lines, docstring lines), each line counted once."""
    docstring_lines: set[int] = set()
    for node in ast.walk(tree):
        doc = _docstring_node(node)
        if doc is not None and doc.end_lineno is not None:
            docstring_lines.update(range(doc.lineno, doc.end_lineno + 1))
    comment_lines: set[int] = set()
    code_lines: set[int] = set()
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        line = tok.start[0]
        if tok.type == tokenize.COMMENT:
            comment_lines.add(line)
        elif (
            tok.type
            not in (
                tokenize.NL,
                tokenize.NEWLINE,
                tokenize.INDENT,
                tokenize.DEDENT,
                tokenize.ENCODING,
                tokenize.ENDMARKER,
            )
            and line not in docstring_lines
        ):
            code_lines.update(range(line, tok.end[0] + 1))
    code_lines -= docstring_lines
    # A trailing comment shares its line with code; it counts as both.
    return len(code_lines), len(comment_lines), len(docstring_lines)


def _is_public(qualname: str) -> bool:
    return not any(part.startswith("_") for part in qualname.split("."))


def measure(path: Path) -> dict:
    """The house-style measurements for one module."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    code, comments, docs = _line_classes(source, tree)
    scoped = _functions_with_scope(tree)
    functions = [(name, func) for name, func, _ in scoped]
    lengths = [(f.end_lineno or f.lineno) - f.lineno + 1 for _, f in functions]
    missing_public: list[str] = []
    missing_private: list[str] = []
    for name, func, closure in scoped:
        # A closure's contract is its enclosing function's; house style rule 2 names module-level functions and methods.
        if closure or _docstring_node(func) is not None:
            continue
        (missing_public if _is_public(name) else missing_private).append(
            f"{name}:{func.lineno}"
        )
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and _docstring_node(node) is None:
            (missing_public if _is_public(node.name) else missing_private).append(
                f"{node.name}:{node.lineno}"
            )
    nesting = [
        f"{name}:{func.lineno} depth {d}"
        for name, func in functions
        if (d := _max_nesting(func)) >= NESTING_REPORT_DEPTH
    ]
    verbs = Counter(
        name.rsplit(".", 1)[-1].lstrip("_").split("_")[0]
        for name, _ in functions
        if name.rsplit(".", 1)[-1].startswith("_")
        and not name.rsplit(".", 1)[-1].startswith("__")
    )
    non_ascii = Counter(ch for ch in source if ord(ch) > 127)
    return {
        "file": str(path),
        "code_lines": code,
        "comment_lines": comments,
        "docstring_lines": docs,
        "comment_density": round(comments / code, 3) if code else 0.0,
        "docstring_density": round(docs / code, 3) if code else 0.0,
        "functions": len(functions),
        "median_function_lines": statistics.median(lengths) if lengths else 0,
        "max_function_lines": max(lengths, default=0),
        "over_80_lines": [
            f"{n}:{f.lineno}" for (n, f), ln in zip(functions, lengths) if ln > 80
        ],
        "module_docstring": _docstring_node(tree) is not None,
        "missing_docstring_public": missing_public,
        "missing_docstring_private": missing_private,
        "nesting_sites": nesting,
        "helper_verbs": dict(verbs.most_common()),
        "em_dashes": source.count("—"),
        "non_ascii": {ch: n for ch, n in non_ascii.most_common()},
    }


def bodies(path: Path) -> list[tuple[str, str]]:
    """(qualname, hash) per function; the hash ignores the name, docstring and position."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: list[tuple[str, str]] = []
    for name, func in _qualified_functions(tree):
        body = func.body[1:] if _docstring_node(func) is not None else func.body
        clone = ast.FunctionDef(
            name="_",
            args=func.args,
            body=body or [ast.Pass()],
            decorator_list=func.decorator_list,
            returns=func.returns,
            type_params=[],
        )
        digest = hashlib.sha1(ast.dump(clone).encode()).hexdigest()[:12]
        out.append((name, digest))
    return out


def _print_report(m: dict) -> None:
    print(f"{m['file']}")
    print(
        f"  lines: code {m['code_lines']}, comments {m['comment_lines']}"
        f" ({m['comment_density']}/code), docstrings {m['docstring_lines']}"
        f" ({m['docstring_density']}/code)"
    )
    print(
        f"  functions: {m['functions']}, median {m['median_function_lines']} lines,"
        f" max {m['max_function_lines']}, over 80: {', '.join(m['over_80_lines']) or 'none'}"
    )
    if not m["module_docstring"]:
        print("  module docstring: missing")
    for key, label in (
        ("missing_docstring_public", "public"),
        ("missing_docstring_private", "private"),
    ):
        names = m[key]
        print(
            f"  missing docstring ({label}, {len(names)}): {', '.join(names) or 'none'}"
        )
    print(
        f"  nesting >= {NESTING_REPORT_DEPTH} ({len(m['nesting_sites'])}): {', '.join(m['nesting_sites']) or 'none'}"
    )
    print(
        "  helper verbs: "
        + (", ".join(f"{v} {n}" for v, n in m["helper_verbs"].items()) or "none")
    )
    print(
        f"  em dashes: {m['em_dashes']}; non-ascii: "
        + (", ".join(f"{ch!r} {n}" for ch, n in m["non_ascii"].items()) or "none")
    )


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--json", action="store_true", help="one JSON object per file")
    ap.add_argument(
        "--bodies",
        action="store_true",
        help="per-function code hashes instead of the report",
    )
    args = ap.parse_args(argv)
    for path in args.files:
        if args.bodies:
            for name, digest in bodies(path):
                print(f"{name} {digest}")
        elif args.json:
            print(json.dumps(measure(path)))
        else:
            _print_report(measure(path))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
