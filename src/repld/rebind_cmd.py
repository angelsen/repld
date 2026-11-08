"""`repld rebind`: a SessionStart hook command that re-keys this pane's connection.

Claude Code puts no session id on the MCP wire, so a bridge only knows the id
its env carried at start. That id is stale after `/clear` and after any resume
(`--resume`, `--continue`, in-session `/resume`), and can be a throwaway one for
a resume from the picker. The SessionStart hook is the one place the real id is
delivered; this command forwards it to the kernel as `ipc.rebind_claude_session`.
"""

import json
import os
import sys

from . import cli_args, paths
from .exec_cmd import _call, _connect

_USAGE = """\
repld rebind: re-key this Claude Code pane's repld connection to the current session id

  repld rebind [--socket PATH]       reads a SessionStart hook payload on stdin

  Wire it as a SessionStart hook in .claude/settings.json:

    {"hooks": {"SessionStart": [{"matcher": "resume|clear", "hooks": [
      {"type": "command", "command": "repld rebind"}]}]}}

  Only source `resume` and `clear` rebind; `startup` and `compact` already
  carry the right id. The hook must run where the kernel's project is the cwd
  (the default), and a kernel must be running: with none, there is no
  connection to fix and the command says so and exits 0.
"""

_LABEL = "repld rebind"
_REBIND_SOURCES = frozenset({"resume", "clear"})


def _err(msg: str) -> None:
    """Print a labeled error to stderr."""
    print(f"{_LABEL}: {msg}", file=sys.stderr, flush=True)


def parse_payload(text: str) -> dict | None:
    """Parse a SessionStart hook payload, or None when it is not a JSON object."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def run_rebind(argv: list[str]) -> int:
    """Entry point for `repld rebind`; exits 0 whenever there is nothing to fix."""
    if cli_args.wants_help(argv):
        print(_USAGE)
        return 0
    sock_path, rest = paths.resolve_socket_path(argv)
    bad = cli_args.check_args(_LABEL, rest, _USAGE, positionals=0)
    if bad is not None:
        return bad

    payload = parse_payload(sys.stdin.read())
    if payload is None:
        _err("stdin is not a SessionStart hook payload (a JSON object)")
        return 2
    if payload.get("source") not in _REBIND_SOURCES:
        return 0
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        _err("payload has no session_id")
        return 2

    conn = _connect(paths.lock_for(sock_path), label=_LABEL)
    if conn is None:
        return 0
    sock, rfile, wfile, _lock = conn
    try:
        return _rebind(rfile, wfile, session_id)
    finally:
        sock.close()


def _rebind(rfile, wfile, session_id: str) -> int:
    """Ask the kernel to re-key the connection under this process's `claude`."""
    resp = _call(
        rfile, wfile, "sessions/rebind", {"session_id": session_id, "pid": os.getpid()}
    )
    if resp is None:
        _err("kernel disconnected")
        return 1
    if "error" in resp:
        _err(resp["error"].get("message", "unknown error"))
        return 1
    result = resp["result"]
    if not result["rebound"]:
        _err(result["message"])
    return 0
