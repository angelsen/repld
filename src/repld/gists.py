"""Auto-reloading import finder for ~/.repld/gists/ and ./gists/."""

from __future__ import annotations

import ast
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

__all__ = [
    "install",
    "scan",
    "scan_tools",
    "resolve_tool",
    "signature",
    "registry",
    "registry_summary",
    "scan_deps",
    "install_deps",
    "read_links",
    "write_links",
    "link_targets",
    "add_link",
    "remove_link",
    "remove_stale_links",
]

# Module names managed by the gist finder (populated by _GistFinder)
_managed: dict[str, Path] = {}  # fullname → source .py path
_mtimes: dict[str, float] = {}  # fullname → last known mtime
_installed_dirs: list[Path] = []  # set by install()

# Cross-project linked gists: name → absolute source path. Populated from
# ./gists/.links at install() time; consulted by the finder + iterators after
# local dirs (so local gists always shadow a linked one of the same name).
_linked: dict[str, Path] = {}
_LINKS_FILENAME = ".links"

_REGISTRY_PATH = (
    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    / "repld"
    / "gist-registry.json"
)


def _register(name: str) -> None:
    """Record a gist import in the central registry. Best-effort, never raises."""
    try:
        src = _managed.get(name)
        if src is None:
            return
        _REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        reg: dict = {}
        if _REGISTRY_PATH.is_file():
            reg = json.loads(_REGISTRY_PATH.read_text("utf-8"))
        doc = _extract_doc(src)
        reg[name] = {
            "path": str(src),
            "description": doc,
            "project": str(Path.cwd()),
            "last_used": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        _REGISTRY_PATH.write_text(json.dumps(reg, indent=2) + "\n", "utf-8")
    except Exception:
        pass


def registry() -> dict:
    """Read the gist registry. Returns {name: {path, description, project, last_used}}."""
    if _REGISTRY_PATH.is_file():
        return json.loads(_REGISTRY_PATH.read_text("utf-8"))
    return {}


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


# ---------------------------------------------------------------------------
# Cross-project links (./gists/.links manifest)
# ---------------------------------------------------------------------------


def read_links(gists_dir: Path) -> dict[str, str]:
    """Read the link manifest. Returns {name: abspath}. Best-effort → {} on error."""
    path = gists_dir / _LINKS_FILENAME
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text("utf-8"))
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except Exception:
        pass
    return {}


def write_links(gists_dir: Path, links: dict[str, str]) -> None:
    """Write the link manifest (pretty JSON, name-sorted)."""
    gists_dir.mkdir(parents=True, exist_ok=True)
    path = gists_dir / _LINKS_FILENAME
    ordered = {k: links[k] for k in sorted(links)}
    path.write_text(json.dumps(ordered, indent=2) + "\n", "utf-8")


def _load_links(gists_dir: Path) -> None:
    """Populate the live _linked overlay from the manifest.

    Skips (with a stderr warning) any entry whose path no longer exists; never
    rewrites the manifest — it is committed, and silently editing it would dirty
    the working tree. Use `repld gist rm --stale` to drop dead links.
    """
    _linked.clear()
    for name, raw in read_links(gists_dir).items():
        p = Path(raw)
        if p.is_file():
            _linked[name] = p
        else:
            print(
                f"repld: linked gist '{name}' path gone: {raw} (repld gist rm {name})",
                file=sys.stderr,
            )


def _sibling_imports(path: Path) -> set[str]:
    """Top-level names imported by `path` that exist as `<name>.py` beside it.

    Same-directory match = sibling gist; everything else is stdlib/third-party.
    """
    siblings: set[str] = set()
    try:
        tree = ast.parse(path.read_text("utf-8"))
    except Exception:
        return siblings
    src_dir = path.parent
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module.split(".")[0]]
        for n in names:
            if n != path.stem and (src_dir / f"{n}.py").is_file():
                siblings.add(n)
    return siblings


