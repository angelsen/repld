"""Task registry and _Tee stdout/stderr interceptor.

Every task that produces any output gets a spill file at
$XDG_RUNTIME_DIR/repld/{pid}-{task_id}.out, opened lazily on first write.
The MCP `exec` / `get_task` responses return a small head+tail preview
sliced from that file; agents use the standard Read/Grep tools on the
spill path for anything beyond the preview.

A spill lives for _EVICT_AGE after its cell completes, then `_prune_spill_files`
drops the task entry and unlinks the file. That bound is what keeps a kernel
running for weeks from filling tmpfs with one file per cell. `_sweep_orphans`
applies the same bound to the runtime files that have no task entry to hang
off — resource spills and browser screenshots.
"""

import contextvars
import io
import os
import sys
import threading
import time
import uuid
from typing import Literal

from . import state
from .events import StdoutChunk, StderrChunk, emit
from .paths import RUNTIME_DIR, ensure_runtime_dir
from .state import open_private

# Inline preview budget. Full output is always on disk; preview bounds only
# what's returned in the `exec` / `get_task` response body.
PREVIEW_HEAD_LINES = 5
PREVIEW_TAIL_LINES = 5
PREVIEW_MAX_BYTES = 4 * 1024  # wire budget — independent of display.VIEWER_MAX_BYTES
PREVIEW_MAX_LINE = 400  # per-line clamp for unbroken-text lines

_current_task: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "repld_task_id", default=None
)
_tasks: dict[str, dict] = {}
_tasks_lock = threading.Lock()
_CLOSED = object()  # sentinel: spill file was open, now closed by pruning
_PRUNE_AGE = 300.0  # seconds after done_event before closing spill handle
_EVICT_AGE = 3600.0  # seconds after done_event before dropping the entry entirely
_PRUNE_EVERY = 50  # finalize() calls between prune sweeps
_PRUNE_INTERVAL = 600.0  # seconds between prune sweeps, regardless of call count
_finalize_count = 0
_last_prune = 0.0


def _open_spill(task: dict, task_id: str) -> io.TextIOWrapper:
    # 0600, not 0644: a spill holds whatever the cell printed — tokens, API
    # responses, query results — and under the /tmp fallback the directory is
    # the only other thing standing between it and other local users.
    ensure_runtime_dir()
    path = RUNTIME_DIR / f"{os.getpid()}-{task_id}.out"
    fp = open_private(path)
    task["spill_file"] = fp
    task["spill_path"] = str(path)
    return fp


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
                with _tasks_lock:
                    fp = task["spill_file"]
                    if fp is None:
                        fp = _open_spill(task, task_id)
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


def set_current_task(task_id: str | None) -> None:
    """Bind the running coroutine's ContextVar so `_Tee.write` attributes
    output (and async descendants via copy_context()) to *task_id*."""
    _current_task.set(task_id)


def current_task_id() -> str | None:
    """The task whose context the calling code is running in, if any.

    Lets `defer()` inherit the originating session of the cell that called it,
    so a background task's completion push lands where the work was asked for.
    """
    return _current_task.get()


def new_task(origin: object = None) -> tuple[str, dict]:
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
        # ipc.Session that asked for this work, or None for ambient tasks
        # (repld_init.py, @every bodies). Drives targeted vs. broadcast push.
        "origin": origin,
    }
    with _tasks_lock:
        _tasks[task_id] = task
    return task_id, task


def get(task_id: str) -> dict | None:
    """Thread-safe lookup — callers outside this module should use this
    instead of reaching into `_tasks` directly."""
    with _tasks_lock:
        return _tasks.get(task_id)


def items() -> list[tuple[str, dict]]:
    """Thread-safe snapshot of all tasks, for iteration (watchdog, dashboard)."""
    with _tasks_lock:
        return list(_tasks.items())


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
    # Match open_private's utf-8; errors="replace" because seeking to a byte
    # offset can land mid-codepoint on a partially-flushed spill.
    with open(path, "r", encoding="utf-8", errors="replace") as f:
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


def preview_since(task: dict, offset: int) -> tuple[str, bool]:
    """Head+tail preview of spill output written since *offset*.

    Callers outside this module should use this instead of reaching into
    `_read_from`/`_make_preview` directly.
    """
    try:
        delta = _read_from(task, offset)
    except Exception:
        return "", False
    return _make_preview(delta)


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
        ensure_runtime_dir()
        tid = uuid.uuid4().hex[:12]
        path = RUNTIME_DIR / f"{os.getpid()}-{label}-{tid}.out"
        tmp = path.with_suffix(".tmp")
        with open_private(tmp) as f:
            f.write(text)
        tmp.rename(path)  # atomic on same filesystem
        spill_path = str(path)
    return {"text": preview, "spill_path": spill_path, "truncated": truncated}


