"""Minimal best-practices linter for gist files -- AST-based, no dependencies.

Checks:
  firstline  Module docstring's first line must stand alone as a complete
             sentence -- it's what gets auto-extracted into tool listings,
             resource descriptions, and MCP instructions (gists.scan() /
             gists.introspect() both take doc.split("\\n")[0]).
  shape      Public functions/methods returning dict/list/Any should
             document the return shape on the docstring's first line
             (`-> {key, ...}` or `-> [{key, ...}]`), per the gist-authoring
             convention in repld://docs/guide.
  deps       Every top-level import of a non-stdlib, non-sibling-gist
             package should be declared in __repld_deps__ so
             gist_deps.install_deps() can offer to install it for a
             linked project that doesn't already have it.
  legacy     __repld_tools__ is a deprecated tool-registration override;
             gists.py only warns about it reactively (at tool-call time,
             via _warn_deprecated()) -- this catches it statically instead
             of waiting for someone to invoke the tool.

Suppress a finding with a `# gistlint: ignore=<rule>[,<rule>]` comment on
the flagged line or the line above it. For `firstline` (a whole-file check)
the comment may appear anywhere in the file.
"""

from __future__ import annotations

import ast
import io
import re
import sys
import tokenize
from dataclasses import dataclass
from pathlib import Path

from . import gist_deps, gists

_IGNORE_RE = re.compile(r"#\s*gistlint:\s*ignore=([\w,]+)")
# __main__ isn't in stdlib_module_names but every process has one — importing
# it (the shared kernel namespace, per repld's own gist convention) is never
# a pip dependency to declare.
_STDLIB = set(sys.stdlib_module_names) | {"__future__", "__main__"}
_SHAPE_HINTS = ("dict", "list", "any")


@dataclass
class Finding:
    path: Path
    line: int
    rule: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: [{self.rule}] {self.message}"


def lint_paths(paths: list[Path]) -> list[Finding]:
    findings: list[Finding] = []
    for p in paths:
        findings.extend(lint_file(p))
    return findings