def link_targets(name: str) -> list[tuple[str, Path]]:
    """Resolve `name` via the registry + transitive same-dir sibling imports.

    Returns [(name, path), ...] — the full set that must be linked for `name` to
    import. Raises LookupError (listing known projects) if `name` isn't registered.
    """
    reg = registry()
    if name not in reg:
        projects = sorted({v.get("project", "?") for v in reg.values()})
        raise LookupError(
            f"gist '{name}' is not registered. Known projects:\n  "
            + "\n  ".join(projects)
        )
    resolved: dict[str, Path] = {}
    queue = [name]
    while queue:
        cur = queue.pop()
        if cur in resolved:
            continue
        entry = reg.get(cur)
        # Siblings may not be registered themselves — fall back to a path beside
        # an already-resolved gist.
        if entry is not None:
            p = Path(entry["path"])
        else:
            p = next((rp.parent / f"{cur}.py" for rp in resolved.values()), Path())
        if not p.is_file():
            continue
        resolved[cur] = p
        queue.extend(_sibling_imports(p) - resolved.keys())
    return list(resolved.items())


def add_link(name: str, gists_dir: Path) -> list[tuple[str, Path]]:
    """Link `name` (and its siblings) into gists_dir's manifest.

    Refuses on local collision — a target already present in ./gists or
    ~/.repld/gists, or resolving to a path inside this project. Returns the newly
    linked (name, path) pairs.
    """
    targets = link_targets(name)
    project_root = gists_dir.parent.resolve()
    for tname, tpath in targets:
        if (gists_dir / f"{tname}.py").is_file():
            raise FileExistsError(
                f"'{tname}' already exists locally: {gists_dir / f'{tname}.py'}"
            )
        global_gist = Path.home() / ".repld" / "gists" / f"{tname}.py"
        if global_gist.is_file():
            raise FileExistsError(f"'{tname}' already exists globally: {global_gist}")
        if project_root in tpath.resolve().parents:
            raise FileExistsError(f"'{tname}' already lives in this project: {tpath}")
    links = read_links(gists_dir)
    for tname, tpath in targets:
        links[tname] = str(tpath.resolve())
    write_links(gists_dir, links)
    return targets


def remove_link(name: str, gists_dir: Path) -> bool:
    """Drop `name` from the manifest (works on stale names). Returns True if removed.

    Leaves siblings in place — they may be shared with other linked gists.
    """
    links = read_links(gists_dir)
    if name not in links:
        return False
    del links[name]
    write_links(gists_dir, links)
    return True


def remove_stale_links(gists_dir: Path) -> list[str]:
    """Drop every manifest entry whose path is gone. Returns the removed names."""
    links = read_links(gists_dir)
    stale = [n for n, p in links.items() if not Path(p).is_file()]
    if stale:
        for n in stale:
            del links[n]
        write_links(gists_dir, links)
    return stale


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
        # Cross-project linked gist (exact name only — local dirs win above).
        linked = _linked.get(fullname)
        if linked is not None and linked.is_file():
            _managed[fullname] = linked
            _mtimes[fullname] = linked.stat().st_mtime
            return importlib.util.spec_from_file_location(fullname, linked)
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

        # Auto-inject API summary on gist import + register in central registry.
        if top in _managed:
            _register(top)
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
    """Resolve gist name to file path (local dirs first, then linked)."""
    for d in _installed_dirs:
        p = d / f"{name}.py"
        if p.is_file():
            return p
    linked = _linked.get(name)
    if linked is not None and linked.is_file():
        return linked
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
    return signature_for_path(path) if path else ""


def signature_for_path(path: Path) -> str:
    """Like signature(), but for a path already in hand (no _installed_dirs lookup)."""
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
    """Yield non-private .py gist paths: installed dirs first, then linked.

    Deduped by stem so a local gist shadows a linked one of the same name, and
    stale linked paths are skipped.
    """
    seen: set[str] = set()
    for d in _installed_dirs:
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.py")):
            if p.name.startswith("_") or p.stem in seen:
                continue
            seen.add(p.stem)
            yield p
    for name, p in sorted(_linked.items()):
        if name in seen or not p.is_file():
            continue
        seen.add(name)
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


# ---------------------------------------------------------------------------
# Dependency management
# ---------------------------------------------------------------------------

_VERSION_SPECIFIERS = {">=", "<=", "==", "!=", "~=", ">", "<"}


def _parse_pkg_name(req: str) -> str:
    """Extract the base package name from a PEP 508 requirement string."""
    for spec in _VERSION_SPECIFIERS:
        if spec in req:
            return req[: req.index(spec)].strip()
    return req.strip()


