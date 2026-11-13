"""XDG runtime paths, the single source of truth for where kernel state lives.

Nothing repld writes at runtime lands in the project directory. Each kernel
gets a per-project subdirectory under ``$XDG_RUNTIME_DIR/repld/projects/``,
named ``{basename}-{sha256(realpath)[:8]}`` so it stays readable in `ls` while
two same-named projects still resolve apart.

One derivation rule for everything else: every runtime file is the socket path
with a different suffix. That keeps an explicit ``--socket`` / ``REPLD_SOCKET``
coherent: the lockfile, flock mutex, dashboard hint, and event log all follow
the socket wherever the caller pointed it.

    kernel.sock   unix-domain IPC socket
    kernel.lock   JSON {pid, socket_path, cwd, started_at, python, executable,
                  dashboard_port}
    kernel.flock  flock(2) mutex, never replaced, so the lock survives
                  atomic rewrites of kernel.lock
    kernel.dashboard  dashboard port + token + browser restore hint (0600)
    kernel.events     NDJSON event log
    kernel.cache      last-computed instructions/tools/resources (0600),
                      lets the bridge answer MCP discovery without a live
                      kernel; survives kernel death, superseded on next boot.
                      A copy under $XDG_STATE_HOME (`state_cache_for`) also
                      survives a reboot, the one thing that wipes the tmpfs.
                      `GLOBAL_CACHE` is the project-free variant (no gist
                      tools or resources) a project that never ran a kernel
                      falls back to.

Per-process scratch is the one exception: task spills, resource spills, and
browser screenshots belong to the process rather than to any project, so they
sit directly in RUNTIME_DIR. They all share a ``{pid}-...`` prefix, which is
what lets a booting kernel sweep everything a dead pid left behind in one pass
(``state.sweep_dead_pid_files``); nothing runs on the way out of a SIGKILL,
and under the /tmp fallback the leftovers would otherwise be permanent. The
same prefix lets a *live* kernel age out its own (``state.sweep_own_stale_files``,
driven by ``tasks._sweep_orphans``), which is the half boot-time sweeping can
never cover for a process that stays up for weeks. Keep the prefix on anything
new written here or neither rule will find it.

A dead kernel's ``PROJECTS_DIR/<slug>/`` directory itself is a third case
those two rules do not reach: they sweep files sitting flat in RUNTIME_DIR,
never the per-project subdirectories one level up. ``state.sweep_dead_project_dirs``
is that third rule: same boot-time pass, same liveness check via
``state.read_lock``, applied to ``kernel.lock`` instead of a ``{pid}-...``
filename.

Without ``XDG_RUNTIME_DIR`` (macOS, most containers, any env-scrubbed spawn)
the tree falls back to ``/tmp/repld-{uid}``. The uid suffix is load-bearing:
``/tmp`` is world-writable, so a shared unsuffixed name could be pre-created
and owned by another local user. ``ensure_runtime_dir()`` is what puts the
0700 gate over everything below.
"""

import hashlib
import os
import stat
import sys
from pathlib import Path

# -- Module-level constants --------------------------------------------------

_XDG = os.environ.get("XDG_RUNTIME_DIR")
RUNTIME_DIR = Path(_XDG) / "repld" if _XDG else Path("/tmp") / f"repld-{os.getuid()}"
PROJECTS_DIR = RUNTIME_DIR / "projects"
STATE_PROJECTS_DIR = (
    Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    / "repld"
    / "projects"
)

_ensured = False


# -- Runtime directory -------------------------------------------------------


def ensure_runtime_dir() -> Path:
    """Create `RUNTIME_DIR` 0700, enforcing the mode on an existing directory.

    Must run before anything underneath is created. ``Path.mkdir(mode=...)``
    applies the mode only to the *leaf* it creates, so a RUNTIME_DIR brought
    into being implicitly (as a parent of ``projects/`` or ``sessions/``)
    comes out 0755. Under the /tmp fallback that leaves spill files and the
    session registry world-readable, which is the whole reason this exists.

    RUNTIME_DIR is the only level that needs the mode: everything below it
    inherits its protection. The chmod repairs a 0755 directory left by an
    older version; a foreign owner is fatal rather than silently written into.
    """
    global _ensured
    if _ensured:
        return RUNTIME_DIR
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    st = RUNTIME_DIR.stat()
    if st.st_uid != os.getuid():
        raise RuntimeError(
            f"{RUNTIME_DIR} is owned by uid {st.st_uid}, not {os.getuid()}, "
            "refusing to write runtime state into another user's directory"
        )
    if stat.S_IMODE(st.st_mode) != 0o700:
        RUNTIME_DIR.chmod(0o700)
    _ensured = True
    return RUNTIME_DIR


# -- Project derivation ------------------------------------------------------


def project_slug(cwd: Path | None = None) -> str:
    """Readable, collision-free identifier for a project directory."""
    real = (cwd or Path.cwd()).resolve()
    digest = hashlib.sha256(str(real).encode("utf-8")).hexdigest()[:8]
    base = real.name or "root"
    return f"{base}-{digest}"


def project_path(cwd: Path | None = None) -> Path:
    """``$XDG_RUNTIME_DIR/repld/projects/<slug>/``, not created (for lookups)."""
    return PROJECTS_DIR / project_slug(cwd)


def project_dir(cwd: Path | None = None) -> Path:
    """`project_path`, created 0700 (for callers about to write into it)."""
    ensure_runtime_dir()
    d = project_path(cwd)
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


# -- Socket and suffix accessors ---------------------------------------------


def socket_path(cwd: Path | None = None) -> Path:
    """Default socket inside the project directory, without creating it.

    Pure: `status`/`exec`/`log` resolve this for projects with no kernel, and
    must not leave a directory behind. The kernel's flock creates it.
    """
    return project_path(cwd) / "kernel.sock"


