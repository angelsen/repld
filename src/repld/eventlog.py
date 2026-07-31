"""NDJSON event log — what makes a headless kernel observable.

A `--no-display` kernel has no TUI, and therefore no consumer for the event
queue — `events.disable_queue()` turns that half off, leaving this module's
sink as the only thing an auto-spawned kernel's events reach. It tees
`events.emit()` to one line-per-event file next to the socket, which
`repld log` reads back (and follows).

The tee is installed by the kernel, not imported by `events` — `events` stays
dependency-free so this module can import `display` for its chunk cap without
creating a cycle.

Bounded twice over: chunk events honour the display's per-cell byte cap, and
the file itself is size-capped (oldest history is dropped by truncating, since
a headless kernel can run for weeks). Full cell output always remains in the
task's spill file; this is a viewer log, not an archive.
"""

import dataclasses
import json
import threading
import time
from pathlib import Path
from typing import Iterator

from . import events
from .display import VIEWER_MAX_BYTES
from .paths import ensure_runtime_dir
from .state import open_private

MAX_BYTES = 8 * 1024 * 1024
CHUNK_CAP_BYTES = VIEWER_MAX_BYTES
_POLL_SECONDS = 0.25

_lock = threading.Lock()
_fp = None
_written = 0
_chunk_bytes: dict[str, int] = {}
_chunk_capped: set[str] = set()


def install(path: Path) -> None:
    """Open (truncating) the log and start teeing events into it.

    Truncating at boot is deliberate: the log describes the *running* kernel,
    and a fresh kernel's task ids don't mean anything against the last one's.
    """
    global _fp, _written
    with _lock:
        _close_locked()
        ensure_runtime_dir()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _fp = open_private(path)
        _written = 0
        _chunk_bytes.clear()
        _chunk_capped.clear()
    events.set_sink(write)


def close() -> None:
    events.set_sink(None)
    with _lock:
        _close_locked()


def _close_locked() -> None:
    global _fp
    if _fp is not None:
        try:
            _fp.close()
        except OSError:
            pass
        _fp = None


def _record(ev: "events.Event") -> dict | None:
    """One JSON-safe dict per event, or None to skip it.

    Caller holds the lock — the per-task chunk budget is mutated here.
    """
    rec: dict = {"t": time.time(), "type": type(ev).__name__}
    for f in dataclasses.fields(ev):
        rec[f.name] = getattr(ev, f.name)

    if isinstance(ev, events.CellDone):
        _chunk_bytes.pop(ev.task_id, None)
        _chunk_capped.discard(ev.task_id)
        return rec

    if isinstance(ev, (events.StdoutChunk, events.StderrChunk)) and ev.task_id:
        tid = ev.task_id
        if tid in _chunk_capped:
            return None
        used = _chunk_bytes.get(tid, 0)
        room = CHUNK_CAP_BYTES - used
        head = ev.text[:room]
        _chunk_bytes[tid] = used + len(head)
        rec["text"] = head
        if len(ev.text) > room:
            _chunk_capped.add(tid)
            rec["truncated"] = True
    return rec


def write(ev: "events.Event") -> None:
    """Append one NDJSON record. Never raises into emit()."""
    global _written
    try:
        with _lock:
            if _fp is None:
                return
            rec = _record(ev)
            if rec is None:
                return
            line = json.dumps(rec, default=str) + "\n"
            if _written + len(line) > MAX_BYTES:
                _fp.seek(0)
                _fp.truncate()
                notice = (
                    json.dumps(
                        {
                            "t": time.time(),
                            "type": "LogRotated",
                            "note": f"event log exceeded {MAX_BYTES} bytes; "
                            "older history dropped",
                        }
                    )
                    + "\n"
                )
                _fp.write(notice)
                # Counted, not zeroed: the notice is bytes in the file like any
                # other record, and skipping it let the cap drift upward by its
                # length on every rotation.
                _written = len(notice)
            _fp.write(line)
            _fp.flush()
            _written += len(line)
    except (OSError, ValueError, TypeError):
        pass


def read_records(path: Path, *, tail: int | None = None) -> list[dict]:
    """Parse the log. Unparseable lines (a torn final write) are skipped."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out[-tail:] if tail else out


def follow(path: Path, *, stop: threading.Event | None = None) -> Iterator[dict]:
    """Yield records as they are appended, starting from the current end.

    Handles the writer truncating underneath us (size cap, or a kernel restart
    reopening the file) by rewinding to the start of the shorter file.
    """
    pos = path.stat().st_size if path.exists() else 0
    buf = ""
    while stop is None or not stop.is_set():
        try:
            size = path.stat().st_size
        except OSError:
            time.sleep(_POLL_SECONDS)
            continue
        if size < pos:
            pos, buf = 0, ""
        if size == pos:
            time.sleep(_POLL_SECONDS)
            continue
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(pos)
            buf += f.read()
            pos = f.tell()
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                yield rec
