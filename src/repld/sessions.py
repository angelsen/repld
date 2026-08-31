"""Session registry — user-scoped directory of live repld instances.

Each running kernel writes `$XDG_RUNTIME_DIR/repld/sessions/<pid>.json` on
boot and removes it on shutdown. Unlike the per-project lockfile
(`projects/<slug>/kernel.lock`), this index doesn't depend on knowing the
project cwd — any repld instance (or its dashboard) can enumerate all live
siblings by reading this directory.

Stale entries (dead PIDs, corrupt files) are pruned lazily whenever the
directory is read.
"""

import json
import os
import time
from pathlib import Path

from .paths import RUNTIME_DIR, ensure_runtime_dir
from .state import atomic_write_json, pid_alive

__all__ = ["list_sessions", "register", "unregister"]

SESSIONS_DIR = RUNTIME_DIR / "sessions"


def _session_path() -> Path:
    """This process's own session file. Reading someone else's goes through
    `list_sessions`, which has to prune stale entries as it walks anyway."""
    return SESSIONS_DIR / f"{os.getpid()}.json"


def register(cwd: str, socket_path: str, dashboard_port: int | None) -> None:
    """Write this process's session file (0600 — it names every project cwd)."""
    ensure_runtime_dir()
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    info: dict[str, object] = {
        "pid": os.getpid(),
        "cwd": cwd,
        "socket_path": socket_path,
        "dashboard_port": dashboard_port,
        "started_at": time.time(),
    }
    atomic_write_json(_session_path(), info, chmod=0o600)


def unregister() -> None:
    """Remove this process's session file. Best-effort."""
    try:
        _session_path().unlink()
    except FileNotFoundError:
        pass


def list_sessions() -> list[dict]:
    """Live sessions, pruning stale (dead PID or corrupt) entries.

    Every returned entry is a dict with a parseable `pid`; callers may index
    that key directly.
    """
    if not SESSIONS_DIR.is_dir():
        return []
    result = []
    for f in SESSIONS_DIR.glob("*.json"):
        info = None
        try:
            info = json.loads(f.read_text())
            pid = int(info["pid"])
        except (OSError, KeyError, ValueError, TypeError):
            # Corrupt, unreadable, or not the shape we write. Judge liveness by
            # the filename pid so a live kernel's file isn't deleted out from
            # under it — but drop the payload rather than returning it. Every
            # consumer indexes `pid` unguarded (`repld status`, `repld stop
            # --all`, the dashboard sidebar), and an entry reaches here
            # *because* its pid didn't parse, so handing it back turns one bad
            # file into a crashed command.
            info = None
            try:
                pid = int(f.stem)
            except ValueError:
                pid = None
        if pid is not None and pid_alive(pid):
            if info is not None:
                result.append(info)
            continue
        try:
            f.unlink()
        except OSError:
            pass
    return result
