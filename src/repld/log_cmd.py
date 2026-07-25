"""`repld log` — read a headless kernel's activity from any terminal.

The TUI display is only available to whoever started the kernel in a pane. An
auto-spawned kernel has no pane at all, so this reads the same event stream
back off the NDJSON event log (eventlog.py) and renders it in the same shapes.
"""

import json
import sys
import time

from . import eventlog, paths, state

_DIM = "\033[2m"
_RED = "\033[31m"
_CYAN = "\033[36m"
_GREEN = "\033[32m"
_RESET = "\033[0m"

_USAGE = """\
repld log — recent activity from this project's kernel

  repld log [-n N] [-f|--follow] [--json] [--socket PATH]

  -n N        number of recent records to show (default 200)
  -f          stream new activity instead of exiting
  --json      raw NDJSON records, one per line
"""


def _ts(rec: dict) -> str:
    return time.strftime("%H:%M:%S", time.localtime(rec.get("t", 0)))


def _render(rec: dict) -> str | None:
    """One line (or block) per record, mirroring the TUI's vocabulary."""
    kind = rec.get("type")
    ts = _ts(rec)
    if kind == "CellStart":
        tid = str(rec.get("task_id", ""))[:8]
        src = (rec.get("source") or "").rstrip()
        head = f"{_DIM}── cell {tid} · {ts} ──{_RESET}"
        body = "\n".join(f"  {ln}" for ln in src.splitlines())
        return f"{head}\n{body}" if body else head
    if kind in ("StdoutChunk", "StderrChunk"):
        text = rec.get("text") or ""
        if not text:
            return None
        text = text.rstrip("\n")
        if rec.get("truncated"):
            text += f"\n{_DIM}… output elided (per-cell cap){_RESET}"
        return f"{_RED}{text}{_RESET}" if kind == "StderrChunk" else text
    if kind == "CellDone":
        tid = str(rec.get("task_id", ""))[:8]
        ms = f"{rec.get('elapsed_ms', 0):.0f}ms"
        if rec.get("error"):
            return f"{_RED}✗{_RESET} {_DIM}{tid} · err({rec['error']}) · {ms}{_RESET}"
        return f"{_GREEN}✓{_RESET} {_DIM}{tid} · done · {ms}{_RESET}"
    if kind == "ChannelPush":
        lines = [f"{_CYAN}┌─ channel · {ts} ─{_RESET}"]
        lines += [
            f"{_CYAN}│{_RESET} {ln}"
            for ln in (rec.get("content") or "").rstrip().splitlines()
        ]
        meta = rec.get("meta") or {}
        if meta:
            joined = "  ".join(f"{k}={v}" for k, v in meta.items())
            lines.append(f"{_CYAN}│{_RESET} {_DIM}{joined}{_RESET}")
        lines.append(f"{_CYAN}└───────────{_RESET}")
        return "\n".join(lines)
    if kind == "HumanPromptOpen":
        return f"{_CYAN}? {rec.get('prompt', '')}{_RESET} {_DIM}({rec.get('kind')}){_RESET}"
    if kind == "HumanPromptResponse":
        return f"{_DIM}↳ {rec.get('value')}{_RESET}"
    if kind == "BrowserTabAttached":
        return f"{_DIM}[browser] attached {str(rec.get('target', ''))[:12]} {rec.get('url', '')}{_RESET}"
    if kind == "BrowserTabDetached":
        return f"{_DIM}[browser] detached {str(rec.get('target', ''))[:12]}{_RESET}"
    if kind == "LogRotated":
        return f"{_DIM}… {rec.get('note', 'log rotated')}{_RESET}"
    return f"{_DIM}{kind}{_RESET}"


def _emit(rec: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(rec))
        return
    line = _render(rec)
    if line is not None:
        print(line)


def run_log(argv: list[str]) -> int:
    if any(a in ("-h", "--help") for a in argv):
        print(_USAGE)
        return 0

    sock_path, rest = paths.resolve_socket_path(argv)
    follow = False
    as_json = False
    tail = 200
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg in ("-f", "--follow"):
            follow = True
        elif arg == "--json":
            as_json = True
        elif arg == "-n" and i + 1 < len(rest):
            try:
                tail = int(rest[i + 1])
            except ValueError:
                print(f"repld log: -n expects a number, got {rest[i + 1]!r}")
                return 2
            i += 1
        else:
            print(f"repld log: unknown argument {arg!r}\n")
            print(_USAGE)
            return 2
        i += 1

    log_path = paths.eventlog_for(sock_path)
    lock = state.read_lock(paths.lock_for(sock_path))
    if isinstance(lock, str) and not log_path.exists():
        print(f"repld log: {lock}", file=sys.stderr)
        return 1
    if isinstance(lock, str):
        print(f"{_DIM}(no kernel running — showing the last one's log){_RESET}")

    for rec in eventlog.read_records(log_path, tail=tail):
        _emit(rec, as_json)

    if not follow:
        return 0
    if not log_path.exists():
        print(f"repld log: no event log at {log_path}", file=sys.stderr)
        return 1
    try:
        for rec in eventlog.follow(log_path):
            _emit(rec, as_json)
    except KeyboardInterrupt:
        pass
    return 0
