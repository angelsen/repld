"""Task registry and _Tee stdout/stderr interceptor.

Every task that produces any output gets a spill file at
$XDG_RUNTIME_DIR/repld/{pid}-{task_id}.out, opened lazily on first write.
The MCP `exec` / `get_task` responses return a small head+tail preview
sliced from that file; agents use the standard Read/Grep tools on the
spill path for anything beyond the preview.
"""

import contextvars
import io
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Literal

from .events import StdoutChunk, StderrChunk, emit

# Inline preview budget. Full output is always on disk; preview bounds only
# what's returned in the `exec` / `get_task` response body.
PREVIEW_HEAD_LINES = 5
PREVIEW_TAIL_LINES = 5
PREVIEW_MAX_BYTES = 4 * 1024  # wire budget — independent of display.VIEWER_MAX_BYTES
PREVIEW_MAX_LINE = 400  # per-line clamp for unbroken-text lines

RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "repld"
SPILL_DIR = RUNTIME_DIR

_current_task: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "repld_task_id", default=None
)
_tasks: dict[str, dict] = {}
_CLOSED = object()  # sentinel: spill file was open, now closed by pruning
_PRUNE_AGE = 300.0  # seconds after done_event before closing spill handle
_PRUNE_EVERY = 50  # run pruning every N finalize() calls
_finalize_count = 0


def _ensure_spill_dir() -> None:
    SPILL_DIR.mkdir(parents=True, exist_ok=True)


def _open_spill(task: dict, task_id: str) -> None:
    _ensure_spill_dir()
    path = SPILL_DIR / f"{os.getpid()}-{task_id}.out"
    fp = open(path, "w")
    task["spill_file"] = fp
    task["spill_path"] = str(path)


class _Tee(io.TextIOBase):
    """stdout/stderr interceptor.

    Persists writes to the active task's spill file (lazily opened on first
    write) and emits StdoutChunk / StderrChunk events. Does NOT write to
    self.real — the display thread owns sys.__stdout__.

    Async tasks spawned via asyncio.create_task() inside user code inherit
    the ContextVar via copy_context(), so fire-and-forget output stays
    attributed to the originating cell.
    """

    def __init__(self, real: io.TextIOBase, stream: Literal["stdout", "stderr"]):
        self.real = real
        self.stream = stream

    def write(self, s: str) -> int:
        if not s:
            return 0
        task_id = _current_task.get()
        task = _tasks.get(task_id) if task_id is not None else None
        if task_id is not None and task is not None:
            fp = task["spill_file"]
            if fp is None:
                _open_spill(task, task_id)
                fp = task["spill_file"]
            if fp is not _CLOSED:
                try:
                    fp.write(s)
                    fp.flush()
                except (ValueError, OSError):
                    pass  # pruned between check and write
        cls = StdoutChunk if self.stream == "stdout" else StderrChunk
        emit(cls(task_id, s))
        return len(s)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return self.real.isatty()

    def fileno(self) -> int:
        return self.real.fileno()


def install_tee() -> None:
    if not isinstance(sys.stdout, _Tee):
        sys.stdout = _Tee(sys.__stdout__, "stdout")  # type: ignore[arg-type]
    if not isinstance(sys.stderr, _Tee):
        sys.stderr = _Tee(sys.__stderr__, "stderr")  # type: ignore[arg-type]


def new_task() -> tuple[str, dict]:
    task_id = uuid.uuid4().hex[:12]
    task: dict = {
        "done_event": threading.Event(),
        "exception": None,
        "spill_file": None,
        "spill_path": None,
        "nudged": False,
        "nudge_cutoff": 0,
        "asyncio_task": None,  # asyncio.Task handle, set from inside _run_cell
        "label": None,
    }
    _tasks[task_id] = task
    return task_id, task


def _read_from(task: dict, offset: int = 0) -> str:
    """Flush the task's spill file (if any) and read it from *offset*."""
    path = task["spill_path"]
    if path is None:
        return ""
    fp = task.get("spill_file")
    if fp is not None:
        try:
            fp.flush()
        except Exception:
            pass
    with open(path, "r") as f:
        f.seek(offset)
        return f.read()


