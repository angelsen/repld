"""Kernel: background asyncio loop + IPC server + display thread.

Architecture:
  - Daemon thread runs the asyncio loop (run_forever).
  - Main thread runs the display consumer (or parks on stop event in
    --no-display mode).
  - IPC accept thread (started by ipc.start_server) handles connections;
    per-conn reader threads call Dispatcher.handle.

Pure stdlib; rich is an optional rendering backend.
"""

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
from .ipc import _pid_alive
from .protocol import Dispatcher
from .tasks import _current_task, install_tee

LOCK_PATH = Path.cwd() / ".pyrepl.lock"
DEFAULT_SOCKET_PATH = Path.cwd() / ".pyrepl.sock"


# ---------------------------------------------------------------------------
# Lock file
# ---------------------------------------------------------------------------


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


def _banner(socket_path: Path, watchdog_threshold: float) -> str:
    return (
        f"\033[90m[repld] pid={os.getpid()}  socket={socket_path}  (lock: {LOCK_PATH.name})\n"
        f"  watchdog:  loop_blocked channel push if cell holds the loop > {watchdog_threshold}s "
        f"(REPLD_LOOP_BLOCK_THRESHOLD)\n"
        f"  register:  claude mcp add -s project repld -- repld bridge\n"
        f"  launch:    claude --dangerously-load-development-channels server:repld\n"
        f"  human:     repld exec   # interactive REPL (state shared with agent)\033[0m"
    )


# ---------------------------------------------------------------------------
# Channel push
# ---------------------------------------------------------------------------


def push_channel(content: str, meta: dict | None = None) -> None:
    """Broadcast a notifications/claude/channel notification to all sessions
    AND emit a local ChannelPush event so the pane mirrors what the MCP agent
    receives. Single source of truth for every channel push."""
    meta = meta or {}
    ipc.broadcast_channel(
        {
            "jsonrpc": "2.0",
            "method": "notifications/claude/channel",
            "params": {"content": content, "meta": meta},
        }
    )
    from .events import ChannelPush

    events.emit(ChannelPush(content, {k: str(v) for k, v in meta.items()}))


def _notify(content, **meta) -> None:
    """Push a channel notification to all connected MCP sessions. meta keys become XML attributes."""
    push_channel(str(content), meta)


