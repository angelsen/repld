"""Auto-reloading import finder for ~/.repld/gists/ and ./gists/."""

from __future__ import annotations

import ast
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import inspect
import json
import os
import sys
import types
import typing
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

# Deps + links live in sibling modules. Intentional two-way cycle: they do
# `from . import gists` back; all cross-module access is module.attr at call
# time (never `from x import y`), which is cycle-safe and keeps test
# monkeypatching (e.g. gists.registry) effective.
from . import gist_deps, gist_links
from .state import atomic_write_json

__all__ = [
    "install",
    "scan",
    "scan_tools",
    "resolve_tool",
    "signature",
    "signature_for_path",
    "introspect",
    "hint_for_name",
    "usage_for",
    "registry",
    "registry_summary",
]

# Module names managed by the gist finder (populated by _GistFinder)
_managed: dict[str, Path] = {}  # fullname → source .py path
_mtimes: dict[str, float] = {}  # fullname → last known mtime
_installed_dirs: list[Path] = []  # set by install()

# Subset of _managed sourced from a 'path:' dep directory rather than a real
# gist dir or link. Still gets _check_reload's mtime-eviction, but is excluded
# from _register()/introspect() — those assume gist authoring conventions
# (docstring-as-description, registry entries) that don't apply to vendored
# third-party code.
_path_dep_modules: set[str] = set()

# Dedup warnings (malformed __repld_deps__, un-inspectable tool signatures,
# failed imports) so boot warns once but subsequent tools/list scans stay quiet.
_malformed_warned: set[str] = set()

# Python type → JSON Schema type, for inferring tool input schemas from
# _tool_* function signatures.
_TYPE_MAP: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}

_REGISTRY_PATH = (
    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    / "repld"
    / "gist-registry.json"
)


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


def _warn_once(key: str, msg: str) -> None:
    """Print msg to stderr the first time key is seen; silent on repeats."""
    if key in _malformed_warned:
        return
    _malformed_warned.add(key)
    print(msg, file=sys.stderr)


# (name, path) pairs already written to the registry this process — avoids a
# full read-parse-write of the registry JSON on every re-import.
_registered: set[tuple[str, str]] = set()