def spill_marker(path: str) -> str:
    """Canonical '[full output: {path}]' marker.

    Agents and tests grep for this exact shape — every wire-facing producer
    must build it here.  (display.py's 'full: {path}' viewer-cap notice is a
    deliberately distinct, terminal-only variant.)
    """
    return f"[full output: {path}]"


def snapshot(task_id: str) -> dict | None:
    """State dict for a task, or None for an unknown task_id."""
    task = get(task_id)
    if task is None:
        return None
    # Full re-read per poll is fine at current poll rates; revisit with a
    # head-cache + tail-seek if multi-MB spills under polling become common.
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
    task = get(task_id)
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
    task = get(task_id)
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
    # Drop the asyncio.Task reference now — it's no longer needed once the
    # cell is done, and holding it keeps the whole coroutine frame chain alive.
    task["asyncio_task"] = None
    _finalize_count += 1
    if _finalize_count % _PRUNE_EVERY == 0:
        _prune_spill_files()


def maybe_prune() -> None:
    """Time-triggered prune sweep, for callers outside the finalize() path.

    `finalize`'s every-_PRUNE_EVERY-calls trigger only fires while cells are
    running, so a kernel that executes a handful of cells and then idles for a
    week never reaches it — the entries and their spill files sit there for the
    life of the process, and `_EVICT_AGE` stops describing anything real. The
    kernel's watchdog thread ticks regardless of load, so it calls this; the
    interval check keeps that cheap.
    """
    global _last_prune
    now = time.monotonic()
    if now - _last_prune < _PRUNE_INTERVAL:
        return
    _last_prune = now
    _prune_spill_files()


def _close_spill(task: dict) -> None:
    """Close the task's spill handle if it is still open. Idempotent."""
    fp = task.get("spill_file")
    if fp is None or fp is _CLOSED:
        return
    try:
        fp.close()
    except Exception:
        pass
    task["spill_file"] = _CLOSED


def _prune_spill_files() -> None:
    """Close spill file handles on tasks completed more than _PRUNE_AGE ago,
    and drop entries — and their spill files — once they're older than
    _EVICT_AGE.

    Both halves exist because a kernel is meant to run for weeks: without the
    eviction it accumulates one dict entry per exec/defer call forever, and
    without the unlink it accumulates one file per output-producing cell.
    `state.sweep_dead_pid_files` can't help there — it only reclaims what a
    *dead* pid left behind, and a live kernel's own files are untouchable by
    construction. Evicting the entry without unlinking would in fact make them
    unreclaimable until the kernel exits.

    _EVICT_AGE is therefore also the retention window on a spill path handed
    to an agent: roughly an hour on, `[full output: …]` from an old response
    stops resolving. Anything still wanted by then belongs somewhere durable.
    Roughly, because eviction happens on the next sweep past the deadline, not
    at it — whichever of `finalize`'s call counter or `maybe_prune`'s interval
    comes first.

    The registry can only account for what it knows about, so `_sweep_orphans`
    finishes the job for the runtime files that never get a task entry.
    """
    now = time.monotonic()
    evict: list[tuple[str, str | None]] = []
    for task_id, task in items():
        done_at = task.get("done_at")
        if done_at is None:
            continue
        if now - done_at >= _EVICT_AGE:
            _close_spill(task)
            evict.append((task_id, task.get("spill_path")))
            continue
        if now - done_at < _PRUNE_AGE:
            continue
        _close_spill(task)
    if evict:
        with _tasks_lock:
            for task_id, _ in evict:
                _tasks.pop(task_id, None)
        for _, path in evict:
            if path is None:
                continue
            try:
                os.unlink(path)
            except OSError:
                pass
    _sweep_orphans()


def _sweep_orphans() -> None:
    """Reclaim this kernel's own runtime files that no task entry owns.

    Three producers write `{pid}-…` into RUNTIME_DIR without going through the
    task registry: `spill_text` (oversized `resources/read` responses, and any
    browser tool response past the preview budget — `browser_tree` and
    `browser_network` routinely are), and `Tab.screenshot`. Nothing above
    reaches them: `_prune_spill_files` walks `_tasks`, and
    `state.sweep_dead_pid_files` only reclaims what a *dead* pid left, so for a
    kernel running the weeks it is designed to run these were permanent.

    Same `_EVICT_AGE` as a task spill, deliberately: both hand the agent a path
    in a tool response, so both should stay resolvable for the same hour. Live
    tasks' spill paths are excluded — they age out by `done_at` above, and a
    long-idle task holding an open handle would otherwise have the file pulled
    out from under it.
    """
    live = {t["spill_path"] for _tid, t in items() if t["spill_path"] is not None}
    try:
        state.sweep_own_stale_files(RUNTIME_DIR, max_age=_EVICT_AGE, keep=live)
    except Exception:
        pass  # reclamation is housekeeping; never let it break a running cell
