"""XDG runtime paths — the single source of truth for where kernel state lives.

Nothing repld writes at runtime lands in the project directory. Each kernel
gets a per-project subdirectory under ``$XDG_RUNTIME_DIR/repld/projects/``,
named ``{basename}-{sha256(realpath)[:8]}`` so it stays readable in `ls` while
two same-named projects still resolve apart.

One derivation rule for everything else: every runtime file is the socket path
with a different suffix. That keeps an explicit ``--socket`` / ``REPLD_SOCKET``
coherent — the lockfile, flock mutex, dashboard hint, and event log all follow
the socket wherever the caller pointed it.

    kernel.sock   unix-domain IPC socket
    kernel.lock   JSON {pid, socket_path, cwd, started_at, dashboard_port}
    kernel.flock  flock(2) mutex — never replaced, so the lock survives
                  atomic rewrites of kernel.lock
    kernel.dashboard  dashboard port + token + browser restore hint (0600)
    kernel.events     NDJSON event log
    kernel.cache      last-computed instructions/tools/resources (0600) —
                      lets the bridge answer MCP discovery without a live
                      kernel; survives kernel death, superseded on next boot

Per-process scratch is the one exception: task spills, resource spills, and
browser screenshots belong to the process rather than to any project, so they
sit directly in RUNTIME_DIR. They all share a ``{pid}-…`` prefix, which is what
lets a booting kernel sweep everything a dead pid left behind in one pass
(``state.sweep_dead_pid_files``) — nothing runs on the way out of a SIGKILL,
and under the /tmp fallback the leftovers would otherwise be permanent.

Without ``XDG_RUNTIME_DIR`` (macOS, most containers, any env-scrubbed spawn)
the tree falls back to ``/tmp/repld-{uid}``. The uid suffix is load-bearing:
``/tmp`` is world-writable, so a shared unsuffixed name could be pre-created
and owned by another local user. ``ensure_runtime_dir()`` is what puts the
0700 gate over everything below.
"""

import hashlib
import os
import stat
from pathlib import Path

_XDG = os.environ.get("XDG_RUNTIME_DIR")
RUNTIME_DIR = Path(_XDG) / "repld" if _XDG else Path("/tmp") / f"repld-{os.getuid()}"
PROJECTS_DIR = RUNTIME_DIR / "projects"

_ensured = False


def ensure_runtime_dir() -> Path:
    """Create RUNTIME_DIR 0700, enforcing the mode on an existing directory.

    Must run before anything underneath is created. ``Path.mkdir(mode=...)``
    applies the mode only to the *leaf* it creates, so a RUNTIME_DIR brought
    into being implicitly — as a parent of ``projects/`` or ``sessions/`` —
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
            f"{RUNTIME_DIR} is owned by uid {st.st_uid}, not {os.getuid()} — "
            "refusing to write runtime state into another user's directory"
        )
    if stat.S_IMODE(st.st_mode) != 0o700:
        RUNTIME_DIR.chmod(0o700)
    _ensured = True
    return RUNTIME_DIR


def project_slug(cwd: Path | None = None) -> str:
    """Readable, collision-free identifier for a project directory."""
    real = (cwd or Path.cwd()).resolve()
    digest = hashlib.sha256(str(real).encode("utf-8")).hexdigest()[:8]
    base = real.name or "root"
    return f"{base}-{digest}"


def project_dir(cwd: Path | None = None) -> Path:
    """``$XDG_RUNTIME_DIR/repld/projects/<slug>/``, created 0700."""
    ensure_runtime_dir()
    d = PROJECTS_DIR / project_slug(cwd)
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def socket_path(cwd: Path | None = None) -> Path:
    return project_dir(cwd) / "kernel.sock"


def lock_for(sock: Path) -> Path:
    return sock.with_suffix(".lock")


def flock_for(sock: Path) -> Path:
    return sock.with_suffix(".flock")


def hint_for(sock: Path) -> Path:
    return sock.with_suffix(".dashboard")


def eventlog_for(sock: Path) -> Path:
    return sock.with_suffix(".events")


def cache_for(sock: Path) -> Path:
    return sock.with_suffix(".cache")


def lock_path(cwd: Path | None = None) -> Path:
    return lock_for(socket_path(cwd))


# ---------------------------------------------------------------------------
# Resolution — turning CLI flags and env into one of the paths above
# ---------------------------------------------------------------------------


def default_socket_path() -> Path:
    """Resolved at call time — cwd may change between import and run_kernel."""
    override = os.environ.get("REPLD_SOCKET")
    return Path(override) if override else socket_path()


def resolve_socket_path(argv: list[str]) -> tuple[Path, list[str]]:
    """Resolve the kernel socket path from --socket flags, REPLD_SOCKET env,
    or the per-project XDG default. Shared by every client subcommand.

    Parses AND consumes every ``--socket PATH`` / ``--socket=PATH`` occurrence
    (first value wins), returning (socket_path, argv_without_socket_flags) so
    callers never re-implement the flag syntax.
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
    return (Path(sock) if sock else default_socket_path()), rest


def resolve_lock_path(argv: list[str]) -> tuple[Path, list[str]]:
    """``resolve_socket_path`` for callers that want the lockfile instead."""
    sock, rest = resolve_socket_path(argv)
    return lock_for(sock), rest
