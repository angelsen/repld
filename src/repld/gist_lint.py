"""Minimal best-practices linter for gist files -- AST-based, no dependencies.

Checks:
  firstline  Module docstring's first line must stand alone as a complete
             sentence -- it's what gets auto-extracted into tool listings,
             resource descriptions, and MCP instructions (gists.scan() /
             gists.introspect() both take doc.split("\\n")[0]). Skipped for
             private (underscore-prefixed) files, whose docstring is never
             extracted; the other three rules still apply to them.
  shape      Public functions/methods returning dict/list/Any should say
             what comes back on the docstring's first line, marked with an
             `->` (the convention in repld://docs/guide is `-> {key, ...}`
             or `-> [{key, ...}]`, but the check only requires the arrow --
             prose like `-> the new record's id` is fine).
  deps       Every import of a non-stdlib, non-sibling-gist package should
             be declared in __repld_deps__ so gist_deps.install_deps() can
             offer to install it for a linked project that doesn't already
             have it. Imports inside a try/except that catches ImportError
             are skipped -- that's how a soft dependency is written, and
             requiring a declaration would defeat the point.
  legacy     The pre-0.1.0 tool convention, both halves: the
             __repld_tools__ declaration list and the _tool_x(args: dict)
             handler signature. repld 0.3 removes them. gists.py only
             warns about the first, reactively (at tool-call time, via
             _warn_deprecated()), and about the second not at all -- so
             this is the only way to find them without invoking the tool.

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
    # firstline only matters for files whose docstring actually gets extracted.
    # gists.scan() skips privates, so a private helper's first line is never
    # surfaced anywhere and the rule has nothing to protect. The other three
    # apply regardless — a private file is imported for real.
    if gists.is_public_gist_file(path):
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
                    "line has no '->' saying what comes back -- e.g. "
                    "'-> {id, name, price}'",
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


# Distribution name -> import root, for packages whose two names are unrelated.
# Deliberately short and deliberately *here* rather than in gist_deps: that
# module answers "is this installed?" from real metadata and must stay
# authoritative, while this is a hardcoded guess that only exists because lint
# is a static check and routinely runs where the dep isn't installed at all.
# The tail of aliased packages is what `# gistlint: ignore=deps` is for.
_IMPORT_ALIASES = {
    "pillow": ("PIL",),
    "beautifulsoup4": ("bs4",),
    "pyyaml": ("yaml",),
    "python-dateutil": ("dateutil",),
    "opencv-python": ("cv2",),
}


def _normalize_dist(name: str) -> str:
    """PEP 503 name normalization, so `Pillow` and `pillow` hit the same key."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _import_names(dist: str) -> set[str]:
    """Import roots a declared distribution can satisfy.

    A declaration is a *distribution* name and an import is a *module* name;
    the two are unrelated for a fair number of packages, so comparing them
    directly flags correctly-declared gists. Three tiers, cheapest first:
    the name normalized (resvg-py -> resvg_py), the installed distribution's
    real top-level modules (the same map gist_deps._is_importable trusts),
    and the static alias table for what's declared but not installed here.
    """
    names = {dist, dist.replace("-", "_")}
    names.update(gist_deps._distribution_import_names(dist))
    names.update(_IMPORT_ALIASES.get(_normalize_dist(dist), ()))
    return names


def _dot_dep_import_names(gist_path: Path) -> set[str]:
    """Import roots the '.' self-dep provides -- the gist's own project.

    Mirrors gist_deps._resolve_dot_dep: project root is the gist directory's
    parent, name from [project] in its pyproject.toml, directory name as the
    fallback. Unlike that function this stays quiet when there's no
    pyproject -- a linter shouldn't manufacture a finding out of a missing
    file. Shares its blind spot too: a project whose package name differs
    from its distribution name won't match either way.
    """
    project_root = gist_path.parent.parent
    pyproject = project_root / "pyproject.toml"
    name = gist_deps._read_project_name(pyproject) if pyproject.is_file() else None
    return _import_names(name or project_root.name)


