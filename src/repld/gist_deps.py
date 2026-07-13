"""Gist dependency management — __repld_deps__ scanning + interactive install.

Gists declare external dependencies via `__repld_deps__ = ["httpx>=0.27"]`
(or "." for the gist's own project as an editable install). scan_deps()
AST-scans gist files at boot; install_deps() prompts on the real tty and
installs into the current venv (or a --target dir when repld runs as a uv
tool) via `uv pip install`.

Shares the parse cache and file iterator with gists.py; the two modules
import each other and access attributes at call time (never
`from x import y`), which keeps the cycle safe.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path

from . import gists
from .ipc import tty_prompt

_VERSION_SPECIFIERS = {">=", "<=", "==", "!=", "~=", ">", "<"}


def _parse_pkg_name(req: str) -> str:
    """Extract the base package name from a PEP 508 requirement string."""
    # Split at the earliest-occurring specifier — _VERSION_SPECIFIERS is a
    # set, so iteration order must not decide the split point (multi-clause
    # requirements like "foo>=1.0,!=1.2" would truncate wrongly).
    positions = [req.index(spec) for spec in _VERSION_SPECIFIERS if spec in req]
    name = req[: min(positions)] if positions else req
    # Drop extras — "httpx[http2]" is not an importable module name.
    return name.split("[")[0].strip()


def _is_importable(name: str) -> bool:
    """Check if a package is importable. Tries the name as-is (covers most packages).

    find_spec raises ModuleNotFoundError for dotted names whose parent is
    missing (e.g. "ruamel.yaml") and ValueError for malformed names — treat
    both as "not importable" so a gist dep can never crash boot.
    """
    try:
        return importlib.util.find_spec(name.replace("-", "_")) is not None
    except (ImportError, ValueError):
        return False


@dataclass
class _DepInfo:
    requirement: str
    gists: list[str]
    editable: bool = False


_TOOL_DEPS_DIR = Path.home() / ".local" / "share" / "repld" / "deps"


def _is_tool_venv() -> bool:
    return "uv/tools/" in sys.prefix


def _read_project_name(pyproject: Path) -> str | None:
    """Read [project] name from pyproject.toml."""
    import tomllib

    try:
        data = tomllib.loads(pyproject.read_text("utf-8"))
        return data.get("project", {}).get("name")
    except Exception:
        return None


def _resolve_dot_dep(gist_path: Path) -> _DepInfo | None:
    """Resolve '.' dep to the source project's editable install path."""
    project_root = gist_path.parent.parent
    pyproject = project_root / "pyproject.toml"
    if not pyproject.is_file():
        print(
            f"repld: {gist_path.name}: '.' dep but "
            f"{project_root} has no pyproject.toml",
            file=sys.stderr,
        )
        return None
    pkg_name = _read_project_name(pyproject) or project_root.name
    if _is_importable(pkg_name):
        return None
    return _DepInfo(
        requirement=str(project_root),
        gists=[gist_path.stem],
        editable=True,
    )


def scan_deps(paths: list[Path] | None = None) -> list[_DepInfo]:
    """AST-scan gist files for __repld_deps__. Returns missing deps with their sources.

    Scans `paths` when given (used by `gist add` for just-linked files), else all
    local + linked gist files.
    """
    deps: dict[str, _DepInfo] = {}
    for p in paths if paths is not None else gists._iter_gist_files():
        tree = gists._parse(p)
        if tree is None:
            continue
        node = gists._dunder_value(tree, "__repld_deps__")
        if node is None:
            continue
        try:
            reqs = ast.literal_eval(node)
        except Exception:
            gists._warn_once(
                f"{p}:__repld_deps__",
                f"repld: {p.name}: malformed __repld_deps__ "
                f"(not a valid literal) — skipped",
            )
            continue
        if not isinstance(reqs, list):
            continue
        for req in reqs:
            req_str = str(req).strip()
            if req_str == ".":
                info = _resolve_dot_dep(p)
                if info is not None:
                    key = info.requirement
                    if key in deps:
                        deps[key].gists.append(p.stem)
                    else:
                        deps[key] = info
                continue
            pkg = _parse_pkg_name(req_str)
            if _is_importable(pkg):
                continue
            if pkg in deps:
                deps[pkg].gists.append(p.stem)
            else:
                deps[pkg] = _DepInfo(req_str, [p.stem])
    return list(deps.values())


def _tty_write(msg: str) -> None:
    """Write directly to the real stderr, bypassing _Tee."""
    w = sys.__stderr__
    if w is not None:
        w.write(msg)
        w.flush()


def _tty_input(prompt: str) -> str:
    """Prompt on real stderr, read from real stdin."""
    return tty_prompt(prompt) or ""


def _prompt_dep_selection(missing: list[_DepInfo]) -> list[_DepInfo]:
    """Prompt which deps to install. Empty list means install nothing."""
    n = len(missing)
    if n == 1:
        choice = _tty_input("\nInstall? [\033[1mY\033[0m/n]: ")
        return missing if choice in ("", "y", "yes") else []
    choice = _tty_input(f"\nInstall? [\033[1mY\033[0m/n] or pick \033[1m1-{n}\033[0m: ")
    if choice in ("", "y", "yes", "all"):
        return missing
    if choice in ("n", "no", "none"):
        return []
    indices = []
    for part in choice.replace(",", " ").split():
        try:
            idx = int(part) - 1
        except ValueError:
            continue
        if 0 <= idx < n:
            indices.append(idx)
    return [missing[i] for i in indices]


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
    for i, dep in enumerate(missing, 1):
        _tty_write(f"  {i}) {dep.requirement:<24} ({', '.join(dep.gists)})\n")

    try:
        selected = _prompt_dep_selection(missing)
    except (EOFError, KeyboardInterrupt):
        _tty_write("\n")
        return False
    if not selected:
        return False

    uv = shutil.which("uv")
    req_args: list[str] = []
    for d in selected:
        if d.editable:
            req_args.extend(["-e", d.requirement])
        else:
            req_args.append(d.requirement)

    if uv:
        if _is_tool_venv():
            _TOOL_DEPS_DIR.mkdir(parents=True, exist_ok=True)
            cmd = [uv, "pip", "install", "--target", str(_TOOL_DEPS_DIR), *req_args]
        else:
            cmd = [uv, "pip", "install", "--python", sys.executable, *req_args]
    else:
        cmd = [sys.executable, "-m", "pip", "install", *req_args]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        if _is_tool_venv() and str(_TOOL_DEPS_DIR) not in sys.path:
            sys.path.insert(0, str(_TOOL_DEPS_DIR))
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
