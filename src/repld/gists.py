"""Auto-reloading import finder for ~/.repld/gists/ and ./gists/."""

from __future__ import annotations

import ast
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import sys
from pathlib import Path

__all__ = ["install", "scan", "scan_tools", "resolve_tool", "signature"]

# Module names managed by the gist finder (populated by _GistFinder)
_managed: dict[str, Path] = {}    # fullname → source .py path
_mtimes: dict[str, float] = {}    # fullname → last known mtime
_installed_dirs: list[Path] = []  # set by install()


def _check_reload(fullname: str) -> None:
    """If the gist file changed, evict from sys.modules so next import reloads it."""
    src = _managed.get(fullname)
    if src is None or not src.is_file():
        return
    mtime = src.stat().st_mtime
    prev = _mtimes.get(fullname)
    if prev is not None and mtime > prev:
        sys.modules.pop(fullname, None)
        # Don't update _mtimes here — let find_spec update it on reload


class _GistFinder(importlib.abc.MetaPathFinder):
    """Finder that checks gist directories and tracks mtimes for auto-reload.

    Must be placed first in sys.meta_path so it's consulted before the standard
    PathFinder can return the cached module.
    """

    def __init__(self, dirs: list[Path]) -> None:
        self._dirs = dirs

    def find_spec(
        self,
        fullname: str,
        path: object,
        target: object = None,
    ) -> importlib.machinery.ModuleSpec | None:
        parts = fullname.split(".")
        for d in self._dirs:
            candidate = d.joinpath(*parts)
            # Check package (dir/__init__.py) or module (.py)
            for p in [candidate / "__init__.py", candidate.with_suffix(".py")]:
                if p.is_file():
                    mtime = p.stat().st_mtime
                    _managed[fullname] = p
                    _mtimes[fullname] = mtime
                    return importlib.util.spec_from_file_location(
                        fullname,
                        p,
                        submodule_search_locations=(
                            [str(candidate)] if p.name == "__init__.py" else None
                        ),
                    )
        return None


class _GistImportHook:
    """Wraps builtins.__import__ to check for stale gist modules before import."""

    def __init__(self, original) -> None:
        self._original = original

    def __call__(self, name, globals=None, locals=None, fromlist=(), level=0):
        # Resolve the fully-qualified module name for relative imports
        if level > 0 and globals is not None:
            package = globals.get("__package__") or ""
            if level > 1:
                parts = package.rsplit(".", level - 1)
                package = parts[0] if parts else ""
            base = package + ("." + name if name else "")
        else:
            base = name

        # Check if this module (or its top-level) is a managed gist
        top = base.split(".")[0]
        _check_reload(base)
        _check_reload(top)

        result = self._original(name, globals, locals, fromlist, level)

        # Auto-inject API summary on gist import.
        if top in _managed:
            try:
                summary = introspect(top)
                if summary:
                    print(summary)
            except Exception:
                pass

        return result


def _extract_doc(path: Path) -> str:
    """Extract first line of module docstring without importing."""
    try:
        tree = ast.parse(path.read_text("utf-8"))
        doc = ast.get_docstring(tree)
        if doc:
            return doc.split("\n")[0].strip()[:80]
    except Exception:
        pass
    return ""


def hint_for_name(name: str) -> str | None:
    """If `name` matches a gist variable or class name, return a usage hint."""
    for d in _installed_dirs:
        if not d.is_dir():
            continue
        for p in d.glob("*.py"):
            if p.name.startswith("_"):
                continue
            try:
                tree = ast.parse(p.read_text("utf-8"))
            except Exception:
                continue
            usage = None
            classes: list[str] = []
            for node in ast.iter_child_nodes(tree):
                if (
                    isinstance(node, ast.Assign)
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == "__repld_usage__"
                    and isinstance(node.value, ast.Constant)
                ):
                    usage = str(node.value.value)
                elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
                    classes.append(node.name)
            # Check usage variable (e.g. "ig" from "ig = await IG.connect()")
            if usage:
                lhs = usage.split("=")[0].strip()
                if lhs == name:
                    return f"from gist {p.stem}: {usage}"
            # Check class names (e.g. "IG" from instagram.py)
            if name in classes:
                hint = f"from {p.stem} import {name}"
                if usage:
                    hint += f"; then: {usage}"
                return hint
    return None


def scan() -> list[tuple[str, str]]:
    """Scan gist directories for .py modules. Returns [(name, one_line_doc), ...]."""
    results: list[tuple[str, str]] = []
    seen: set[str] = set()
    for d in _installed_dirs:
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.py")):
            if p.name.startswith("_"):
                continue
            name = p.stem
            if name in seen:
                continue
            seen.add(name)
            # Check loaded module for __repld_help__ override
            mod = sys.modules.get(name)
            if mod and hasattr(mod, "__repld_help__"):
                results.append((name, str(mod.__repld_help__)))
                continue
            # Else parse first docstring line from file
            doc = _extract_doc(p)
            if doc:
                results.append((name, doc))
    return results


def introspect(name: str) -> str:
    """AST-introspect a gist module. Returns formatted API summary."""
    path = _find_gist(name)
    if path is None:
        raise FileNotFoundError(f"No gist '{name}' found in {_installed_dirs}")

    tree = ast.parse(path.read_text("utf-8"))
    lines: list[str] = []

    mod_doc = ast.get_docstring(tree)
    if mod_doc:
        lines.append(mod_doc.split("\n")[0].strip())
        lines.append("")

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            _format_class(node, lines)
        elif isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef)
        ) and not node.name.startswith("_"):
            _format_function(node, lines, indent="")

    return "\n".join(lines)