def _declared_deps(tree: ast.Module, gist_path: Path) -> set[str]:
    """Import roots covered by __repld_deps__ -- import names, not dist names."""
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
        req_str = str(r).strip()
        if req_str == ".":
            declared.update(_dot_dep_import_names(gist_path))
            continue
        if req_str.startswith("path:"):
            target = gist_deps.resolve_path_target(req_str[len("path:") :], gist_path)
            if target.is_dir():
                declared.update(_importable_stems(target))
            continue
        declared.update(_import_names(gist_deps._parse_pkg_name(req_str)))
    return declared


def _sibling_gist_names(path: Path) -> set[str]:
    """Module names importable from the gist's own directory.

    Deliberately *not* filtered through gists.is_public_gist_file(): that
    predicate decides what gets listed and exposed as a gist, not what
    resolves at import time. The directory is on sys.path either way, so
    `from _helpers import x` works and is never a pip dep to declare.
    """
    return {p.stem for p in path.parent.glob("*.py")}


# Anything in here catches an ImportError, so an import wrapped in it is a
# soft dependency by construction. Exception/BaseException are included
# because they genuinely do catch it -- the rule is "would this import raise
# out of the module", not "did the author name the tidiest exception".
_IMPORT_GUARDS = frozenset(
    {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"}
)


def _catches_import_error(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True  # bare `except:` -- sloppy, but it does guard
    types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(isinstance(t, ast.Name) and t.id in _IMPORT_GUARDS for t in types)


def _guarded_imports(tree: ast.Module) -> set[ast.stmt]:
    """Imports the author deliberately made optional.

    ``try: import x / except ImportError: x = None`` is the standard way to
    write a soft dependency, and the deps rule flagging it told people to
    declare something they had gone out of their way not to require.

    Only the ``try`` body counts. An import in a handler is the *fallback* and
    is unguarded; one in ``else``/``finally`` isn't covered by the handlers
    either.
    """
    guarded: set[ast.stmt] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        if not any(_catches_import_error(h) for h in node.handlers):
            continue
        for stmt in node.body:
            for child in ast.walk(stmt):
                if isinstance(child, (ast.Import, ast.ImportFrom)):
                    guarded.add(child)
    return guarded


def _check_deps(
    path: Path, tree: ast.Module, ignores: dict[int, set[str]]
) -> list[Finding]:
    declared = _declared_deps(tree, path)
    siblings = _sibling_gist_names(path)
    guarded = _guarded_imports(tree)
    seen: set[str] = set()
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if node in guarded:
            continue
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


def _is_old_style_def(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """AST twin of ``gists._is_old_style``: one parameter, ``dict`` or bare.

    That module decides by ``inspect.signature`` on the imported function; this
    has to reach the same verdict without importing anything, so the two must
    be changed together.
    """
    args = node.args
    if args.posonlyargs or args.kwonlyargs or args.vararg or args.kwarg:
        return False
    if len(args.args) != 1:
        return False
    ann = args.args[0].annotation
    return ann is None or (isinstance(ann, ast.Name) and ann.id == "dict")


def _check_legacy_tools(
    path: Path, tree: ast.Module, ignores: dict[int, set[str]]
) -> list[Finding]:
    """Both halves of the pre-0.1.0 tool convention, which 0.3 removes.

    A gist can be caught by either independently: the ``__repld_tools__``
    declaration list, and the ``_tool_x(args: dict)`` handler signature it used
    to pair with. Typed-declaring files with an old-style handler are the easy
    ones to miss — nothing warns about those until the tool is actually called.
    """
    findings: list[Finding] = []
    node = gists._dunder_value(tree, "__repld_tools__")
    if node is not None and not _is_ignored(node.lineno, "legacy", ignores):
        findings.append(
            Finding(
                path,
                node.lineno,
                "legacy",
                "__repld_tools__ is deprecated -- declare tools as _tool_ "
                "functions with type hints and let the schema be inferred "
                "(removed in repld 0.3)",
            )
        )
    for fn in ast.iter_child_nodes(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not fn.name.startswith("_tool_") or not _is_old_style_def(fn):
            continue
        if _is_ignored(fn.lineno, "legacy", ignores):
            continue
        findings.append(
            Finding(
                path,
                fn.lineno,
                "legacy",
                f"{fn.name}({fn.args.args[0].arg}: dict) uses the legacy "
                "single-dict handler signature -- give each argument its own "
                "typed parameter instead (removed in repld 0.3)",
            )
        )
    return findings
