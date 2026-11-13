"""`repld peer-pipe`: the far end of a peer call that arrives over `ssh`.

`peer.Peer` on another machine runs `ssh HOST repld peer-pipe TARGET` and speaks
the peer protocol over that process's stdio. This command resolves `TARGET`
against the kernels of the machine it runs on, writes one status line, and from
then on is a byte relay to that kernel's unix socket: the kernel cannot tell the
call from a local one.

The status line comes before any request byte reaches the kernel, which is what
lets the caller tell "never ran" from "unknown". Stdin EOF is forwarded as a
hangup, the signal the kernel cancels a running call on.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from typing import BinaryIO

from . import cli_args, peer, sessions

_LABEL = "repld peer-pipe"
_USAGE = """\
usage: repld peer-pipe [--start] TARGET

Relay one peer call between stdio and the kernel for TARGET (a project path, or
the directory name of a live kernel). Run by `peers.get("host:TARGET")` over
ssh, not by hand.

  --start   start a headless kernel for TARGET if none is running
"""
_START_TIMEOUT = 60.0
_CHUNK = 65536


def run_peer_pipe(argv: list[str]) -> int:
    """Relay stdio to TARGET's kernel; exit 0 once the kernel hangs up."""
    if cli_args.wants_help(argv):
        print(_USAGE)
        return 0
    bad = cli_args.check_args(_LABEL, argv, _USAGE, flags=("--start",))
    if bad is not None:
        return bad
    targets = [a for a in argv if a != "--start"]
    if not targets:
        print(_USAGE, file=sys.stderr)
        return 2
    out = sys.stdout.buffer
    try:
        cwd, sock = _open_kernel(targets[0], start="--start" in argv)
    except peer.PeerNotFound as exc:
        _write_status(out, "not_found", str(exc), candidates=exc.candidates)
        return 1
    except peer.PeerUnavailable as exc:
        _write_status(out, "unavailable", str(exc))
        return 1
    _write_status(out, "ready", cwd, socket_path=sock.getpeername())
    threading.Thread(
        target=_pump_stdin, args=(sock,), daemon=True, name="repld-peer-pipe-stdin"
    ).start()
    _pump_socket(sock, out)
    return 0


def _open_kernel(target: str, *, start: bool) -> tuple[str, socket.socket]:
    """Resolve `target` on this machine and connect to its kernel.

    Raises `PeerNotFound` for a name no live kernel has, `PeerUnavailable` for
    a project with no reachable kernel.
    """
    cwd = peer.resolve_cwd(target, sessions.list_sessions())
    path = peer.resolve_socket(cwd)
    if path is None and start:
        _start_kernel(cwd)
        path = peer.resolve_socket(cwd)
    if path is None:
        raise peer.PeerUnavailable(
            f"no kernel is running for {cwd}: run `repld --project {cwd} start` "
            "there, or peers.get(..., start=True)"
        )
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(path)
    except OSError as exc:
        sock.close()
        raise peer.PeerUnavailable(f"cannot connect to {path}: {exc}") from exc
    return cwd, sock


def _start_kernel(cwd: str) -> None:
    """Start a headless kernel for `cwd`, raising `PeerUnavailable` on failure."""
    try:
        done = subprocess.run(
            peer.start_command(cwd),
            env=peer.clean_env(dict(os.environ), set()),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=_START_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise peer.PeerUnavailable(f"timed out starting a kernel for {cwd}") from None
    if done.returncode != 0:
        text = done.stdout.decode("utf-8", "replace").strip()[-1000:]
        raise peer.PeerUnavailable(f"`repld --project {cwd} start` failed: {text}")


def _write_status(
    out: BinaryIO,
    status: str,
    message: str,
    *,
    candidates: list[str] | None = None,
    socket_path: str = "",
) -> None:
    """Write the one status line the caller reads before sending its request.

    `socket_path` is what the caller forwards to reach this kernel directly.
    """
    params = {
        "status": status,
        "message": message,
        "candidates": candidates or [],
        "socket": socket_path,
    }
    line = json.dumps(
        {"jsonrpc": "2.0", "method": peer.PIPE_STATUS_METHOD, "params": params}
    )
    out.write(line.encode("utf-8") + b"\n")
    out.flush()


def _pump_stdin(sock: socket.socket) -> None:
    """Copy stdin to the kernel, then hang up the write side at EOF."""
    try:
        while chunk := os.read(0, _CHUNK):
            sock.sendall(chunk)
        # Write side only: the reply to a request already sent still has to
        # come back, and the kernel reads this EOF as the caller hanging up.
        sock.shutdown(socket.SHUT_WR)
    except OSError:
        pass


def _pump_socket(sock: socket.socket, out: BinaryIO) -> None:
    """Copy the kernel's bytes to stdout until either side is gone."""
    try:
        while chunk := sock.recv(_CHUNK):
            out.write(chunk)
            out.flush()
    except OSError:
        pass