def _asyncio_exception_handler(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    """Route otherwise-unretrieved asyncio task exceptions to a channel push.

    Without this, user code like `asyncio.create_task(broken())` would only
    log a `Task exception was never retrieved` warning to stderr. Here we
    surface it ambient-style so the agent can react.
    """
    exc = context.get("exception")
    msg = context.get("message", "")
    task = context.get("task")
    task_name = getattr(task, "get_name", lambda: "?")() if task else "?"
    if exc is not None:
        summary = f"{type(exc).__name__}: {exc}"
    else:
        summary = msg
    push_channel(
        f"[repld] bg asyncio task error in {task_name}: {summary}",
        {
            "kind": "bg_task_error",
            "task_name": str(task_name),
            "exception": type(exc).__name__ if exc else "",
        },
    )


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
        # Probe first so `threshold` is the actual hang-detection time
        # (not threshold + interval).
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
        if stop.wait(interval):
            return


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
    label = task.get("label")
    label_str = f' "{label}"' if label else ""
    parts = [f"[repld] task {task_id}{label_str} done"]
    if delta_preview.strip():
        parts.append(delta_preview.rstrip())
    if path is not None:
        parts.append(f"[full output: {path}]")
    if task["exception"]:
        parts.append(str(task["exception"]).rstrip())
    meta_dict: dict[str, str] = {
        "kind": "task_done",
        "task_id": task_id,
        "error": "1" if task["exception"] else "0",
    }
    if label:
        meta_dict["label"] = label
    push_channel("\n".join(parts), meta_dict)


async def _run_cell(task_id: str, src: str, n: int) -> None:
    """Coroutine that runs on the bg asyncio loop.

    Sets _current_task ContextVar so that asyncio.create_task() calls inside
    user code inherit it via copy_context() — preserving per-task output
    attribution for fire-and-forget background tasks.
    """
    from . import runtime

    _current_task.set(task_id)
    task = tasks._tasks[task_id]
    # Stash the asyncio.Task handle so cancel_task can call .cancel() on it
    # directly (cf.Future.cancel() on a running threadsafe-launched task
    # doesn't propagate reliably).
    task["asyncio_task"] = asyncio.current_task()
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
        task["exception"] = type(exc).__name__
    finally:
        elapsed = (time.monotonic() - t_start) * 1000
        events.emit(CellDone(task_id, elapsed, task.get("exception")))
        tasks.finalize(task_id)
        _maybe_push_done(task_id)


async def _run_deferred(task_id: str, coro) -> None:
    """Await a user-supplied coroutine within the task lifecycle.

    Like _run_cell but skips compile/eval — just awaits the coroutine directly.
    Sets _current_task so stdout/stderr attribution works via _Tee.
    """
    _current_task.set(task_id)
    task = tasks._tasks[task_id]
    task["asyncio_task"] = asyncio.current_task()
    t_start = time.monotonic()

    try:
        await coro
    except asyncio.CancelledError:
        task["exception"] = "CancelledError"
    except BaseException as exc:
        task["exception"] = type(exc).__name__
        sys.stderr.write(traceback.format_exc())
    finally:
        elapsed = (time.monotonic() - t_start) * 1000
        events.emit(CellDone(task_id, elapsed, task.get("exception")))
        tasks.finalize(task_id)
        _maybe_push_done(task_id)


def _make_defer(loop: asyncio.AbstractEventLoop):
    """Return a defer(coro, label=None) function bound to the kernel's loop."""

    def defer(coro, label: str | None = None) -> str:
        """Schedule a coroutine as a tracked task. Returns task_id immediately.

        The task is visible to get_task and cancel. On completion, a task_done
        channel notification is pushed.
        """
        import inspect

        if not inspect.iscoroutine(coro):
            raise TypeError(
                f"defer() expects a coroutine object, got {type(coro).__name__}. "
                "Call it as: defer(my_async_fn())"
            )
        task_id, task = tasks.new_task()
        task["nudged"] = True
        task["nudge_cutoff"] = 0
        if label is not None:
            task["label"] = label
        src_label = label or "..."
        events.emit(CellStart(task_id, f"defer({src_label})", time.time()))
        asyncio.run_coroutine_threadsafe(_run_deferred(task_id, coro), loop)
        return task_id

    return defer


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

    def cancel_task(self, task_id: str) -> bool:
        """Attempt to cancel a running cell. Returns True if the cancellation
        request was scheduled. Cannot preempt tight sync loops — only
        await-yielding code is cancellable."""
        task = tasks._tasks.get(task_id)
        if task is None:
            return False
        asyncio_task = task.get("asyncio_task")
        if asyncio_task is None or asyncio_task.done():
            return False
        self.loop.call_soon_threadsafe(asyncio_task.cancel)
        return True


# ---------------------------------------------------------------------------
# Init file
# ---------------------------------------------------------------------------


def _run_init_file(path: Path, loop: asyncio.AbstractEventLoop) -> None:
    if not path.exists():
        sys.stderr.write(f"\033[31m[repld] --init file not found: {path}\033[0m\n")
        push_channel(
            f"[repld] --init file not found: {path}",
            {"kind": "init_error", "file": str(path), "reason": "not_found"},
        )
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
        tb = traceback.format_exc()
        sys.stderr.write(f"\033[31m[repld] --init {path.name} raised:\n{tb}\033[0m\n")
        push_channel(
            f"[repld] --init {path.name} raised: {tb.rstrip()}",
            {"kind": "init_error", "file": str(path)},
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
    loop.set_exception_handler(_asyncio_exception_handler)
    threading.Thread(target=loop.run_forever, daemon=True, name="repld-asyncio").start()

    # 2. Init event queue and tee (must happen before any user code runs).
    events.init_event_queue()
    install_tee()

    # 2b. Set up gist directories on sys.path with auto-reload.
    from . import gists as _gists

    _gists.install(
        [
            Path.home() / ".repld" / "gists",
            Path.cwd() / "gists",
        ]
    )

    # 3. Inject helpers into __main__.
    from . import gates
    import pydoc

    setattr(__main__, "notify", _notify)
    setattr(__main__, "defer", _make_defer(loop))
    setattr(__main__, "ask", gates.ask)
    setattr(__main__, "confirm", gates.confirm)
    setattr(__main__, "choose", gates.choose)
    # Pager-free help — pydoc's default pager forks less(1) on the kernel tty,
    # bypassing _Tee and deadlocking the asyncio loop. Helper(output=...) writes
    # directly through sys.stdout (the _Tee) so output flows to exec clients.
    setattr(__main__, "help", pydoc.Helper(output=sys.stdout))

    # Inject lazy browser builtin (zero import cost until first browser.attach()).
    try:
        from .browser import LazyBrowser

        _lazy_browser = LazyBrowser(loop)
        setattr(__main__, "browser", _lazy_browser)

        def _browser_cleanup() -> None:
            b = getattr(__main__, "browser", None)
            real = getattr(b, "_real", None) if hasattr(b, "_real") else b
            if real is not None and hasattr(real, "_session"):
                try:
                    fut = asyncio.run_coroutine_threadsafe(
                        real._session.disconnect(), loop
                    )
                    fut.result(timeout=5)
                except Exception:
                    pass

        atexit.register(_browser_cleanup)
    except ImportError:
        pass  # repld[browser] not installed — no browser builtin

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

    # 5. Loop watchdog — channel-push if the bg loop wedges (typically a
    #    cell doing sync I/O while uvicorn or similar lives on the loop).
    #    Tunable via REPLD_LOOP_BLOCK_THRESHOLD (seconds, default 5).
    stop = threading.Event()
    threshold = float(os.environ.get("REPLD_LOOP_BLOCK_THRESHOLD", "5.0"))

    # 6. Print banner (goes to sys.__stderr__ directly so it's visible even
    #    in --no-display mode before the tee is fully wired). Includes the
    #    active watchdog threshold so users know what to expect.
    stderr = sys.__stderr__
    if stderr is not None:
        stderr.write(_banner(sock_path, threshold) + "\n")
        stderr.flush()
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
