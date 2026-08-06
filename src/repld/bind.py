"""Binding the kernel to the project's virtualenv.

A repld installed as a uv tool has its interpreter fixed by the console
script's shebang, so by default the kernel runs under the *tool's* Python and
cannot import the project it is sitting in. This module decides which
interpreter it should have been, and whether the running one will do.

Its own module for the reason ``spawn.py`` is: the bridge is spawned once per
Claude Code session, so its import graph stays small. Stdlib only.

Two ways to reach the project's packages, and the choice between them is not a
preference — it's a hard constraint:

  * **Re-exec** under ``uv run``, which resolves repld for the project's
    interpreter. Correct always; costs a process replacement, so it only
    happens before the kernel has any state to lose.
  * **``site.addsitedir``** at runtime. Free, and it picks up editable installs'
    ``.pth`` finders — but *only sound when the interpreter minor version
    already matches*. A venv's compiled extensions are built for one version
    (``…cpython-312…so``); the rest are pure Python or stable-ABI (``abi3``).
    Splicing a mismatched venv onto ``sys.path`` therefore half-works: the
    pure-Python and abi3 packages import, the version-locked ones don't. A
    partial success is worse to debug than a clean refusal, so a mismatch must
    leave ``sys.path`` untouched and say so.

Nothing here restarts anything on its own. Rebinding a live kernel discards
variables, tickers and browser connections, which is the user's call to make.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Set across the re-exec. `uv run … repld bridge` re-enters the same entry
# point, so without a sentinel the bind check would fire again and fork-bomb.
SENTINEL = "REPLD_BOUND"


def project_venv(cwd: Path | None = None, *, ambient: bool = True) -> Path | None:
    """The project virtualenv: ``./.venv``, falling back to ``$VIRTUAL_ENV``.

    cwd wins over the ambient environment deliberately. Everything else in
    repld is derived from cwd (``paths.py``), while ``$VIRTUAL_ENV`` is
    inherited from whatever shell launched the client — so activating one
    project's venv and then opening a second project would otherwise bind that
    kernel to the first project's packages. ``$VIRTUAL_ENV`` is still honoured
    for projects whose venv doesn't sit at ``./.venv``.

    ``ambient=False`` drops that fallback, for callers asking about a *different*
    process's project rather than deciding their own binding. `$VIRTUAL_ENV`
    describes the shell that ran the command, so `repld status` inspecting a
    kernel elsewhere would otherwise compare that kernel's Python against a venv
    belonging to neither of them — the exact confusion cwd-precedence exists to
    prevent, arriving through the back door.

    A directory only counts with a ``pyvenv.cfg`` in it — otherwise a stray
    ``.venv/`` left behind by a failed create would look bindable.
    """
    local = (cwd or Path.cwd()) / ".venv"
    if (local / "pyvenv.cfg").is_file():
        return local
    env = os.environ.get("VIRTUAL_ENV") if ambient else None
    if env:
        p = Path(env)
        if (p / "pyvenv.cfg").is_file():
            return p
    return None


def venv_python_version(venv: Path) -> tuple[int, int] | None:
    """``(major, minor)`` for a venv, or None if it can't be determined.

    ``pyvenv.cfg`` spells it ``version_info`` when uv made the venv and
    ``version`` when the stdlib did; fall back to the ``lib/pythonX.Y``
    directory, which is authoritative for where packages actually live.
    """
    cfg = venv / "pyvenv.cfg"
    try:
        for line in cfg.read_text("utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip() in ("version_info", "version"):
                parts = value.strip().split(".")
                if len(parts) >= 2:
                    return int(parts[0]), int(parts[1])
    except (OSError, ValueError):
        pass
    for d in sorted(venv.glob("lib/python3.*")):
        try:
            return 3, int(d.name.removeprefix("python3."))
        except ValueError:
            continue
    return None


def site_packages(venv: Path) -> Path | None:
    """The venv's ``site-packages`` directory, if it exists."""
    version = venv_python_version(venv)
    if version is not None:
        p = venv / "lib" / f"python{version[0]}.{version[1]}" / "site-packages"
        if p.is_dir():
            return p
    for p in sorted(venv.glob("lib/python3.*/site-packages")):
        return p
    return None


def version_matches(venv: Path) -> bool:
    """Whether this interpreter can safely load the venv's compiled packages."""
    return venv_python_version(venv) == sys.version_info[:2]


def is_bound(venv: Path) -> bool:
    """Whether this process can already import the venv's packages.

    Asks the question that actually matters rather than comparing prefixes:
    under ``uv run --with``, ``sys.prefix`` is an ephemeral overlay in uv's
    cache, but the project's ``site-packages`` is spliced onto ``sys.path``
    verbatim — so it is on the path that decides imports either way.
    """
    sp = site_packages(venv)
    return sp is not None and str(sp) in sys.path


