"""Gist source introspection: AST in, agent-facing API text out.

Split out of `gists.py`, which owned two jobs — an import hook with mutable
module state (`_managed`, `_mtimes`, `_installed_dirs`, the registry) and this,
which renders a gist's public surface for `repld://gists/{name}`, the MCP
`instructions`, and `repld gist list`. Nothing here touches any of that state:
it takes a path or a parsed tree and returns a string.

That is also why it imports nothing from repld. `gists.py` is deliberately in a
two-way cycle with `gist_deps`/`gist_links`, resolved by call-time `module.attr`
access — this module sits underneath all of it and can be imported outright.
`gists` re-exports `_parse` and `_dunder_value`, which `gist_links`,
`gist_deps` and `gist_lint` reach as `gists._parse` / `gists._dunder_value`;
that is the documented cycle contract and it keeps working unchanged.
"""

from __future__ import annotations

import ast
from pathlib import Path

_parse_cache: dict[str, tuple[float, ast.Module | None]] = {}


def _parse(path: Path) -> ast.Module | None:
    """ast.parse a gist file; None if unreadable or unparseable.

    Memoized on (path, mtime) — a single MCP initialize touches each gist
    file several times (scan / signature / usage / tools), and mtime-keyed
    staleness matches the reload semantics of _check_reload.
    """
    try:
        key, mtime = str(path), path.stat().st_mtime
    except OSError:
        return None
    hit = _parse_cache.get(key)
    if hit is not None and hit[0] == mtime:
        return hit[1]
    try:
        tree = ast.parse(path.read_text("utf-8"))
    except Exception:
        tree = None
    _parse_cache[key] = (mtime, tree)
    return tree


def _dunder_value(tree: ast.Module, name: str) -> ast.expr | None:
    """Return the value node of the first top-level `name = <literal>` assignment."""
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return node.value
    return None


def _usage_value(tree: ast.Module) -> str | None:
    """String value of a top-level `__repld_usage__ = "..."`, or None."""
    node = _dunder_value(tree, "__repld_usage__")
    return str(node.value) if isinstance(node, ast.Constant) else None


def _first_line(doc: str | None, limit: int | None = None) -> str:
    """First line of a docstring, stripped; '' if no doc."""
    return doc.split("\n")[0].strip()[:limit] if doc else ""


def _extract_doc(path: Path) -> str:
    """Extract first line of module docstring without importing."""
    tree = _parse(path)
    doc = ast.get_docstring(tree) if tree else None
    return _first_line(doc, limit=80)


def _init_args(node: ast.ClassDef) -> str:
    """Extract and format __init__'s argument list (excluding self)."""
    for item in node.body:
        if isinstance(item, ast.FunctionDef) and item.name == "__init__":
            return _format_args(item.args, skip_self=True)
    return ""


def _format_class(node: ast.ClassDef, lines: list[str]) -> None:
    """Format a class: ClassName(init_args) + public methods."""
    lines.append(f"{node.name}({_init_args(node)})")

    cls_doc = ast.get_docstring(node)
    if cls_doc:
        lines.append(f"  {_first_line(cls_doc)}")
        lines.append("")

    for item in node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if item.name.startswith("_"):
                continue
            if _decorator_names(item) & {"setter", "deleter"}:
                continue  # getter (below) already lists this name once
            is_property = bool(_decorator_names(item) & {"property", "cached_property"})
            _format_function(
                item, lines, indent="  ", is_method=True, is_property=is_property
            )


def _decorator_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Bare decorator names on a function/method (`@x` and `@x.y` → {'x', 'y'})."""
    names: set[str] = set()
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, ast.Attribute):
            names.add(target.attr)
    return names


def _format_function(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    lines: list[str],
    indent: str = "",
    is_method: bool = False,
    is_property: bool = False,
) -> None:
    """Format one function/method line.

    Properties render as `.name -> ret` (no call parens, no args) since
    they're accessed as attributes, not called.
    """
    async_prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
    prefix = "." if is_method else ""
    ret = ""
    if node.returns:
        ret = f" -> {ast.unparse(node.returns)}"

    if is_property:
        sig = f"{indent}{prefix}{node.name}{ret}"
    else:
        args = _format_args(node.args, skip_self=is_method)
        sig = f"{indent}{async_prefix}{prefix}{node.name}({args}){ret}"

    doc = ast.get_docstring(node)
    if doc:
        sig += f"  # {_first_line(doc)}"

    lines.append(sig)


def _format_args(args: ast.arguments, skip_self: bool = False) -> str:
    """Format function arguments as compact string ('=' marks a default)."""
    parts: list[str] = []
    pos_args = args.posonlyargs + args.args
    if skip_self:
        pos_args = pos_args[1:]
    first_default = len(pos_args) - len(args.defaults)

    for i, arg in enumerate(pos_args):
        s = arg.arg
        if arg.annotation:
            s += f": {ast.unparse(arg.annotation)}"
        if i >= first_default:
            s += "="
        parts.append(s)

    if args.vararg:
        parts.append(f"*{args.vararg.arg}")
    elif args.kwonlyargs:
        parts.append("*")

    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        s = arg.arg
        if arg.annotation:
            s += f": {ast.unparse(arg.annotation)}"
        if default is not None:
            s += "="
        parts.append(s)

    if args.kwarg:
        parts.append(f"**{args.kwarg.arg}")

    return ", ".join(parts)


def _is_exception_class(node: ast.ClassDef) -> bool:
    """True if node looks like an exception type, not an entry-point class.

    Name- and base-suffix heuristic (e.g. GigahostError(RuntimeError)) — gists
    commonly define a custom error type before their main class, and that
    error type should never win "the" signature() pick.
    """
    if node.name.endswith(("Error", "Exception")):
        return True
    for base in node.bases:
        base_name = base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
        if base_name.endswith(("Error", "Exception")):
            return True
    return False


def signature_for_path(path: Path) -> str:
    """Like signature(), but for a path already in hand (no _installed_dirs lookup)."""
    tree = _parse(path)
    if tree is None:
        return ""
    for node in ast.iter_child_nodes(tree):
        if (
            isinstance(node, ast.ClassDef)
            and not node.name.startswith("_")
            and not _is_exception_class(node)
        ):
            has_async = any(
                isinstance(item, ast.AsyncFunctionDef) for item in node.body
            )
            sig = f"{node.name}({_init_args(node)})"
            if has_async:
                sig += " [async]"
            return sig
    return ""
