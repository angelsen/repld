"""Kernel: background asyncio loop + IPC server + display thread.

Architecture:
  - Daemon thread runs the asyncio loop (run_forever).
  - Main thread runs the display consumer (or parks on stop event in
    --no-display mode).
  - IPC accept thread (started by ipc.start_server) handles connections;
    per-conn reader threads call Dispatcher.handle.

No IPython, no prompt_toolkit. Pure stdlib + optional rich.
"""

from __future__ import annotations

import __main__
import asyncio
import atexit
import concurrent.futures
import json
import os
import signal
import sys
import threading
import time
import traceback
from pathlib import Path

from . import events, ipc, tasks
from .events import CellDone, CellStart
from .protocol import Dispatcher
from .tasks import _current_task, install_tee

LOCK_PATH = Path.cwd() / ".pyrepl.lock"
DEFAULT_SOCKET_PATH = Path.cwd() / ".pyrepl.sock"


# ---------------------------------------------------------------------------
# Lock file
# ---------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _check_existing_kernel() -> None:
    """Refuse to start if another repld kernel owns this cwd."""
    if not LOCK_PATH.exists():
        return
    try:
        lock = json.loads(LOCK_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return
    pid = lock.get("pid", -1)
    if _pid_alive(pid):
        raise SystemExit(
            f"\033[31m[repld] another kernel (pid={pid}) is running in "
            f"{Path.cwd()}. Stop it or remove {LOCK_PATH.name} if stale.\033[0m"
        )


def _write_lockfile(socket_path: Path) -> None:
    info = {
        "pid": os.getpid(),
        "socket_path": str(socket_path),
        "cwd": os.getcwd(),
        "started": time.time(),
    }
    LOCK_PATH.write_text(json.dumps(info))


def _cleanup_lockfile() -> None:
    try:
        LOCK_PATH.unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------


def _banner(socket_path: Path) -> str:
    return (
        f"\033[90m[repld] pid={os.getpid()}  socket={socket_path}  (lock: {LOCK_PATH.name})\n"
        f"  register:  claude mcp add -s project repld -- repld bridge\n"
        f"  launch:    claude --dangerously-load-development-channels server:repld\033[0m"
    )


# ---------------------------------------------------------------------------
# Channel push
# ---------------------------------------------------------------------------


def push_channel(content: str, meta: dict | None = None) -> None:
    """Broadcast a notifications/claude/channel notification to all sessions."""
    ipc.broadcast_channel(
        {
            "jsonrpc": "2.0",
            "method": "notifications/claude/channel",
            "params": {"content": content, "meta": meta or {}},
        }
    )


def _notify(content, **meta) -> None:
    push_channel(str(content), meta)
    # Also emit to the display queue so the local log shows it.
    from .events import ChannelPush

    events.emit(ChannelPush(str(content), {k: str(v) for k, v in meta.items()}))


# ---------------------------------------------------------------------------
# Loop watchdog
# ---------------------------------------------------------------------------


def _loop_watchdog(
    loop: asyncio.AbstractEventLoop,
    stop: threading.Event,
    threshold: float,
    interval: float,
) -> None:
    """Daemon thread that detects when the bg asyncio loop is wedged.

    Common cause: a cell that does sync I/O (e.g. `urlopen`) while uvicorn
    or similar lives on the same loop — both deadlock. We schedule a no-op
    coroutine each `interval`s; if it doesn't return within `threshold`s
    we push a channel notification with the active task ids so the agent
    knows what's stuck.
    """
    while not stop.is_set():
        if stop.wait(interval):
            return
        future = asyncio.run_coroutine_threadsafe(asyncio.sleep(0), loop)
        try:
            future.result(timeout=threshold)
        except concurrent.futures.TimeoutError:
            active = [
                tid for tid, t in tasks._tasks.items() if not t["done_event"].is_set()
            ]
            active_str = ",".join(active) if active else "none"
            push_channel(
                f"[repld] event loop blocked > {threshold}s "
                f"(active tasks: {active_str}) — likely sync I/O on the "
                "shared loop; wrap blocking calls in asyncio.to_thread()",
                {
                    "kind": "loop_blocked",
                    "threshold_s": str(threshold),
                    "active_tasks": active_str,
                },
            )
            # Wait (without spamming) for the loop to recover before the
            # next probe.
            try:
                future.result(timeout=300)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Cell execution (bg loop coroutines)
# ---------------------------------------------------------------------------

_exec_count = 0


def _maybe_push_done(task_id: str) -> None:
    """Push channel notification for nudged tasks on completion."""
    task = tasks._tasks.get(task_id)
    if task is None or not task.get("nudged"):
        return
    cutoff = task.get("nudge_cutoff", 0)
    delta = ""
    path = task.get("spill_path")
    if path is not None:
        fp = task.get("spill_file")
        if fp is not None:
            try:
                fp.flush()
            except Exception:
                pass
        try:
            with open(path, "r") as f:
                f.seek(cutoff)
                delta = f.read()
        except Exception:
            delta = ""
    delta_preview, _truncated = tasks._make_preview(delta)
    parts = [f"[repld] task {task_id} done"]
    if delta_preview.strip():
        parts.append(delta_preview.rstrip())
    if path is not None:
        parts.append(f"[full output: {path}]")
    if task["exception"]:
        parts.append(str(task["exception"]).rstrip())
    push_channel(
        "\n".join(parts),
        {
            "kind": "task_done",
            "task_id": task_id,
            "error": "1" if task["exception"] else "0",
        },
    )


async def _run_cell(task_id: str, src: str, n: int) -> None:
    """Coroutine that runs on the bg asyncio loop.

    Sets _current_task ContextVar so that asyncio.create_task() calls inside
    user code inherit it via copy_context() — preserving per-task output
    attribution for fire-and-forget background tasks.
    """
    from . import runtime

    _current_task.set(task_id)
    task = tasks._tasks[task_id]
    t_start = time.monotonic()

    try:
        compiled = runtime.compile_cell(src, task_id)
    except SyntaxError:
        tb = traceback.format_exc()
        sys.stderr.write(tb)
        task["exception"] = "SyntaxError"
        elapsed = (time.monotonic() - t_start) * 1000
        events.emit(CellDone(task_id, elapsed, "SyntaxError"))
        tasks.finalize(task_id)
        _maybe_push_done(task_id)
        return

    try:
        await runtime.run_cell(compiled, __main__.__dict__, n)
    except BaseException as exc:
        if not isinstance(exc, SystemExit):
            task["exception"] = type(exc).__name__
        else:
            raise
    finally:
        elapsed = (time.monotonic() - t_start) * 1000
        events.emit(CellDone(task_id, elapsed, task.get("exception")))
        tasks.finalize(task_id)
        _maybe_push_done(task_id)


# ---------------------------------------------------------------------------
# KernelContext (implements protocol.KernelContext)
# ---------------------------------------------------------------------------


class _Context:
    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop

    def start_task(self, src: str):
        global _exec_count
        _exec_count += 1
        n = _exec_count
        task_id, task = tasks.new_task()
        events.emit(CellStart(task_id, src, time.time()))
        asyncio.run_coroutine_threadsafe(_run_cell(task_id, src, n), self.loop)
        return task_id, task["done_event"]

    def snapshot(self, task_id: str) -> dict:
        return tasks.snapshot(task_id)

    def mark_nudged(self, task_id: str) -> None:
        tasks.mark_nudged(task_id)


# ---------------------------------------------------------------------------
# Init file
# ---------------------------------------------------------------------------


def _run_init_file(path: Path, loop: asyncio.AbstractEventLoop) -> None:
    if not path.exists():
        sys.stderr.write(f"\033[31m[repld] --init file not found: {path}\033[0m\n")
        return
    global _exec_count
    _exec_count += 1
    n = _exec_count
    src = path.read_text()
    # Set __main__.__file__ so the init file's Path(__file__) works the way
    # `python path/to/script.py` would.
    __main__.__file__ = str(path.resolve())
    task_id, task = tasks.new_task()
    events.emit(CellStart(task_id, src, time.time()))
    future = asyncio.run_coroutine_threadsafe(_run_cell(task_id, src, n), loop)
    # Block until the init file completes (including any run_until_complete
    # semantics — background tasks it spawned stay alive on the loop).
    try:
        future.result(timeout=30)
    except Exception:
        sys.stderr.write(
            f"\033[31m[repld] --init {path.name} raised:\n{traceback.format_exc()}\033[0m\n"
        )


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


def _shutdown(loop: asyncio.AbstractEventLoop) -> None:
    loop.call_soon_threadsafe(loop.stop)
    ipc.stop_server()


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def run_kernel(
    socket_path: str | None = None,
    *,
    display: bool = True,
    init_file: str | None = None,
) -> int:
    _check_existing_kernel()

    # 1. Start the asyncio loop on a daemon thread.
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True, name="repld-asyncio").start()

    # 2. Init event queue and tee (must happen before any user code runs).
    events.init_event_queue()
    install_tee()

    # 3. Inject helpers into __main__.
    from . import gates

    setattr(__main__, "notify", _notify)
    setattr(__main__, "ask", gates.ask)
    setattr(__main__, "confirm", gates.confirm)
    setattr(__main__, "choose", gates.choose)

    # 4. Wire IPC.
    sock_path = Path(socket_path) if socket_path else DEFAULT_SOCKET_PATH
    ctx = _Context(loop)
    dispatcher = Dispatcher(ctx)

    def _handler(req: dict, session: ipc.Session) -> dict | None:
        return dispatcher.handle(req, session)

    ipc.start_server(sock_path, _handler)
    _write_lockfile(sock_path)
    atexit.register(_cleanup_lockfile)
    atexit.register(ipc.stop_server)

    # 5. Print banner (goes to sys.__stderr__ directly so it's visible even
    #    in --no-display mode before the tee is fully wired).
    stderr = sys.__stderr__
    if stderr is not None:
        stderr.write(_banner(sock_path) + "\n")
        stderr.flush()

    # 6. Loop watchdog — channel-push if the bg loop wedges (typically a
    #    cell doing sync I/O while uvicorn or similar lives on the loop).
    #    Tunable via REPLD_LOOP_BLOCK_THRESHOLD (seconds, default 5).
    stop = threading.Event()
    threshold = float(os.environ.get("REPLD_LOOP_BLOCK_THRESHOLD", "5.0"))
    threading.Thread(
        target=_loop_watchdog,
        args=(loop, stop, threshold, 1.0),
        daemon=True,
        name="repld-watchdog",
    ).start()

    # 7. Optionally run init file.
    if init_file:
        _run_init_file(Path(init_file), loop)

    # 8. Main thread: display or headless.
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    if display:
        from .display import run_display

        run_display(stop)
    else:
        from .display import make_drainer

        make_drainer(stop)
        stop.wait()

    _shutdown(loop)
    return 0
