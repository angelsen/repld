"""Stdio MCP ↔ unix-socket bridge.

Dumb bidirectional byte-pipe. Does not parse MCP. Reads the kernel's socket
path from ./.pyrepl.lock, connects, then:

    stdin  → socket   (thread 1)
    socket → stdout   (thread 2)

Exits on EOF from either side. One bridge = one MCP client session.
"""

import json
import socket
import sys
import threading
from pathlib import Path

LOCK_PATH = Path.cwd() / ".pyrepl.lock"


def _err(msg: str) -> None:
    print(f"repld bridge: {msg}", file=sys.stderr, flush=True)


def _pid_alive(pid: int) -> bool:
    import os

    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def run_bridge(argv: list[str]) -> int:
    if not LOCK_PATH.exists():
        _err(
            f"no kernel found (missing {LOCK_PATH.name}); start `repld` in this cwd first"
        )
        return 1
    try:
        lock = json.loads(LOCK_PATH.read_text())
    except (OSError, json.JSONDecodeError) as e:
        _err(f"cannot read {LOCK_PATH.name}: {e}")
        return 1
    if not _pid_alive(lock.get("pid", -1)):
        _err(f"kernel pid {lock.get('pid')} is not running (stale {LOCK_PATH.name})")
        return 1
    sock_path = lock.get("socket_path")
    if not sock_path:
        _err(f"{LOCK_PATH.name} missing socket_path")
        return 1

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(sock_path)
    except OSError as e:
        _err(f"cannot connect to kernel socket {sock_path}: {e}")
        return 1

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
