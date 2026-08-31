"""Gist dependency management — __repld_deps__ scanning + interactive install.

Gists declare external dependencies via `__repld_deps__ = ["httpx>=0.27"]`
(or "." for the gist's own project as an editable install, or
"path:some/dir" to prepend a local directory to sys.path — for vendored,
non-pip-installable code). scan_deps() AST-scans gist files at boot;
install_deps() prompts on the real tty and installs via `uv pip install
--target`. Path deps skip install_deps() entirely — resolved and added to
sys.path directly by scan_deps() since there's nothing to install.

Installing is gated on there actually being a terminal to ask at. A headless
kernel — which is how every auto-spawned one runs — reports what's missing and
declines, rather than reading its /dev/null stdin as a yes.

Installs go to a shared directory under ~/.local/share/repld/deps, never into
whatever venv happens to be active. Two reasons. A project venv is the wrong
home — `uv sync` prunes anything not in the project's own lockfile, so the
boot prompt would come back after every sync. And once the kernel binds itself
to a project (see bind.py), the active prefix is an ephemeral `uv run` overlay
that is a *different* temp directory on every invocation, so an install there
is gone before the next boot.

The directory is keyed by interpreter version (deps/py3.12, deps/py3.13):
wheels with compiled extensions are built for one minor version, so a single
shared directory could not be put on the path of a kernel bound to a project
pinning a different Python.

Being shared across projects, each install must be resolved against everything
ever installed there — not just the newly-missing packages — or independent
installs can leave mutually-incompatible transitive pins on disk.
`_read_manifest()` / `_write_manifest()` track the accumulated requirement set
for that purpose.

Shares the parse cache and file iterator with gists.py; the two modules
import each other and access attributes at call time (never
`from x import y`), which keeps the cycle safe.
"""

from __future__ import annotations

import ast
import importlib
import importlib.metadata
import importlib.util
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import gists
from .gates import stdin_owned, tty_prompt, yes_by_default

_VERSION_SPECIFIERS = {">=", "<=", "==", "!=", "~=", ">", "<"}

# Wall-clock ceiling on one `uv pip install` / `pip install`. See install_deps.
_INSTALL_TIMEOUT = 600.0


def _parse_pkg_name(req: str) -> str:
    """Extract the base package name from a PEP 508 requirement string."""
    # Split at the earliest-occurring specifier — _VERSION_SPECIFIERS is a
    # set, so iteration order must not decide the split point (multi-clause
    # requirements like "foo>=1.0,!=1.2" would truncate wrongly).
    positions = [req.index(spec) for spec in _VERSION_SPECIFIERS if spec in req]
    name = req[: min(positions)] if positions else req
    # Drop extras — "httpx[http2]" is not an importable module name.
    return name.split("[")[0].strip()


_dist_to_import: dict[str, list[str]] | None = None


def _distribution_import_names(dist_name: str) -> list[str]:
    """Import names an installed distribution actually provides, e.g.
    'pyyaml' -> ['yaml', '_yaml'] — the PyPI project name and the importable
    module name are unrelated for a fair number of packages (pyyaml/yaml,
    beautifulsoup4/bs4, ...), so name.replace("-", "_") alone can't find them.

    Caches importlib.metadata's reverse mapping — scanning installed-package
    metadata on every _is_importable() call would be wasteful. Reset after
    install_deps() actually installs something, so a dep installed mid-session
    is picked up without needing a kernel restart.
    """
    global _dist_to_import
    if _dist_to_import is None:
        reverse: dict[str, list[str]] = {}
        for imp, dists in importlib.metadata.packages_distributions().items():
            for dist in dists:
                reverse.setdefault(dist.lower(), []).append(imp)
        _dist_to_import = reverse
    return _dist_to_import.get(dist_name.lower(), [])