def _find_gist(name: str) -> Path | None:
    """Resolve gist name to file path."""
    for d in _installed_dirs:
        p = d / f"{name}.py"
        if p.is_file():
            return p
    return None


def _format_class(node: ast.ClassDef, lines: list[str]) -> None:
    """Format a class: ClassName(init_args) + public methods."""
    init_args = ""
    for item in node.body:
        if isinstance(item, ast.FunctionDef) and item.name == "__init__":
            init_args = _format_args(item.args, skip_self=True)
            break

    lines.append(f"{node.name}({init_args})")

    cls_doc = ast.get_docstring(node)
    if cls_doc:
        lines.append(f"  {cls_doc.split(chr(10))[0].strip()}")
        lines.append("")

    for item in node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if item.name.startswith("_"):
                continue
            _format_function(item, lines, indent="  ", is_method=True)


def _format_function(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    lines: list[str],
    indent: str = "",
    is_method: bool = False,
) -> None:
    """Format one function/method line."""
    async_prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
    prefix = "." if is_method else ""
    args = _format_args(node.args, skip_self=is_method)
    ret = ""
    if node.returns:
        ret = f" -> {ast.unparse(node.returns)}"

    sig = f"{indent}{async_prefix}{prefix}{node.name}({args}){ret}"

    doc = ast.get_docstring(node)
    if doc:
        first_line = doc.split("\n")[0].strip()
        sig += f"  # {first_line}"

    lines.append(sig)


def _format_args(args: ast.arguments, skip_self: bool = False) -> str:
    """Format function arguments as compact string."""
    parts: list[str] = []
    all_args = args.args[1:] if skip_self else args.args

    for arg in all_args:
        s = arg.arg
        if arg.annotation:
            s += f": {ast.unparse(arg.annotation)}"
        parts.append(s)

    for arg in args.kwonlyargs:
        s = arg.arg
        if arg.annotation:
            s += f": {ast.unparse(arg.annotation)}"
        s += "="
        parts.append(s)

    return ", ".join(parts)


def signature(name: str) -> str:
    """Return 'ClassName(args)' for a gist's first public class, or ''.

    Always AST-derived — ``__repld_usage__`` is handled separately in
    ``build_instructions()`` as a display concern.
    Appends ``[async]`` when the class has async methods.
    """
    path = _find_gist(name)
    if not path:
        return ""
    try:
        tree = ast.parse(path.read_text("utf-8"))
    except Exception:
        return ""
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            init_args = ""
            has_async = False
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                    init_args = _format_args(item.args, skip_self=True)
                if isinstance(item, ast.AsyncFunctionDef):
                    has_async = True
            sig = f"{node.name}({init_args})"
            if has_async:
                sig += " [async]"
            return sig
    return ""


def _extract_tools(path: Path) -> list[dict]:
    """Extract __repld_tools__ list from a gist file via ast.literal_eval."""
    try:
        tree = ast.parse(path.read_text("utf-8"))
    except Exception:
        return []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__repld_tools__":
                    try:
                        return ast.literal_eval(ast.unparse(node.value))
                    except Exception:
                        return []
    return []


def _iter_gist_files():
    """Yield non-private .py paths from installed gist directories."""
    for d in _installed_dirs:
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.py")):
            if not p.name.startswith("_"):
                yield p


def scan_tools() -> list[dict]:
    """Scan gist files for __repld_tools__ declarations. Returns tool schemas."""
    results: list[dict] = []
    seen: set[str] = set()
    for p in _iter_gist_files():
        for tool in _extract_tools(p):
            name = tool.get("name")
            if name and name not in seen:
                seen.add(name)
                results.append(tool)
    return results


def resolve_tool(name: str):
    """Import the gist that declares *name* and return its ``_tool_*`` handler.

    Returns ``None`` if no gist claims the tool.  Raises ``AttributeError``
    if the gist declares the tool but has no matching handler function.
    """
    for p in _iter_gist_files():
        tool_names = {t.get("name") for t in _extract_tools(p)}
        if name in tool_names:
            mod_name = p.stem
            _check_reload(mod_name)
            mod = importlib.import_module(mod_name)
            handler_name = f"_tool_{name}"
            handler = getattr(mod, handler_name, None)
            if handler is None:
                raise AttributeError(
                    f"gist '{mod_name}' declares tool '{name}' "
                    f"but has no {handler_name}() handler"
                )
            return handler
    return None


def install(dirs: list[Path]) -> None:
    """Add gist directories to sys.path and install the auto-reload finder."""
    import builtins

    global _installed_dirs
    _installed_dirs = dirs

    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
        s = str(d)
        if s not in sys.path:
            sys.path.insert(0, s)

    # Install the finder at the front of sys.meta_path
    # Guard against double-install
    if not any(isinstance(f, _GistFinder) for f in sys.meta_path):
        sys.meta_path.insert(0, _GistFinder(dirs))

    # Wrap builtins.__import__ to intercept stale-module eviction
    # Guard against double-wrapping
    if not isinstance(builtins.__import__, _GistImportHook):
        builtins.__import__ = _GistImportHook(builtins.__import__)
