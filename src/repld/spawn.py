"""Spawning a headless kernel — the one copy of the incantation.

Two callers need it and must not drift: `bridge.py`, when Claude Code connects
and no kernel is running for the cwd, and `repld restart`. It lives in its own
module rather than inside `lifecycle_cmd` because the bridge is spawned once
per Claude Code session and `cli.py` keeps it to a dict lookup plus a single
import — pulling in `lifecycle_cmd`'s urllib/signal/sessions would tax every
session for code the bridge never runs.

Only the spawn is shared. Each caller keeps its own wait loop, because they
wait on genuinely different things: the bridge polls until the socket is
*connectable* (it needs the connection anyway), `repld restart` polls until the
lockfile appears. Parameterising one loop over both predicates would cost more
than the six lines it saved.
"""

import os
import subprocess
import sys
from pathlib import Path

from . import paths


def spawn_headless(sock_path: Path) -> bool:
    """Start a detached headless kernel for the current cwd.

    Detached (`start_new_session`) so it outlives whoever spawned it — the
    kernel's in-memory state is meant to survive a Claude Code restart. If two
    callers race here, the kernel's flock mutex settles it and the loser exits 0.

    `python -m repld` rather than the `repld` console script: the script may not
    be on PATH in the environment a client hands us, but the interpreter running
    us always has the package importable.

    True means the process started, not that the kernel is up — poll for that.
    """
    cmd = [sys.executable, "-m", "repld", "--no-display"]
    # Only forward --socket when it isn't the default this cwd would resolve to
    # anyway; REPLD_SOCKET is inherited through the environment.
    if sock_path != paths.socket_path():
        cmd += ["--socket", str(sock_path)]
    try:
        subprocess.Popen(
            cmd,
            cwd=os.getcwd(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as e:
        print(f"repld: could not spawn kernel: {e}", file=sys.stderr)
        return False
    return True