def _is_importable(name: str) -> bool:
    """Check if a package is importable. Tries the name as-is (covers most packages)."""
    return importlib.util.find_spec(name.replace("-", "_")) is not None


@dataclass
class _DepInfo:
    requirement: str
    gists: list[str]


def scan_deps(paths: list[Path] | None = None) -> list[_DepInfo]:
    """AST-scan gist files for __repld_deps__. Returns missing deps with their sources.

    Scans `paths` when given (used by `gist add` for just-linked files), else all
    local + linked gist files.
    """
    deps: dict[str, _DepInfo] = {}
    for p in paths if paths is not None else _iter_gist_files():
        try:
            tree = ast.parse(p.read_text("utf-8"))
        except Exception:
            continue
        for node in ast.iter_child_nodes(tree):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "__repld_deps__"
            ):
                try:
                    reqs = ast.literal_eval(ast.unparse(node.value))
                except Exception:
                    continue
                if not isinstance(reqs, list):
                    continue
                for req in reqs:
                    pkg = _parse_pkg_name(str(req))
                    if _is_importable(pkg):
                        continue
                    if pkg in deps:
                        deps[pkg].gists.append(p.stem)
                    else:
                        deps[pkg] = _DepInfo(str(req), [p.stem])
    return list(deps.values())


def _tty_write(msg: str) -> None:
    """Write directly to the real stderr, bypassing _Tee."""
    w = sys.__stderr__
    if w is not None:
        w.write(msg)
        w.flush()


def _tty_input(prompt: str) -> str:
    """Prompt on real stderr, read from real stdin."""
    _tty_write(prompt)
    stdin = sys.__stdin__
    assert stdin is not None
    return stdin.readline().strip().lower()


def install_deps(missing: list[_DepInfo]) -> bool:
    """Prompt user and install missing deps. Returns True if anything was installed."""
    import shutil
    import subprocess

    if not missing:
        return False

    if sys.prefix == sys.base_prefix:
        _tty_write("\033[33m[repld] gists need packages not in system Python:\n")
        for dep in missing:
            _tty_write(f"  {dep.requirement:<24} ({', '.join(dep.gists)})\n")
        _tty_write("  use: uv tool install repld-tool --with <pkg>\033[0m\n")
        return False

    _tty_write("\033[36m[repld]\033[0m missing gist deps:\n")
    n = len(missing)
    for i, dep in enumerate(missing, 1):
        _tty_write(f"  {i}) {dep.requirement:<24} ({', '.join(dep.gists)})\n")

    try:
        if n == 1:
            choice = _tty_input("\nInstall? [\033[1mY\033[0m/n]: ")
            if choice in ("", "y", "yes"):
                selected = missing
            else:
                return False
        else:
            choice = _tty_input(
                f"\nInstall? [\033[1mY\033[0m/n] or pick \033[1m1-{n}\033[0m: "
            )
            if choice in ("", "y", "yes", "all"):
                selected = missing
            elif choice in ("n", "no", "none"):
                return False
            else:
                indices = []
                for part in choice.replace(",", " ").split():
                    try:
                        idx = int(part) - 1
                        if 0 <= idx < n:
                            indices.append(idx)
                    except ValueError:
                        pass
                selected = [missing[i] for i in indices]
                if not selected:
                    return False
    except (EOFError, KeyboardInterrupt):
        _tty_write("\n")
        return False

    reqs = [d.requirement for d in selected]
    uv = shutil.which("uv")
    cmd = (
        [uv, "pip", "install", "--python", sys.executable, *reqs]
        if uv
        else [sys.executable, "-m", "pip", "install", *reqs]
    )

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        importlib.invalidate_caches()
        count = len(selected)
        _tty_write(
            f"  \033[32m✓\033[0m installed {count} package{'s' * (count != 1)}\n"
        )
        return True

    _tty_write("  \033[31m✗\033[0m install failed:\n")
    for line in result.stderr.strip().splitlines()[-5:]:
        _tty_write(f"    {line}\n")
    return False


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

    # Load cross-project links from the project gist dir's manifest.
    _load_links(Path.cwd() / "gists")
