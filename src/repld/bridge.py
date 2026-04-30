"""Stdio MCP ↔ unix-socket bridge.

Dumb bidirectional byte-pipe. Does not parse MCP. Reads the kernel's socket
path from ./.pyrepl.lock, connects, then:

    stdin  → socket   (thread 1)
    socket → stdout   (thread 2)

Exits on EOF from either side. One bridge = one MCP client session.
"""

import os
import socket
import sys
import threading
from pathlib import Path

from .ipc import connect_to_kernel


def _err(msg: str) -> None:
    print(f"repld bridge: {msg}", file=sys.stderr, flush=True)


def _resolve_socket(argv: list[str]) -> Path | None:
    """Return explicit socket path from --socket flag or REPLD_SOCKET env, or None for default."""
    # Simple arg parsing — bridge doesn't need argparse
    for i, arg in enumerate(argv):
        if arg == "--socket" and i + 1 < len(argv):
            return Path(argv[i + 1])
        if arg.startswith("--socket="):
            return Path(arg.split("=", 1)[1])
    env = os.environ.get("REPLD_SOCKET")
    if env:
        return Path(env)
    return None


def run_bridge(argv: list[str]) -> int:
    sock_override = _resolve_socket(argv)
    if sock_override is not None:
        lock_path = sock_override.with_suffix(".lock")
    else:
        lock_path = Path.cwd() / ".pyrepl.lock"
    result = connect_to_kernel(lock_path)
    if isinstance(result, str):
        _err(result)
        return 1
    sock, _lock = result

    stop = threading.Event()

    def stdin_to_sock() -> None:
        try:
            for line in sys.stdin:
                if not line.endswith("\n"):
                    line = line + "\n"
                sock.sendall(line.encode("utf-8"))
        except (BrokenPipeError, OSError):
            stop.set()
        finally:
            # Half-close write side so the kernel sees EOF and drains/closes.
            # DO NOT set stop here: in-flight responses may still be inbound.
            try:
                sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    def sock_to_stdout() -> None:
        try:
            rfile = sock.makefile("r", encoding="utf-8")
            for line in rfile:
                sys.stdout.write(line)
                sys.stdout.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            # Socket-side EOF drives shutdown.
            stop.set()

    threading.Thread(target=stdin_to_sock, daemon=True, name="bridge-stdin").start()
    threading.Thread(target=sock_to_stdout, daemon=True, name="bridge-stdout").start()
    stop.wait()
    try:
        sock.close()
    except OSError:
        pass
    return 0