def lock_for(sock: Path) -> Path:
    """JSON lockfile for *sock* (``kernel.lock``)."""
    return sock.with_suffix(".lock")


def flock_for(sock: Path) -> Path:
    """Flock(2) mutex file for *sock* (``kernel.flock``)."""
    return sock.with_suffix(".flock")


def hint_for(sock: Path) -> Path:
    """Dashboard hint file for *sock* (``kernel.dashboard``)."""
    return sock.with_suffix(".dashboard")


def eventlog_for(sock: Path) -> Path:
    """NDJSON event log for *sock* (``kernel.events``)."""
    return sock.with_suffix(".events")


def cache_for(sock: Path) -> Path:
    """Discovery cache for *sock* (``kernel.cache``)."""
    return sock.with_suffix(".cache")


GLOBAL_CACHE = STATE_PROJECTS_DIR.parent / "last.cache"


def state_cache_for(sock: Path) -> Path | None:
    """Reboot-surviving copy of `cache_for(sock)`, or None outside `PROJECTS_DIR`.

    An ``--ephemeral`` or ``--socket`` kernel keeps nothing durable: its directory
    is reclaimed or caller-owned, and a slug taken from it would collide.
    """
    if sock.parent.parent != PROJECTS_DIR:
        return None
    return STATE_PROJECTS_DIR / sock.parent.name / "kernel.cache"


def lock_path(cwd: Path | None = None) -> Path:
    """Convenience: `lock_for(socket_path(cwd))`."""
    return lock_for(socket_path(cwd))


def front_lock_for(port: int) -> Path:
    """Per-Chrome-instance front lock, not per project.

    Two separate repld kernels (or a kernel and a manually-run one) can point
    `browser.connect` at the same ``--remote-debugging-port``, and
    Page.bringToFront races across that boundary the same way it does across
    two tabs in one kernel. Lives directly under RUNTIME_DIR rather than a
    project dir for that reason.
    """
    ensure_runtime_dir()
    return RUNTIME_DIR / f"browser-{port}.front-lock"


# -- Resolution (CLI flags and env to one of the paths above) ----------------


def default_socket_path() -> Path:
    """Resolved at call time; cwd may change between import and `run_kernel`."""
    override = os.environ.get("REPLD_SOCKET")
    return Path(override) if override else socket_path()


# Set by `cli._apply_project` once --project/--project-git has chdir'd: an
# explicit --socket would then name a kernel whose cwd is not the project.
project_pinned = False
PROJECT_SOCKET_CONFLICT = (
    "repld: --project/--project-git and --socket are mutually exclusive"
)

# `sun_path` including its NUL is 108 bytes on Linux, 104 on macOS.
SOCKET_PATH_MAX = 103 if sys.platform == "darwin" else 107


def resolve_socket_path(argv: list[str]) -> tuple[Path, list[str]]:
    """Parse ``--socket PATH`` from *argv*, returning (socket, remaining args).

    Parses and consumes every ``--socket PATH`` / ``--socket=PATH`` occurrence
    (first value wins) so callers never re-implement the flag syntax.
    A path the kernel could not bind (longer than `SOCKET_PATH_MAX` bytes) is
    refused here, before any caller dials it or spawns a kernel at it.
    """
    sock: str | None = None
    rest: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--socket" and i + 1 < len(argv):
            sock = sock or argv[i + 1]
            i += 2
            continue
        if arg.startswith("--socket="):
            sock = sock or arg.split("=", 1)[1]
            i += 1
            continue
        rest.append(arg)
        i += 1
    if sock and project_pinned:
        raise SystemExit(PROJECT_SOCKET_CONFLICT)
    path = Path(sock) if sock else default_socket_path()
    length = len(os.fsencode(path))
    if length > SOCKET_PATH_MAX:
        raise SystemExit(
            f"repld: socket path is {length} bytes, over the {SOCKET_PATH_MAX} "
            f"a unix socket allows: {path}"
        )
    return path, rest


def resolve_lock_path(argv: list[str]) -> tuple[Path, list[str]]:
    """``resolve_socket_path`` for callers that want the lockfile instead."""
    sock, rest = resolve_socket_path(argv)
    return lock_for(sock), rest


# -- Gist directories -------------------------------------------------------
#
# Not runtime state: these are source directories the kernel imports from, and
# they live in the project (and in $HOME) precisely because they are the user's
# code. They belong here anyway, for the reason everything above does: they had
# no owner and were derived by hand at fourteen call sites across `gist_cmd`,
# `gists`, `gist_links` and `kernel`, one of which carried the comment "Same
# dirs + precedence a live kernel resolves against (kernel.py boot)", a
# hand-sync note, which is exactly the thing a single derivation makes
# unnecessary.


def global_gists_dir() -> Path:
    """``~/.repld/gists``, importable by every kernel regardless of cwd."""
    return Path.home() / ".repld" / "gists"


def local_gists_dir(cwd: Path | None = None) -> Path:
    """``./gists`` for *cwd* (default: the current one).

    Resolved at call time, never at import: `repld` is a long-lived process and
    the CLI commands each run in the directory they were invoked from.
    """
    return (cwd or Path.cwd()) / "gists"


def gist_dirs(cwd: Path | None = None) -> list[Path]:
    """Both gist directories in the kernel's own import precedence.

    Global first, then local: `gists.install` puts them on `sys.path` in this
    order, so this is what "which gist wins" means anywhere it is asked.
    `gist_lint --local` deliberately does *not* use this: it narrows to one
    project and resolves names against `./gists` first, which is the opposite
    precedence and the whole point of the flag.
    """
    return [global_gists_dir(), local_gists_dir(cwd)]