def lint_file(path: Path) -> list[Finding]:
    source = path.read_text("utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return [Finding(path, e.lineno or 1, "syntax", f"does not parse: {e.msg}")]

    ignores = _parse_ignores(source)
    findings: list[Finding] = []
    findings.extend(_check_firstline(path, tree, ignores))
    findings.extend(_check_shape(path, tree, ignores))
    findings.extend(_check_deps(path, tree, ignores))
    findings.extend(_check_legacy_tools(path, tree, ignores))
    return findings


def _parse_ignores(source: str) -> dict[int, set[str]]:
    """line number -> set of rule names suppressed by a `# gistlint: ignore=` comment."""
    ignores: dict[int, set[str]] = {}
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for tok in tokens:
            if tok.type != tokenize.COMMENT:
                continue
            m = _IGNORE_RE.search(tok.string)
            if m:
                ignores.setdefault(tok.start[0], set()).update(m.group(1).split(","))
    except tokenize.TokenError:
        pass
    return ignores


def _is_ignored(line: int, rule: str, ignores: dict[int, set[str]]) -> bool:
    for candidate in (line, line - 1):
        rules = ignores.get(candidate)
        if rules and (rule in rules or "all" in rules):
            return True
    return False


def _check_firstline(
    path: Path, tree: ast.Module, ignores: dict[int, set[str]]
) -> list[Finding]:
    if any("firstline" in rules or "all" in rules for rules in ignores.values()):
        return []
    doc = ast.get_docstring(tree, clean=False)
    if not doc or "\n" not in doc:
        return []
    first, rest = doc.split("\n", 1)
    first = first.strip()
    if not rest.strip():
        return []  # single-line docstring, nothing to truncate
    if first.endswith((".", "!", "?", ":")):
        return []
    return [
        Finding(
            path,
            tree.body[0].lineno,
            "firstline",
            "module docstring's first line doesn't end in sentence-terminal "
            "punctuation and continues on the next line -- it gets truncated "
            f"to just {first!r} in tool listings and instructions",
        )
    ]


def _needs_shape_doc(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    if node.returns is None:
        return False
    if isinstance(node.returns, ast.Constant) and node.returns.value is None:
        return False
    try:
        ret = ast.unparse(node.returns).lower()
    except Exception:
        return False
    return any(hint in ret for hint in _SHAPE_HINTS)


def _check_shape(
    path: Path, tree: ast.Module, ignores: dict[int, set[str]]
) -> list[Finding]:
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name.startswith("_"):
            continue
        if not _needs_shape_doc(node):
            continue
        if _is_ignored(node.lineno, "shape", ignores):
            continue
        doc = ast.get_docstring(node)
        first_line = doc.split("\n", 1)[0] if doc else ""
        if "->" not in first_line:
            ret = ast.unparse(node.returns) if node.returns else "?"
            findings.append(
                Finding(
                    path,
                    node.lineno,
                    "shape",
                    f"{node.name}() returns {ret} but its docstring's first "
                    "line has no '-> {shape}' -- document the fields the "
                    "caller gets back",
                )
            )
    return findings


def _root_module(dotted: str) -> str:
    return dotted.split(".")[0]


def _importable_stems(directory: Path) -> set[str]:
    """Top-level module/package names importable from a path: dep directory."""
    stems: set[str] = set()
    for child in directory.iterdir():
        if child.is_file() and child.suffix == ".py" and child.stem != "__init__":
            stems.add(child.stem)
        elif child.is_dir() and (child / "__init__.py").is_file():
            stems.add(child.name)
    return stems


def _declared_deps(tree: ast.Module, gist_path: Path) -> set[str]:
    node = gists._dunder_value(tree, "__repld_deps__")
    if node is None:
        return set()
    try:
        reqs = ast.literal_eval(node)
    except Exception:
        return set()
    if not isinstance(reqs, list):
        return set()
    declared: set[str] = set()
    for r in reqs:
        req_str = str(r)
        if req_str == ".":
            continue
        if req_str.startswith("path:"):
            target = gist_deps.resolve_path_target(req_str[len("path:") :], gist_path)
            if target.is_dir():
                declared.update(_importable_stems(target))
            continue
        declared.add(gist_deps._parse_pkg_name(req_str))
    return declared


def _sibling_gist_names(path: Path) -> set[str]:
    return {p.stem for p in path.parent.glob("*.py") if gists.is_public_gist_file(p)}


def _check_deps(
    path: Path, tree: ast.Module, ignores: dict[int, set[str]]
) -> list[Finding]:
    declared = _declared_deps(tree, path)
    siblings = _sibling_gist_names(path)
    seen: set[str] = set()
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [(alias.name, node.lineno) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0 or node.module is None:  # relative import
                continue
            names = [(node.module, node.lineno)]
        else:
            continue
        for dotted, lineno in names:
            root = _root_module(dotted)
            if root in _STDLIB or root == "repld" or root in siblings:
                continue
            if root in declared or root in seen:
                continue
            if _is_ignored(lineno, "deps", ignores):
                continue
            seen.add(root)
            findings.append(
                Finding(
                    path,
                    lineno,
                    "deps",
                    f"imports '{root}' but __repld_deps__ doesn't declare it "
                    "-- won't auto-install for a project that links this gist",
                )
            )
    return findings


def _check_legacy_tools(
    path: Path, tree: ast.Module, ignores: dict[int, set[str]]
) -> list[Finding]:
    node = gists._dunder_value(tree, "__repld_tools__")
    if node is None:
        return []
    if _is_ignored(node.lineno, "legacy", ignores):
        return []
    return [
        Finding(
            path,
            node.lineno,
            "legacy",
            "__repld_tools__ is deprecated -- use _tool_ functions with "
            "type hints instead (only warns at tool-call time otherwise)",
        )
    ]
