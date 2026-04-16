"""Task registry and _Tee stdout/stderr interceptor.

Every task that produces any output gets a spill file at
$XDG_RUNTIME_DIR/repld/{pid}-{task_id}.out, opened lazily on first write.
The MCP `exec` / `get_task` responses return a small head+tail preview
sliced from that file; `read_spill` exposes arbitrary byte ranges.
"""

import contextvars
import io
import os
import sys
import threading
import uuid
from pathlib import Path
from typing import Literal

from .events import StdoutChunk, StderrChunk, emit

# Inline preview budget. Full output is always on disk; preview bounds only
# what's returned in the `exec` / `get_task` response body.
PREVIEW_HEAD_LINES = 5
PREVIEW_TAIL_LINES = 5
PREVIEW_MAX_BYTES = 4 * 1024
PREVIEW_MAX_LINE = 400  # per-line clamp for unbroken-text lines

SPILL_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "repld"

_current_task: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "repld_task_id", default=None
)
_tasks: dict[str, dict] = {}


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
            if task["spill_file"] is None:
                _open_spill(task, task_id)
            task["spill_file"].write(s)
            task["spill_file"].flush()
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
    }
    _tasks[task_id] = task
    return task_id, task


def _read_full(task: dict) -> str:
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


def snapshot(task_id: str) -> dict:
    task = _tasks.get(task_id)
    if task is None:
        return {"task_id": task_id, "error": "unknown task_id"}
    full = _read_full(task)
    text, truncated = _make_preview(full)
    return {
        "task_id": task_id,
        "text": text,
        "truncated": truncated,
        "spilled": task["spill_path"] is not None,
        "spill_path": task["spill_path"],
        "exception": task["exception"],
        "done": task["done_event"].is_set(),
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
    task = _tasks.get(task_id)
    if task is None:
        return
    # Don't close spill_file: background asyncio tasks spawned by this cell
    # may keep printing after the cell returns (and they inherit task_id via
    # the ContextVar). The OS reaps the fd at process exit.
    fp = task.get("spill_file")
    if fp is not None:
        try:
            fp.flush()
        except Exception:
            pass
    task["done_event"].set()