def _read_registry() -> dict:
    """Read the gist registry JSON, or {} on missing/corrupt file."""
    if not _REGISTRY_PATH.is_file():
        return {}
    try:
        return json.loads(_REGISTRY_PATH.read_text("utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        _warn_once(
            "registry:corrupt",
            f"repld: gist registry {_REGISTRY_PATH} is corrupt ({exc}) — "
            "treating as empty",
        )
        return {}


def _register(name: str) -> None:
    """Record a gist import in the central registry. Best-effort, never raises."""
    try:
        src = _managed.get(name)
        if src is None:
            return
        if (name, str(src)) in _registered:
            return
        _REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        reg = _read_registry()
        doc = _extract_doc(src)
        reg[name] = {
            "path": str(src),
            "description": doc,
            "project": str(Path.cwd()),
            "last_used": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        atomic_write_json(_REGISTRY_PATH, reg, indent=2)
        _registered.add((name, str(src)))
    except Exception:
        pass


def registry() -> dict:
    """Read the gist registry. Returns {name: {path, description, project, last_used}}."""
    return _read_registry()


def registry_summary() -> str:
    """Render the cross-project registry as text, grouped by project (recent first)."""
    reg = registry()
    if not reg:
        return "(gist registry empty — import a gist in any project to populate it)"
    by_project: dict[str, list[tuple[str, dict]]] = {}
    for name, entry in reg.items():
        by_project.setdefault(entry.get("project", "?"), []).append((name, entry))
    lines = [
        "Gist registry — every gist seen across projects.",
        "Link one into the current project: repld gist add <name>",
        "",
    ]
    for project, entries in sorted(
        by_project.items(),
        key=lambda kv: max((e.get("last_used", "") for _, e in kv[1]), default=""),
        reverse=True,
    ):
        lines.append(project)
        for name, entry in sorted(entries, key=lambda x: x[0]):
            stale = "" if Path(entry.get("path", "")).is_file() else "  (stale)"
            date = (entry.get("last_used", "") or "")[:10]
            desc = entry.get("description", "") or ""
            lines.append(f"  {name:<22} {date}  {desc}{stale}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _check_reload(fullname: str) -> None:
    """If the gist file changed, evict from sys.modules so next import reloads it.

    Also re-checks __repld_deps__ for just this file and prompts for anything
    newly declared — otherwise a dependency added after kernel boot would sit
    silently unchecked until someone thought to restart the whole process
    (scan_deps() only ever ran once, at boot, before this).
    """
    src = _managed.get(fullname)
    if src is None or not src.is_file():
        return
    mtime = src.stat().st_mtime
    prev = _mtimes.get(fullname)
    if prev is not None and mtime > prev:
        sys.modules.pop(fullname, None)
        missing = gist_deps.scan_deps(paths=[src])
        if missing:
            gist_deps.install_deps(missing)
        # Don't update _mtimes here — let find_spec update it on reload


def _scan_new_deps(src: Path) -> None:
    """First-sight __repld_deps__ scan for a module find_spec hasn't tracked yet.

    Boot-time scan_deps() covers everything that exists when the kernel starts;
    _check_reload's edit-triggered rescan covers every later change. Neither
    covers a gist written and imported for the first time in the same session
    -- this closes that gap at the one point a never-before-seen module is
    guaranteed to pass through.
    """
    missing = gist_deps.scan_deps(paths=[src])
    if missing:
        gist_deps.install_deps(missing)


class _GistFinder(importlib.abc.MetaPathFinder):
    """Finder that checks gist directories and tracks mtimes for auto-reload.

    Also checks 'path:' dep directories (see gist_deps._path_dep_dirs), so
    vendored code gets the same reload tracking as gists — modules found
    there are flagged in _path_dep_modules to skip gist-specific side
    effects on import.

    Must be placed first in sys.meta_path so it's consulted before the standard
    PathFinder can return the cached module.
    """

    def __init__(self, dirs: list[Path]) -> None:
        self._dirs = dirs

    @staticmethod
    def _track(
        fullname: str, p: Path, search_root: Path | None
    ) -> importlib.machinery.ModuleSpec | None:
        """Record a hit for auto-reload and build its spec.

        The mtime bookkeeping is the whole point of this finder, so it lives in
        one place rather than once per search root — the three scans below
        differ only in *where* they look and what they do afterwards.
        """
        if fullname not in _managed:
            _scan_new_deps(p)
        _managed[fullname] = p
        _mtimes[fullname] = p.stat().st_mtime
        return importlib.util.spec_from_file_location(
            fullname,
            p,
            submodule_search_locations=(
                [str(search_root)] if search_root and p.name == "__init__.py" else None
            ),
        )

    @staticmethod
    def _find_in(dirs, parts: list[str]) -> "tuple[Path, Path] | None":
        """First (file, package-root) match for a dotted name under *dirs*."""
        for d in dirs:
            candidate = Path(d).joinpath(*parts)
            # Check package (dir/__init__.py) or module (.py)
            for p in [candidate / "__init__.py", candidate.with_suffix(".py")]:
                if p.is_file():
                    return p, candidate
        return None

    def find_spec(
        self,
        fullname: str,
        path: object,
        target: object = None,
    ) -> importlib.machinery.ModuleSpec | None:
        parts = fullname.split(".")
        hit = self._find_in(self._dirs, parts)
        if hit is not None:
            return self._track(fullname, hit[0], hit[1])
        # Cross-project linked gist (exact name only — local dirs win above;
        # same precedence rule as _find_gist and _iter_gist_files).
        linked = gist_links.linked_path(fullname)
        if linked is not None:
            return self._track(fullname, linked, None)
        # 'path:' dep directories (vendored code prepended to sys.path) —
        # same mtime tracking as above, flagged in _path_dep_modules so the
        # import hook skips the gist-authoring side effects for it.
        hit = self._find_in(gist_deps._path_dep_dirs, parts)
        if hit is not None:
            _path_dep_modules.add(fullname)
            return self._track(fullname, hit[0], hit[1])
        return None


# Venvs this kernel has already declined to adopt, so the check isn't redone
# on every failed import. Sound to cache: the only reason to decline is a
# Python version mismatch, and this process's version cannot change.
_unusable_venvs: set[Path] = set()


def _recover_missing_import(original, args):
    """Last chance for a failed import: adopt a late project venv, then retry.

    Retries **the import**, never the caller's cell. Re-running a cell would
    break the rule that ``exec`` runs arbitrary code exactly once — a cell that
    POSTs and then hits a bad import would POST twice. An import is idempotent.

    Two things get fixed here, both of which look identical to user code:
    a package installed into the bound venv since this kernel started (Python
    caches the failed lookup, so it stays missing until the caches are
    invalidated), and a ``.venv`` that only appeared after boot.

    The retry is unconditional on purpose. Remembering "module X is missing"
    would be faster but wrong: a `uv add` mid-session has to be picked up, and
    a memo would keep reporting the package missing after it was installed.
    The venv check is memoized instead, which is safe — see `_unusable_venvs`.
    Guarded imports (`try: import x / except ImportError`) pay for that retry
    too; there is no way to see the caller's `try` from inside `__import__`,
    and the alternative is missing the case the recovery exists for.
    """
    import importlib

    from . import bind

    venv = bind.project_venv()
    if venv is not None and venv not in _unusable_venvs and not bind.is_bound(venv):
        added = bind.adopt(venv)
        if added is None:
            _unusable_venvs.add(venv)
        else:
            from .channel import push_kind

            push_kind(f"[repld] bound {venv} — its packages are now importable", "venv")
    importlib.invalidate_caches()
    try:
        return original(*args)
    except ModuleNotFoundError as retried:
        raise _explain_missing(retried, venv) from None


def _explain_missing(
    exc: ModuleNotFoundError, venv: Path | None
) -> ModuleNotFoundError:
    """Name the interpreter mismatch when that's why an import failed.

    A bare ``No module named 'partbridge'`` is true but unhelpful when the
    package is sitting right there in a venv this interpreter can't use.
    """
    from . import bind

    missing = (exc.name or "").split(".")[0]
    if not missing or venv is None or bind.version_matches(venv):
        return exc
    sp = bind.site_packages(venv)
    if sp is None:
        return exc
    present = (
        (sp / missing).is_dir()
        or (sp / f"{missing}.py").is_file()
        or any(sp.glob(f"{missing}-*.dist-info"))
        or any(sp.glob(f"{missing.replace('_', '-')}-*.dist-info"))
    )
    if not present:
        return exc
    want = bind.venv_python_version(venv)
    running = f"{sys.version_info[0]}.{sys.version_info[1]}"
    target = f"{want[0]}.{want[1]}" if want else "?"
    return ModuleNotFoundError(
        f"{exc} — but '{missing}' is installed in {venv} (Python {target}), "
        f"and this kernel is Python {running}. Its compiled packages can't be "
        f"loaded across versions; call the repld_restart tool (or `repld "
        f"restart`) to rebind the kernel to the project.",
        name=exc.name,
        path=exc.path,
    )


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

        # Check if this module (or its top-level) is a managed gist. Dedupe
        # base/top when equal (the common flat-gist case) — _check_reload's
        # dep-scan prompt would otherwise fire twice for one reload, since
        # _mtimes isn't updated until find_spec runs, below.
        top = base.split(".")[0]
        for candidate in {base, top}:
            _check_reload(candidate)

        try:
            result = self._original(name, globals, locals, fromlist, level)
        except ModuleNotFoundError:
            # The original exception is deliberately dropped: the recovery
            # re-raises off its own retry, so the message reflects the state
            # *after* a late venv was adopted rather than before.
            result = _recover_missing_import(
                self._original, (name, globals, locals, fromlist, level)
            )

        # Auto-inject API summary on gist import + register in central registry.
        # Skipped for path: dep modules — they're vendored third-party code,
        # not gists, so gist-authoring conventions (docstring-as-description,
        # registry entries) don't apply.
        if top in _managed and top not in _path_dep_modules:
            _register(top)
            try:
                summary = introspect(top)
                if summary:
                    print(summary)
            except Exception:
                pass

        return result


def _first_line(doc: str | None, limit: int | None = None) -> str:
    """First line of a docstring, stripped; '' if no doc."""
    return doc.split("\n")[0].strip()[:limit] if doc else ""


def _extract_doc(path: Path) -> str:
    """Extract first line of module docstring without importing."""
    tree = _parse(path)
    doc = ast.get_docstring(tree) if tree else None
    return _first_line(doc, limit=80)


def hint_for_name(name: str) -> str | None:
    """If `name` matches a gist variable or class name, return a usage hint."""
    for p in _iter_gist_files():
        tree = _parse(p)
        if tree is None:
            continue
        usage = _usage_value(tree)
        classes = [
            node.name
            for node in ast.iter_child_nodes(tree)
            if isinstance(node, ast.ClassDef) and not node.name.startswith("_")
        ]
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
    """Scan gist files (local + linked) for .py modules. Returns [(name, doc), ...]."""
    results: list[tuple[str, str]] = []
    for p in _iter_gist_files():
        name = p.stem
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
        msg = f"No gist '{name}' found in {_installed_dirs}"
        if gist_links.linked_names():
            msg += f"; linked: {', '.join(sorted(gist_links.linked_names()))}"
        raise FileNotFoundError(msg)

    tree = _parse(path)
    if tree is None:
        # _parse swallows errors for the scan paths — re-parse to surface why.
        try:
            tree = ast.parse(path.read_text("utf-8"))
        except SyntaxError as e:
            raise ValueError(
                f"gist '{name}': syntax error at line {e.lineno}: {e.msg}"
            ) from e
    lines: list[str] = []

    mod_doc = ast.get_docstring(tree)
    if mod_doc:
        lines.append(_first_line(mod_doc))
        lines.append("")

    lines.append(import_hint(name))
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
    """Resolve gist name to a single .py file for AST introspection.

    Precedence rule (shared with _GistFinder.find_spec and _iter_gist_files):
    installed dirs in order, then _linked — local always shadows linked.
    """
    for d in _installed_dirs:
        p = d / f"{name}.py"
        if p.is_file():
            return p
    return gist_links.linked_path(name)


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


def signature(name: str) -> str:
    """Return 'ClassName(args)' for a gist's first public class, or ''.

    Always AST-derived — ``__repld_usage__`` is handled separately via
    ``usage_for()`` as a display concern.
    Appends ``[async]`` when the class has async methods.
    """
    path = _find_gist(name)
    return signature_for_path(path) if path else ""


def usage_for(name: str) -> str | None:
    """AST-derived ``__repld_usage__`` override for a gist, or None.

    Works before the gist is imported (unlike a ``sys.modules`` lookup),
    so first-boot MCP instructions can show it.
    """
    path = _find_gist(name)
    if path is None:
        return None
    tree = _parse(path)
    if tree is None:
        return None
    return _usage_value(tree)


def import_hint(name: str) -> str:
    """Shortest correct 'how to bring this gist in' line, e.g.

    'from gigahost import Gigahost; gh = Gigahost.from_env()' or
    'import gigahost' when there's no public class/usage to show.

    Shared by build_instructions() and introspect() so the always-loaded
    instructions and the on-demand repld://gists/{name} resource can't
    show different (or no) import advice for the same gist.
    """
    sig = signature(name)
    usage = usage_for(name)
    if usage and sig:
        class_name = sig.split("(")[0]
        return f"from {name} import {class_name}; {usage}"
    if usage:
        return f"import {name}; {usage}"
    if sig:
        return f"from {name} import {sig}"
    return f"import {name}"


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


def is_public_gist_file(p: Path) -> bool:
    """A gist file is public unless its name starts with an underscore."""
    return not p.name.startswith("_")


def _iter_gist_files(*, include_private: bool = False):
    """Yield .py gist paths: installed dirs first, then linked.

    Deduped by stem so a local gist shadows a linked one of the same name, and
    stale linked paths are skipped.

    Private (underscore-prefixed) files are excluded by default: they're
    importable — the directory is on sys.path — but deliberately not *gists*,
    so they must stay out of the MCP instructions, tool discovery, and name
    hints. ``include_private=True`` is for the consumers that care about every
    file a kernel here can import, regardless of whether it's exposed as a
    gist: dependency scanning and ``repld gist lint``.
    """
    seen: set[str] = set()
    for d in _installed_dirs:
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.py")):
            if (not include_private and not is_public_gist_file(p)) or p.stem in seen:
                continue
            seen.add(p.stem)
            yield p
    for name, p in gist_links.linked_items():
        # link_targets() co-links siblings without a public/private check, so a
        # private can land in the manifest; it gets the same treatment here as
        # a local one. Imports are unaffected either way — _GistFinder resolves
        # through the gist_links._linked overlay, not through this iterator.
        if name in seen or (not include_private and not is_public_gist_file(p)):
            continue
        seen.add(name)
        yield p


def _declared_tools(p: Path) -> list[str] | None:
    """AST-only (no import) list of tool names declared by gist *p*.

    Names come from ``_tool_*`` function definitions with the prefix stripped;
    None if the file doesn't parse. Kept AST-only so ``tools/list`` can be
    answered without importing every gist in the project.
    """
    tree = _parse(p)
    if tree is None:
        return None
    return [
        node.name[len("_tool_") :]
        for node in ast.iter_child_nodes(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("_tool_")
    ]


def _annotation_parts(annotation) -> tuple[object, str | None]:
    """Split ``Annotated[T, "description"]`` into ``(T, description)``.

    Bare-string metadata rather than a ``Field``-style object: repld is stdlib
    only, so a plain ``str`` is the analogue of what FastMCP and pydantic spell
    ``Annotated[str, Field(description=...)]``. Metadata that isn't a string is
    ignored rather than rejected, so an annotation carrying something else for
    another consumer still resolves its type here.

    This is the only way a gist can describe a *parameter*: the docstring's
    first line becomes the tool description and there is nowhere else to say
    what a date format or an id refers to.
    """
    if typing.get_origin(annotation) is not typing.Annotated:
        return annotation, None
    base, *meta = typing.get_args(annotation)
    return base, next((m for m in meta if isinstance(m, str)), None)


def _resolve_json_type(annotation) -> str | None:
    """Map a parameter annotation to a JSON Schema type, unwrapping
    ``Annotated[X, ...]``, ``X | None`` / ``Optional[X]`` to the non-None arm,
    and parameterized generics (``list[str]``, ``dict[str, int]``) to their
    base type. None if unmapped."""
    mapped = _TYPE_MAP.get(annotation)
    if mapped is not None:
        return mapped
    origin = typing.get_origin(annotation)
    # Also handled by `_annotation_parts` before this is called; repeated here
    # so a nested form like `Optional[Annotated[str, "..."]]` still resolves
    # its type instead of falling through to the unmapped-type warning.
    if origin is typing.Annotated:
        return _resolve_json_type(typing.get_args(annotation)[0])
    if origin is typing.Union or origin is types.UnionType:
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return _resolve_json_type(args[0])
        return None
    if origin is not None:
        return _TYPE_MAP.get(origin)
    return None


def _schema_from_signature(func, tool_name: str) -> dict:
    """Build an MCP tool schema dict from a function's signature + docstring."""
    sig = inspect.signature(func)
    doc = inspect.getdoc(func)
    description = _first_line(doc) or tool_name

    # Resolved hints, not the raw signature: under `from __future__ import
    # annotations` (PEP 563) every annotation is a *string*, so `int` stops
    # mapping to "integer" and an `Annotated[...]` wrapper is invisible — a gist
    # with that import silently got every param typed "string" with an
    # unmapped-type warning, and no parameter descriptions at all.
    # `include_extras=True` is what keeps Annotated intact rather than
    # stripping it to the bare type.
    try:
        hints = typing.get_type_hints(func, include_extras=True)
    except Exception:
        # Unresolvable forward reference, or a name the module no longer has.
        # Degrade to the raw annotations rather than dropping the whole tool.
        hints = {}

    properties: dict[str, dict] = {}
    required: list[str] = []
    for pname, param in sig.parameters.items():
        annotation, param_doc = _annotation_parts(hints.get(pname, param.annotation))
        json_type = _resolve_json_type(annotation)
        if json_type is None:
            if annotation is not inspect.Parameter.empty:
                _warn_once(
                    f"{tool_name}:{pname}:type",
                    f"repld: tool '{tool_name}' param '{pname}' has unmapped "
                    f"type {annotation!r} — treating as string",
                )
            json_type = "string"
        prop: dict = {"type": json_type}
        if param_doc:
            prop["description"] = param_doc
        if param.default is not inspect.Parameter.empty:
            prop["default"] = param.default
        else:
            required.append(pname)
        properties[pname] = prop

    schema: dict = {
        "name": tool_name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
        },
    }
    if required:
        schema["inputSchema"]["required"] = required
    return schema


def _import_gist(p: Path):
    """Import (or reload) the gist module at *p*, returning the module object.

    Registers the gist even though this bypasses builtins.__import__ (and
    thus _GistImportHook) — tool-only gists are never `import`ed by user
    code, so this is the only chokepoint where they'd otherwise be missed.
    """
    mod_name = p.stem
    _check_reload(mod_name)
    mod = importlib.import_module(mod_name)
    _register(mod_name)
    return mod


def _try_import_gist(p: Path):
    """`_import_gist`, warning once and returning None instead of raising."""
    try:
        return _import_gist(p)
    except Exception as exc:
        _warn_once(f"{p}:import", f"repld: {p.name}: failed to import: {exc}")
        return None


def scan_tools() -> list[dict]:
    """Scan gist files for ``_tool_*`` functions. Returns inferred tool schemas.

    Schemas come from ``inspect.signature`` plus the docstring's first line, so
    the owning gist has to be imported. A gist that fails to import or whose
    signature can't be inspected is skipped with a warning rather than crashing
    the scan (and with it, ``tools/list`` / ``initialize``).
    """
    results: list[dict] = []
    seen: set[str] = set()
    for p in _iter_gist_files():
        declared = _declared_tools(p)
        if not declared:
            continue
        mod = _try_import_gist(p)
        if mod is None:
            continue
        for tname in declared:
            if tname in seen:
                continue
            func = getattr(mod, f"_tool_{tname}", None)
            if func is None:
                continue
            try:
                schema = _schema_from_signature(func, tname)
            except Exception as exc:
                _warn_once(
                    f"{p}:_tool_{tname}", f"repld: {p.name}: _tool_{tname}: {exc}"
                )
                continue
            seen.add(tname)
            results.append(schema)
    return results


def resolve_tool(name: str) -> Callable | None:
    """Import the gist that declares *name* and return its ``_tool_*`` handler.

    Called with keyword arguments matching the inferred schema. Returns
    ``None`` if no gist claims the tool. Raises ``AttributeError`` if a gist
    declares the tool but has no matching handler function.
    """
    for p in _iter_gist_files():
        declared = _declared_tools(p)
        if declared is None or name not in declared:
            continue
        mod = _try_import_gist(p)
        if mod is None:
            continue
        handler = getattr(mod, f"_tool_{name}", None)
        if handler is None:
            raise AttributeError(
                f"gist '{p.stem}' declares tool '{name}' "
                f"but has no _tool_{name}() handler"
            )
        return handler
    return None


def install(dirs: list[Path]) -> None:
    """Add gist directories to sys.path and install the auto-reload finder."""
    import builtins

    global _installed_dirs
    _installed_dirs = dirs

    # Tool-mode deps dir: gist deps installed via --target land here.
    gist_deps.ensure_deps_on_path()

    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
        s = str(d)
        if s not in sys.path:
            sys.path.insert(0, s)

    # Install the finder at the front of sys.meta_path. On repeat calls
    # (different dirs), update the existing finder in place instead of
    # skipping — otherwise real imports would keep resolving against the
    # first call's dirs while _installed_dirs (and everything derived from
    # it, e.g. _find_gist/_iter_gist_files) reflects the latest call.
    existing_finder = next(
        (f for f in sys.meta_path if isinstance(f, _GistFinder)), None
    )
    if existing_finder is not None:
        existing_finder._dirs = dirs
    else:
        sys.meta_path.insert(0, _GistFinder(dirs))

    # Wrap builtins.__import__ to intercept stale-module eviction
    # Guard against double-wrapping
    if not isinstance(builtins.__import__, _GistImportHook):
        builtins.__import__ = _GistImportHook(builtins.__import__)

    # Load cross-project links from the project gist dir's manifest.
    gist_links._load_links(Path.cwd() / "gists")
