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
"""

import hashlib
import os
from pathlib import Path

RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "repld"
PROJECTS_DIR = RUNTIME_DIR / "projects"


def project_slug(cwd: Path | None = None) -> str:
    """Readable, collision-free identifier for a project directory."""
    real = (cwd or Path.cwd()).resolve()
    digest = hashlib.sha256(str(real).encode("utf-8")).hexdigest()[:8]
    base = real.name or "root"
    return f"{base}-{digest}"


def project_dir(cwd: Path | None = None) -> Path:
    """``$XDG_RUNTIME_DIR/repld/projects/<slug>/``, created 0700."""
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


def lock_path(cwd: Path | None = None) -> Path:
    return lock_for(socket_path(cwd))


def flock_path(cwd: Path | None = None) -> Path:
    return flock_for(socket_path(cwd))


def hint_path(cwd: Path | None = None) -> Path:
    return hint_for(socket_path(cwd))


def eventlog_path(cwd: Path | None = None) -> Path:
    return eventlog_for(socket_path(cwd))