def _is_importable(name: str) -> bool:
    """Check if a package is importable. Tries the name as-is (covers most
    packages), then falls back to the installed distribution's actual
    top-level import name(s) — otherwise a dep like "pyyaml" (imports as
    "yaml") would report "missing" forever, install or not.

    find_spec raises ModuleNotFoundError for dotted names whose parent is
    missing (e.g. "ruamel.yaml") and ValueError for malformed names — treat
    both as "not importable" so a gist dep can never crash boot.
    """
    try:
        if importlib.util.find_spec(name.replace("-", "_")) is not None:
            return True
    except (ImportError, ValueError):
        pass
    for import_name in _distribution_import_names(name):
        try:
            if importlib.util.find_spec(import_name) is not None:
                return True
        except (ImportError, ValueError):
            continue
    return False


@dataclass
class _DepInfo:
    requirement: str
    gists: list[str]
    editable: bool = False


_DEPS_ROOT = Path.home() / ".local" / "share" / "repld" / "deps"


def _deps_dir() -> Path:
    """Shared install target for this interpreter, e.g. …/deps/py3.12.

    Computed rather than a constant so it follows a re-exec into a different
    Python, and so tests can point _DEPS_ROOT elsewhere.
    """
    return _DEPS_ROOT / f"py{sys.version_info[0]}.{sys.version_info[1]}"


def _manifest_path() -> Path:
    return _deps_dir() / ".repld-manifest.txt"


# Resolved 'path:' dep directories (as strings, matching what's inserted
# into sys.path). gists.py's _GistFinder checks membership here to extend
# auto-reload to path deps without conflating them with real gist dirs.
_path_dep_dirs: set[str] = set()


def _read_manifest() -> dict[str, str]:
    """Read this interpreter's accumulated requirements manifest.

    Returns {pkg_name: requirement_string} for regular deps, keyed by
    normalized package name so a repeat install of the same package
    replaces its old specifier instead of duplicating. Editable deps are
    keyed by their full "-e <path>" entry. Empty dict if no manifest yet.
    """
    path = _manifest_path()
    if not path.is_file():
        return {}
    entries: dict[str, str] = {}
    for line in path.read_text("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-e "):
            entries[line] = line
        else:
            entries[_parse_pkg_name(line)] = line
    return entries


def _write_manifest(entries: dict[str, str]) -> None:
    """Persist the requirements manifest. Call only after a successful
    install — an install failure must not desync the manifest from what's
    actually on disk in the deps dir.
    """
    _deps_dir().mkdir(parents=True, exist_ok=True)
    _manifest_path().write_text(
        "\n".join(sorted(entries.values())) + "\n", encoding="utf-8"
    )


def ensure_deps_on_path() -> None:
    """Put this interpreter's shared gist-deps dir on sys.path.

    Ungated on purpose: the directory is keyed by interpreter version, so its
    contents are always loadable here. Gating on a prefix test would hide every
    installed gist dep from a kernel bound to a project, since `bind.py`
    re-execs into a `uv run` overlay whose `sys.prefix` is a cache directory.

    Appended, not prepended: the project's own packages must win. A gist
    declaring `httpx>=0.27` should not shadow the version the project
    resolved — these fill gaps, they don't override.

    `site.addsitedir` rather than `sys.path.append`, because an editable dep
    installs as nothing but a `.pth`. `__repld_deps__ = ["."]` becomes
    `uv pip install --target … -e <project>`, which writes `<pkg>.pth` and a
    dist-info into the target and no package directory at all — a plain
    append leaves it unimportable. So the one dep form that exists purely for
    the *unbound* case was the form that didn't work there. addsitedir still
    appends, so the shadowing rule above is unaffected.
    """
    d = _deps_dir()
    if d.is_dir() and str(d) not in sys.path:
        import site

        site.addsitedir(str(d))


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