def _clip_line(line: str) -> str:
    if len(line) <= PREVIEW_MAX_LINE:
        return line
    keep = PREVIEW_MAX_LINE - 40
    suffix = f"… (line truncated, {len(line)} chars total)"
    nl = "\n" if line.endswith("\n") else ""
    return line[:keep] + suffix + nl


def _make_preview(full: str) -> tuple[str, bool]:
    """Build a head+tail preview with per-line and total-byte clamps.

    Three regimes:
      - len(full) ≤ MAX_BYTES: return as-is, untruncated.
      - many lines: head/tail slice with elision marker; each line clamped.
      - few but wide lines: per-line clamp catches the giant ones.
    """
    if not full:
        return "", False
    if len(full) <= PREVIEW_MAX_BYTES:
        return full, False
    lines = full.splitlines(keepends=True)
    if len(lines) > PREVIEW_HEAD_LINES + PREVIEW_TAIL_LINES:
        head = "".join(_clip_line(ln) for ln in lines[:PREVIEW_HEAD_LINES])
        tail = "".join(_clip_line(ln) for ln in lines[-PREVIEW_TAIL_LINES:])
        elided = len(lines) - PREVIEW_HEAD_LINES - PREVIEW_TAIL_LINES
        sep = f"… {elided} lines elided …\n"
        return head + sep + tail, True
    return "".join(_clip_line(ln) for ln in lines), True


def spill_text(text: str, label: str = "output") -> dict:
    """Write text to a spill file, return preview + path.

    Reusable by tools, resources, and exec. Same preview budget as exec.
    Returns {"text": preview, "spill_path": path_or_None, "truncated": bool}.
    """
    if not text:
        return {"text": "", "spill_path": None, "truncated": False}
    preview, truncated = _make_preview(text)
    spill_path = None
    if len(text) > PREVIEW_MAX_BYTES:
        _ensure_spill_dir()
        tid = uuid.uuid4().hex[:12]
        path = SPILL_DIR / f"{os.getpid()}-{label}-{tid}.out"
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            f.write(text)
        tmp.rename(path)  # atomic on same filesystem
        spill_path = str(path)
    return {"text": preview, "spill_path": spill_path, "truncated": truncated}


def snapshot(task_id: str) -> dict:
    task = _tasks.get(task_id)
    if task is None:
        return {"task_id": task_id, "error": "unknown task_id"}
    full = _read_from(task)
    text, truncated = _make_preview(full)
    return {
        "task_id": task_id,
        "text": text,
        "truncated": truncated,
        "spilled": task["spill_path"] is not None,
        "spill_path": task["spill_path"],
        "exception": task["exception"],
        "done": task["done_event"].is_set(),
        "label": task.get("label"),
    }


def mark_nudged(task_id: str) -> None:
    task = _tasks.get(task_id)
    if task is None:
        return
    task["nudged"] = True
    fp = task.get("spill_file")
    if fp is not None:
        try:
            fp.flush()
            task["nudge_cutoff"] = fp.tell()
        except Exception:
            task["nudge_cutoff"] = 0
    else:
        task["nudge_cutoff"] = 0


def finalize(task_id: str) -> None:
    global _finalize_count
    task = _tasks.get(task_id)
    if task is None:
        return
    # Don't close spill_file immediately: background asyncio tasks spawned by
    # this cell may keep printing after the cell returns (they inherit task_id
    # via the ContextVar). Handles are closed by _prune_spill_files after
    # _PRUNE_AGE seconds.
    fp = task.get("spill_file")
    if fp is not None and fp is not _CLOSED:
        try:
            fp.flush()
        except Exception:
            pass
    task["done_event"].set()
    task["done_at"] = time.monotonic()
    _finalize_count += 1
    if _finalize_count % _PRUNE_EVERY == 0:
        _prune_spill_files()


def _prune_spill_files() -> None:
    """Close spill file handles on tasks completed more than _PRUNE_AGE ago."""
    now = time.monotonic()
    for task in list(_tasks.values()):
        done_at = task.get("done_at")
        if done_at is None or now - done_at < _PRUNE_AGE:
            continue
        fp = task.get("spill_file")
        if fp is None or fp is _CLOSED:
            continue
        try:
            fp.close()
        except Exception:
            pass
        task["spill_file"] = _CLOSED
