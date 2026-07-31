"""`repld log` — read a headless kernel's activity from any terminal.

The TUI display is only available to whoever started the kernel in a pane. An
auto-spawned kernel has no pane at all, so this reads the same event stream
back off the NDJSON event log (eventlog.py) and renders it in the same shapes.
"""

import json
import sys

from . import eventlog, paths, render, state
from .render import DIM as _DIM, RED as _RED, RESET as _RESET

# Rendered as a raw byte stream rather than as lines — see _emit.
_CHUNK_TYPES = ("StdoutChunk", "StderrChunk")

_USAGE = """\
repld log — recent activity from this project's kernel

  repld log [-n N] [-f|--follow] [--json] [--socket PATH]

  -n N        number of recent records to show (default 200)
  -f          stream new activity instead of exiting
  --json      raw NDJSON records, one per line
"""


def _render(rec: dict) -> str | None:
    """One line (or block) per record, in the TUI's vocabulary.

    The formats come from `render.py` so the two surfaces can't drift; what is
    local here is reading them back out of a JSON record instead of a
    dataclass, and the fact that a replay has no cursor to keep — every gate
    prompt is already answered by the time it is read.
    """
    kind = rec.get("type")
    t = rec.get("t", 0)
    if kind == "CellStart":
        head = render.cell_header(str(rec.get("task_id", "")), t)
        body = render.cell_source_block(rec.get("source") or "")
        return f"{head}\n{body}" if body else head
    if kind in _CHUNK_TYPES:
        # Verbatim, newlines and all — _emit writes these raw. See its comment.
        text = rec.get("text") or ""
        if not text:
            return None
        if rec.get("truncated"):
            text = (
                text.rstrip("\n") + f"\n{_DIM}… output elided (per-cell cap){_RESET}\n"
            )
        return f"{_RED}{text}{_RESET}" if kind == "StderrChunk" else text
    if kind == "CellDone":
        return render.cell_done_line(
            str(rec.get("task_id", "")),
            float(rec.get("elapsed_ms", 0)),
            rec.get("error"),
        )
    if kind == "ChannelPush":
        return render.channel_block(
            rec.get("content") or "", rec.get("meta") or {}, t=t
        )
    if kind == "HumanPromptOpen":
        # rstrip the trailing "': '" — it exists to park a live cursor, and a
        # replay is printing a finished line.
        return render.prompt_open(
            str(rec.get("kind", "")),
            rec.get("prompt", ""),
            rec.get("options"),
            str(rec.get("gate_id", "")),
        ).rstrip()
    if kind == "HumanPromptResponse":
        return render.prompt_response(rec.get("value"))
    if kind == "BrowserTabAttached":
        return render.browser_attached(
            str(rec.get("target", "")), rec.get("url", ""), rec.get("title", "")
        )
    if kind == "BrowserTabDetached":
        return render.browser_detached(str(rec.get("target", "")))
    if kind == "LogRotated":
        return f"{_DIM}… {rec.get('note', 'log rotated')}{_RESET}"
    return f"{_DIM}{kind}{_RESET}"


# Whether the last thing written ended a line. Cell output is the only thing
# that can leave us mid-line, and a structured record printed onto the end of a
# half-finished line is unreadable — the TUI pads for the same reason
# (display._render_cell_done).
_at_line_start = True


def _emit(rec: dict, as_json: bool) -> None:
    global _at_line_start
    if as_json:
        print(json.dumps(rec))
        return
    line = _render(rec)
    if line is None:
        return
    if rec.get("type") in _CHUNK_TYPES:
        # Cell output is a byte stream, not a line stream: one print() in user
        # code arrives as several chunks ("deferred:", " ", "abc123", "\n"), and
        # a chunk can be a bare newline. print()-ing each one turns a single
        # line into four and doubles every blank. Write verbatim, like the TUI's
        # _out() does, and let the newlines in the data do the work.
        sys.stdout.write(line)
        sys.stdout.flush()
        _at_line_start = line.endswith("\n")
        return
    if not _at_line_start:
        sys.stdout.write("\n")
    print(line)
    _at_line_start = True


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
        # stderr, not stdout: `--json` promises an NDJSON stream, and a `repld
        # log --json | jq` pipeline would break on this line — precisely when
        # the kernel is down, which is when you are most likely reading the log.
        print(
            f"{_DIM}(no kernel running — showing the last one's log){_RESET}",
            file=sys.stderr,
        )

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