def resolve_path_target(rel_path: str, gist_path: Path) -> Path:
    """Resolve a 'path:' dep string to an absolute directory.

    Relative paths resolve from the gist's project root (same convention as
    '.'); absolute paths pass through as-is. Pure resolution — no existence
    check, no sys.path mutation. Shared with gist_lint's deps rule so both
    agree on where a path dep points.
    """
    p = Path(rel_path.strip().rstrip("/"))
    if not p.is_absolute():
        p = gist_path.parent.parent / p
    return p.resolve()


def _resolve_path_dep(rel_path: str, gist_path: Path) -> Path | None:
    """Resolve a 'path:' dep and prepend it to sys.path — no pip install.

    Missing directories fail loudly (submodule-init hint) instead of
    surfacing as a bare ModuleNotFoundError the first time the gist runs.
    """
    resolved = resolve_path_target(rel_path, gist_path)
    if not resolved.is_dir():
        gists._warn_once(
            f"{gist_path}:path:{rel_path}",
            f"repld: {gist_path.name}: path dep '{rel_path}' not found at "
            f"{resolved} — did you run `git submodule update --init`?",
        )
        return None
    resolved_str = str(resolved)
    _path_dep_dirs.add(resolved_str)
    if resolved_str in sys.path:
        return resolved
    sys.path.insert(0, resolved_str)
    return resolved


# The three forms a `__repld_deps__` entry can take.
DEP_DOT = "dot"    # "."       — the gist's own project, as an editable install
DEP_PATH = "path"  # "path:X"  — a local directory to prepend to sys.path
DEP_PKG = "pkg"    # anything else — a PEP 508 requirement string


def _warn_malformed(gist_path: Path, suffix: str, reason: str) -> None:
    gists._warn_once(
        f"{gist_path}:__repld_deps__{suffix}",
        f"repld: {gist_path.name}: {reason} — skipped",
    )


def classify_deps(
    tree: ast.Module,
    gist_path: Path,
    *,
    on_malformed: Callable[[Path, str, str], None] | None = None,
) -> list[tuple[str, str]]:
    """A gist's `__repld_deps__` entries as (form, value) pairs.

    `value` is the entry with its form marker stripped: "" for `DEP_DOT`, the
    path text for `DEP_PATH`, the verbatim requirement for `DEP_PKG`.

    The installer and the linter read the same dunder and recognise the same
    three forms, diverging only in what they do with each — so the taxonomy
    is written once, here. It used to be spelled out in both, which meant a
    fourth form would be understood by whichever module remembered to add it
    and silently misreported by the other. `on_malformed` is called rather
    than raising, because the two disagree there too: the installer warns,
    the linter treats the file as declaring nothing.
    """
    node = gists._dunder_value(tree, "__repld_deps__")
    if node is None:
        return []
    try:
        reqs = ast.literal_eval(node)
    except Exception:
        if on_malformed:
            on_malformed(
                gist_path, "", "malformed __repld_deps__ (not a valid literal)"
            )
        return []
    if not isinstance(reqs, list):
        if on_malformed:
            on_malformed(
                gist_path,
                ":type",
                f"__repld_deps__ is {type(reqs).__name__}, expected a list",
            )
        return []
    forms: list[tuple[str, str]] = []
    for req in reqs:
        req_str = str(req).strip()
        if req_str == ".":
            forms.append((DEP_DOT, ""))
        elif req_str.startswith("path:"):
            forms.append((DEP_PATH, req_str[len("path:") :]))
        else:
            forms.append((DEP_PKG, req_str))
    return forms


