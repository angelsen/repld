"""Session registry — user-scoped directory of live repld instances.

Each running kernel writes `$XDG_RUNTIME_DIR/repld/sessions/<pid>.json` on
boot and removes it on shutdown. Unlike the per-project lockfile
(`.pyrepl.lock`), this index doesn't depend on being inside the project
cwd — any repld instance (or its dashboard) can enumerate all live
siblings by reading this directory.

Stale entries (dead PIDs, corrupt files) are pruned lazily whenever the
directory is read.
"""

import json
import os
import time
from pathlib import Path

from .ipc import _pid_alive
from .tasks import RUNTIME_DIR

__all__ = ["register", "unregister", "list_sessions"]

SESSIONS_DIR = RUNTIME_DIR / "sessions"


def _session_path(pid: int | None = None) -> Path:
    return SESSIONS_DIR / f"{pid or os.getpid()}.json"


def register(cwd: str, socket_path: str, dashboard_port: int | None) -> None:
    """Write this process's session file."""
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    info: dict[str, object] = {
        "pid": os.getpid(),
        "cwd": cwd,
        "socket_path": socket_path,
        "dashboard_port": dashboard_port,
        "started_at": time.time(),
    }
    _session_path().write_text(json.dumps(info))


def unregister() -> None:
    """Remove this process's session file. Best-effort."""
    try:
        _session_path().unlink()
    except FileNotFoundError:
        pass


def list_sessions() -> list[dict]:
    """Read all session files, pruning stale (dead PID or corrupt) entries."""
    if not SESSIONS_DIR.is_dir():
        return []
    result = []
    for f in SESSIONS_DIR.glob("*.json"):
        info = None
        try:
            info = json.loads(f.read_text())
            pid = int(info["pid"])
        except (OSError, KeyError, ValueError, TypeError):
            # Corrupt or mid-write — judge liveness by the filename pid.
            try:
                pid = int(f.stem)
            except ValueError:
                pid = None
        if pid is not None and _pid_alive(pid):
            if info is not None:
                result.append(info)
            continue
        try:
            f.unlink()
        except OSError:
            pass
    return result