def describe(venv: Path | None) -> str:
    """One-line interpreter summary for status output and messages."""
    running = f"{sys.version_info[0]}.{sys.version_info[1]}"
    if venv is None:
        return f"Python {running} (no project venv)"
    version = venv_python_version(venv)
    target = f"{version[0]}.{version[1]}" if version else "?"
    if is_bound(venv):
        return f"Python {running}, bound to {venv}"
    return f"Python {running}, project venv is {target} ({venv}) — not bound"


def adopt(venv: Path) -> Path | None:
    """Splice a version-matched venv onto ``sys.path``.

    Returns the ``site-packages`` directory that was added, or None if it was
    refused — which for a version mismatch is the whole point: ``sys.path``
    must come back untouched rather than half-usable.

    ``site.addsitedir`` rather than ``sys.path.append`` so ``.pth`` files get
    processed; that is what makes an editable install of the project itself
    resolve to its source tree.
    """
    if not version_matches(venv):
        return None
    sp = site_packages(venv)
    if sp is None or str(sp) in sys.path:
        return None
    import importlib
    import site

    site.addsitedir(str(sp))
    # Python caches failed lookups; without this a module that was missing a
    # moment ago stays missing for the life of the process.
    importlib.invalidate_caches()
    return sp


def has_browser_extra() -> bool:
    """Whether *this* interpreter can import the browser stack.

    All three are load-bearing and imported eagerly, so the question is not
    "which one" but "are they all here" — `browser/png.py` pulls in PIL at
    module scope, and `kernel._inject_builtins` reads any one of them missing
    as the whole extra being absent.

    ``find_spec`` rather than an import: this runs on every bound start, and
    importing duckdb to find out whether we could is most of the cost the extra
    exists to avoid paying.
    """
    from importlib.util import find_spec

    for mod in ("duckdb", "websockets", "PIL"):
        try:
            if find_spec(mod) is None:
                return False
        except (ImportError, ValueError):
            return False
    return True


def uv_run_argv(argv: list[str]) -> list[str] | None:
    """The ``uv run`` command that relaunches ``repld <argv>`` under the
    project's interpreter, or None if uv isn't on PATH.

    Reuses ``relaunch._editable_path()`` so a local checkout keeps winning over
    the published package, exactly as it does for ``repld browser``.

    **The browser extra is carried across the re-exec, or the rebind silently
    takes it away.** ``uv run --with`` builds a fresh environment from the spec
    it is given; the interpreter we are leaving does not contribute to it. So a
    repld installed *with* the extra — the tool venv carrying duckdb,
    websockets and PIL — re-execs into an environment that has none of them,
    and the kernel comes up with no ``browser`` builtin, no browser MCP tools,
    and nothing anywhere saying why. The observable shape of that bug is the
    tell: browser worked in every project *without* a ``./.venv`` and in none
    of the projects with one, since only the latter rebind at all.

    Conditional on what we already have rather than unconditional, so a repld
    installed without the extra doesn't start resolving duckdb behind the
    user's back. The rule is that the re-exec must not *lose* a capability, not
    that it should acquire one.
    """
    import shutil

    from .relaunch import _editable_path

    uv = shutil.which("uv")
    if uv is None:
        return None
    extra = "[browser]" if has_browser_extra() else ""
    path = _editable_path()
    with_arg = (
        ["--with-editable", f"{path}{extra}"]
        if path
        else ["--with", f"repld-tool{extra}"]
    )
    return [uv, "run", *with_arg, "repld", *argv]


def rebind_exec(argv: list[str]) -> None:
    """Re-exec ``repld <argv>`` under the project venv. Returns only on failure.

    Callers run this before doing any work. Failures are non-fatal by design:
    an unbound kernel still works for everything that doesn't need the
    project's packages, so a missing uv or a venv we can't read must not stop
    repld from starting.
    """
    if os.environ.get(SENTINEL):
        return
    venv = project_venv()
    if venv is None or is_bound(venv):
        return
    cmd = uv_run_argv(argv)
    if cmd is None:
        print(
            "repld: project venv found but `uv` is not on PATH — running "
            f"unbound under Python {sys.version_info[0]}.{sys.version_info[1]}",
            file=sys.stderr,
        )
        return
    os.environ[SENTINEL] = "1"
    try:
        os.execvp(cmd[0], cmd)
    except OSError as e:
        os.environ.pop(SENTINEL, None)
        print(f"repld: could not bind to {venv}: {e}", file=sys.stderr)