def scan_deps(paths: list[Path] | None = None) -> list[_DepInfo]:
    """AST-scan gist files for __repld_deps__. Returns missing deps with their sources.

    Scans `paths` when given (used by `gist add` for just-linked files), else all
    local + linked gist files, private ones included.
    """
    deps: dict[str, _DepInfo] = {}
    # include_private: a private helper is imported by its siblings at runtime,
    # so its __repld_deps__ is a real declaration. Skipping it meant the deps
    # never installed and the importing sibling died on a bare
    # ModuleNotFoundError, with nothing pointing at the file that needed them.
    for p in (
        paths if paths is not None else gists._iter_gist_files(include_private=True)
    ):
        tree = gists._parse(p)
        if tree is None:
            continue
        for form, value in classify_deps(tree, p, on_malformed=_warn_malformed):
            if form == DEP_DOT:
                info = _resolve_dot_dep(p)
                if info is not None:
                    key = info.requirement
                    if key in deps:
                        deps[key].gists.append(p.stem)
                    else:
                        deps[key] = info
            elif form == DEP_PATH:
                _resolve_path_dep(value, p)
            else:
                pkg = _parse_pkg_name(value)
                if _is_importable(pkg):
                    continue
                if pkg in deps:
                    deps[pkg].gists.append(p.stem)
                else:
                    deps[pkg] = _DepInfo(value, [p.stem])
    return list(deps.values())


def _tty_write(msg: str) -> None:
    """Write directly to the real stderr, bypassing _Tee."""
    w = sys.__stderr__
    if w is not None:
        w.write(msg)
        w.flush()


def _can_prompt() -> bool:
    """Whether there is a real terminal to put a question on, and it is free.

    Mirrors `tty_prompt`'s own two guards so the caller can print a useful
    explanation instead of falling into `_prompt_dep_selection` and reading a
    None as a decline. `stdin_owned()` is the one that matters after boot:
    `install_deps` is reached from `gists._check_reload` / `_scan_new_deps`
    inside a user cell, where the display thread already holds stdin.
    """
    stdin = sys.__stdin__
    if stdin is None or stdin_owned():
        return False
    try:
        return stdin.isatty()
    except (OSError, ValueError):
        return False


