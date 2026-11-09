"""Refuse a signal aimed at the kernel's own process group.

A child started from a cell without `start_new_session=True` shares the
kernel's group, so `os.killpg(child_pgid, ...)` takes the kernel and every
session's state with it. `install()` wraps `os.killpg` and `os.kill` in the
kernel process; a target that reaches the kernel's group raises, every other
target passes through.

Stdlib only and imports nothing from repld.
"""

import os
import signal
from collections.abc import Callable

_MARKER = "_kernel_guard"


class OwnGroupSignal(PermissionError):
    """Raised for a signal that would reach the kernel's own process group."""


def build_guard(
    real_killpg: Callable[[int, int], None],
    real_kill: Callable[[int, int], None],
    own_pgid: int,
) -> tuple[Callable[[int, int], None], Callable[[int, int], None]]:
    """Return `(killpg, kill)` wrappers that refuse any target reaching `own_pgid`."""
    message = (
        f"refused: signal aimed at the repld kernel's own process group ({own_pgid}); "
        "a child started without start_new_session=True shares it, so signal "
        "the child's pid or start it in its own session"
    )

    def killpg(pgid: int, sig: int | signal.Signals) -> None:
        if pgid in (0, own_pgid):
            raise OwnGroupSignal(message)
        real_killpg(pgid, sig)

    def kill(pid: int, sig: int | signal.Signals) -> None:
        # 0 is the caller's group, -1 every process the user may signal.
        if pid in (0, -1) or -pid == own_pgid:
            raise OwnGroupSignal(message)
        real_kill(pid, sig)

    return killpg, kill


def install() -> None:
    """Guard `os.killpg` and `os.kill` for this process; a repeat call is a no-op."""
    if getattr(os.killpg, _MARKER, False):
        return
    killpg, kill = build_guard(os.killpg, os.kill, os.getpgrp())
    setattr(killpg, _MARKER, True)
    os.killpg, os.kill = killpg, kill