def _prompt_dep_selection(missing: list[_DepInfo]) -> list[_DepInfo]:
    """Prompt which deps to install. Empty list means install nothing.

    `tty_prompt` returns None and "" distinctly, and the difference matters
    here: "" is a human pressing Enter at a `[Y/n]` prompt, i.e. consent, while
    None means there was nobody to ask.
    """
    n = len(missing)
    if n == 1:
        choice = tty_prompt("\nInstall? [\033[1mY\033[0m/n]: ")
        return missing if yes_by_default(choice) else []
    choice = tty_prompt(f"\nInstall? [\033[1mY\033[0m/n] or pick \033[1m1-{n}\033[0m: ")
    # "all" is this prompt's own word — it has a third answer shape the plain
    # [Y/n] callers don't — so it sits alongside the shared yes rather than in
    # it. Everything else routes through `gates.yes_by_default`.
    if yes_by_default(choice) or (choice or "").strip().lower() == "all":
        return missing
    if choice is None or choice.strip().lower() in ("n", "no", "none"):
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

    # No system-Python gate: deps go to `_deps_dir()` via `--target` +
    # `--python sys.executable`, which resolves with or without a venv. Refusing
    # when `sys.prefix == sys.base_prefix` would block a `pip install --user`
    # install from ever accepting the prompt, for no gain.

    _tty_write("\033[36m[repld]\033[0m missing gist deps:\n")
    for i, dep in enumerate(missing, 1):
        _tty_write(f"  {i}) {dep.requirement:<24} ({', '.join(dep.gists)})\n")

    # Installing is a human decision, so a kernel with nobody at its stdin
    # reports and declines. Every auto-spawned kernel is in exactly that
    # position — `repld gist add` links gists from other projects by absolute
    # path, so an unattended boot would otherwise resolve and install whatever
    # a linked gist's `__repld_deps__` happens to name.
    if not _can_prompt():
        # The prompt would have to be answered at *this process's* stdin, so
        # `repld exec` is no help — it's a separate process talking IPC, and
        # the install runs here. The two things that actually work are running
        # a kernel with a terminal, or installing into the shared dir by hand.
        #
        # Two different reasons land here and they need different advice. A
        # headless kernel has no terminal at all. A pane kernel has one, but
        # only until `run_display` starts reading it — and this is reached from
        # a gist import inside a user cell, i.e. always after that. Telling
        # someone staring at a pane to "run `repld` in a pane" reads as a bug in
        # the message; the boot scan is what actually re-asks.
        if stdin_owned():
            reason = (
                "  \033[33m⚠\033[0m the pane's stdin is reading gate answers "
                "— skipped.\n"
                "  `repld restart` to be asked at boot, or install them "
                "yourself:\n"
            )
        else:
            reason = (
                "  \033[33m⚠\033[0m no terminal to ask on — skipped.\n"
                "  run `repld` in a pane to be prompted, or install them "
                "yourself:\n"
            )
        _tty_write(
            reason
            + f"    uv pip install --target {_deps_dir()} --python {sys.executable} "
            + " ".join(d.requirement for d in missing)
            + "\n"
        )
        return False

    try:
        selected = _prompt_dep_selection(missing)
    except (EOFError, KeyboardInterrupt):
        _tty_write("\n")
        return False
    if not selected:
        return False

    uv = shutil.which("uv")
    target = _deps_dir()
    target.mkdir(parents=True, exist_ok=True)

    # The deps dir is shared across every project's gists. Passing only the
    # newly-selected deps would let the resolver work in isolation from what's
    # already installed there, silently skewing shared transitive pins (e.g.
    # pydantic-core) between unrelated installs. Merge into the accumulated
    # manifest and re-resolve the whole set every time instead.
    manifest = _read_manifest()
    for d in selected:
        if d.editable:
            key = f"-e {d.requirement}"
            manifest[key] = key
        else:
            manifest[_parse_pkg_name(d.requirement)] = d.requirement

    full_req_args: list[str] = []
    for entry in sorted(manifest.values()):
        if entry.startswith("-e "):
            full_req_args.extend(["-e", entry[3:]])
        else:
            full_req_args.append(entry)

    if uv:
        # --python pins resolution to the interpreter actually running this
        # kernel. Without it, uv resolves against its own default/preferred
        # Python, which can silently differ from sys.executable — a binary
        # wheel (cffi, cryptography, numpy, ...) built for the wrong ABI lands
        # in the deps dir and fails with a bare ModuleNotFoundError for its
        # compiled extension, not an obviously-version-related error.
        cmd = [
            uv,
            "pip",
            "install",
            "--target",
            str(target),
            "--python",
            sys.executable,
            *full_req_args,
        ]
    else:
        cmd = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--target",
            str(target),
            *full_req_args,
        ]

    # Bounded, unlike a shell install a human could Ctrl-C. Two of the three
    # call sites run from the gist import hook — i.e. on the kernel's asyncio
    # thread, mid-`exec` — so a resolver that wedges on a network stall takes
    # the whole kernel with it. Generous, because a cold cache building a
    # sdist (numpy, cryptography) legitimately takes minutes.
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_INSTALL_TIMEOUT, check=False
        )
    except subprocess.TimeoutExpired:
        _tty_write(
            f"  \033[31m✗\033[0m install timed out after {_INSTALL_TIMEOUT:.0f}s — "
            "run it by hand:\n    " + " ".join(cmd) + "\n"
        )
        return False
    if result.returncode == 0:
        _write_manifest(manifest)
        ensure_deps_on_path()
        importlib.invalidate_caches()
        global _dist_to_import
        _dist_to_import = None  # newly installed dists aren't in the cached map yet
        count = len(selected)
        _tty_write(
            f"  \033[32m✓\033[0m installed {count} package{'s' * (count != 1)}\n"
        )
        return True

    _tty_write("  \033[31m✗\033[0m install failed:\n")
    for line in result.stderr.strip().splitlines()[-5:]:
        _tty_write(f"    {line}\n")
    return False
