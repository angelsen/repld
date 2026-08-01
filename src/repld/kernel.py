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
import contextlib
import inspect
import itertools
import json
import os
import signal
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

from . import events, eventlog, gates, ipc, paths, sessions, state, tasks
from .channel import push_channel, push_kind as _push
from .events import CellDone, CellStart
from .paths import default_socket_path, lock_for
from .state import atomic_write_json
from .protocol import Dispatcher
from .tasks import install_tee

# Re-exported: `from repld.kernel import push_channel` is a plausible line in
# a gist, so the name stays importable here though it lives in channel.py.
__all__ = ["push_channel", "run_kernel"]

# ---------------------------------------------------------------------------
# Lock file
# ---------------------------------------------------------------------------


def _claim_project(socket_path: Path) -> int:
    """Take the one-kernel-per-project flock, or exit 0 if we lost the race.

    Losing is not an error: a bridge that raced another bridge (or a human who
    started `repld` while a headless kernel was already up) should just talk to
    the incumbent. Exiting 0 without touching the winner's lockfile is what
    makes an externally-started kernel adopted rather than competed with.
    """
    fd = state.acquire_lock(paths.flock_for(socket_path))
    if fd is None:
        holder = state.read_lock(lock_for(socket_path))
        pid = holder.get("pid") if isinstance(holder, dict) else "?"
        stderr = sys.__stderr__
        if stderr is not None:
            stderr.write(
                f"\033[90m[repld] kernel already running for this project "
                f"(pid={pid}) — nothing to do\033[0m\n"
            )
            stderr.flush()
        raise SystemExit(0)
    return fd


def _write_lockfile(socket_path: Path, dashboard_port: int | None = None) -> None:
    info: dict[str, object] = {
        "pid": os.getpid(),
        "socket_path": str(socket_path),
        "cwd": os.getcwd(),
        "started_at": time.time(),
        # Which interpreter this kernel actually got, so `repld status` can
        # flag one that cannot import the project. No launch command is stored
        # alongside it: every path that spawns a kernel now re-execs through
        # `bind.rebind_exec` first, so `sys.executable` is already the bound
        # interpreter at spawn time and a fresh `uv run` overlay is built on
        # demand — replaying a recorded argv would only pin a cache directory
        # that `uv cache prune` is free to delete.
        "python": f"{sys.version_info[0]}.{sys.version_info[1]}.{sys.version_info[2]}",
        "executable": sys.executable,
    }
    if dashboard_port is not None:
        info["dashboard_port"] = dashboard_port
    # 0600 to match the session file, which carries the same facts. The project
    # dir is already 0700, so this is defence in depth rather than the barrier.
    atomic_write_json(lock_for(socket_path), info, chmod=0o600)


def _write_cache(socket_path: Path) -> None:
    """Persist the computed instructions/tools/resources for the bridge.

    Unlike `kernel.lock`, this file is deliberately never cleaned up on exit:
    it is what lets the *next* bridge answer MCP discovery before a kernel
    exists. Superseded by the next kernel's boot, not by this one's shutdown.
    """
    from .protocol import build_discovery_cache

    try:
        cache = build_discovery_cache()
    except Exception as e:
        # Discovery must never take boot down — a missing cache just means
        # the next bridge falls back to its static tool set.
        print(f"repld: failed to build discovery cache: {e}", file=sys.stderr)
        return
    atomic_write_json(paths.cache_for(socket_path), cache, chmod=0o600)


# Set once the project bootstrap has run (or immediately, when there is none).
# `_run_cell` waits on it; see its docstring for why the wait lives there and
# not in the socket bind.
_init_done = asyncio.Event()

_active_lock_path: Path | None = None
# flock fd for the one-kernel-per-project mutex. Module-level because it must
# stay open for the process's whole life — closing it releases the lock.
_project_lock_fd: int | None = None


# ---------------------------------------------------------------------------
# .env loading
# ---------------------------------------------------------------------------


def load_dotenv() -> None:
    """Load KEY=VALUE pairs from ./.env into os.environ (stdlib only).

    Skips comments, blank lines, and export prefixes. Strips surrounding
    quotes. Does NOT override existing env vars — so a var already set, by an
    earlier call or by the shell, wins.

    Public because the kernel reads `./.env` exactly once, at boot, while
    `./gists` reload themselves on mtime. A value written to `.env` afterwards
    is invisible until something asks: `from repld import load_dotenv`. The
    no-override rule means a var captured while empty stays empty — clear it
    first (`os.environ.pop("KEY", None)`) if you are correcting one.
    """
    p = Path.cwd() / ".env"
    if not p.is_file():
        return
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
            val = val[1:-1]
        if key and key not in os.environ:
            os.environ[key] = val


def _cleanup_lockfile() -> None:
    if _active_lock_path is None:
        return
    try:
        _active_lock_path.unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------


def _banner(
    socket_path: Path,
    watchdog_threshold: float,
    kill_threshold: float,
    dashboard_port: int | None = None,
) -> str:
    lines = [
        f"\033[90m[repld] pid={os.getpid()}  socket={socket_path}",
        f"  watchdog:  loop_blocked channel push if cell holds the loop > {watchdog_threshold}s "
        f"(REPLD_LOOP_BLOCK_THRESHOLD)",
        f"  kill:      longest-running task cancelled if loop blocked > {kill_threshold}s "
        f"(REPLD_LOOP_KILL_THRESHOLD)",
    ]
    if dashboard_port is not None:
        # The port, not a URL: `GET /` needs the API token now, and this banner
        # goes to the systemd journal on a service-spawned kernel — a readable
        # destination that a credential has no business reaching. `repld
        # dashboard` reads the token from the 0600 hint file instead.
        lines.append(
            f"  dashboard: port {dashboard_port} — open with \033[0m\033[4mrepld "
            f"dashboard\033[0m\033[90m"
        )
    lines += [
        "  register:  claude mcp add repld -- repld bridge",
        "  launch:    claude --dangerously-load-development-channels server:repld",
        "  human:     repld exec   # interactive REPL (state shared with agent)",
        "  observe:   repld log -f · repld status · repld dashboard\033[0m",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Channel push
# ---------------------------------------------------------------------------


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
    _push(
        f"[repld] bg asyncio task error in {task_name}: {summary}",
        "bg_task_error",
        task_name=str(task_name),
        exception=type(exc).__name__ if exc else "",
    )


# ---------------------------------------------------------------------------
# Loop watchdog
# ---------------------------------------------------------------------------


def _probe_future(loop: asyncio.AbstractEventLoop) -> "concurrent.futures.Future[None]":
    """Schedule the watchdog's no-op liveness probe under a repld- name.

    Named at task-creation time (not from inside the coroutine) so it's
    already excluded from _pick_victim's candidate filter even while
    pending — a plain run_coroutine_threadsafe(asyncio.sleep(0), loop)
    creates an anonymous Task that the fallback victim search can select
    instead of the actual offending task.
    """
    fut: "concurrent.futures.Future[None]" = concurrent.futures.Future()

    def _start() -> None:
        task = loop.create_task(asyncio.sleep(0), name="repld-watchdog-probe")
        task.add_done_callback(
            lambda _t: fut.set_result(None) if not fut.done() else None
        )

    loop.call_soon_threadsafe(_start)
    return fut


def _pick_victim(loop: asyncio.AbstractEventLoop) -> "asyncio.Task[object] | None":
    """Pick the oldest active user task to cancel.

    Prefers tracked cell/defer tasks (insertion-ordered in tasks.items(),
    asyncio.Task referenced directly via task["asyncio_task"]). Falls back
    to any non-internal loop task — typically an @every ticker — sorted by
    name for determinism.
    """
    for _tid, task in tasks.items():
        if task["done_event"].is_set():
            continue
        atask = task.get("asyncio_task")
        if atask is not None and not atask.done():
            return atask
    candidates = sorted(
        (t for t in asyncio.all_tasks(loop) if not t.get_name().startswith("repld-")),
        key=lambda t: t.get_name(),
    )
    return candidates[0] if candidates else None


def _loop_watchdog(
    loop: asyncio.AbstractEventLoop,
    stop: threading.Event,
    threshold: float,
    kill_threshold: float,
    interval: float,
) -> None:
    """Daemon thread that detects when the bg asyncio loop is wedged.

    Common cause: a cell that does sync I/O (e.g. `urlopen`) while uvicorn
    or similar lives on the same loop — both deadlock. We schedule a no-op
    coroutine each `interval`s; if it doesn't return within `threshold`s
    we push a channel notification with the active task ids so the agent
    knows what's stuck.

    After the warn at `threshold`, we wait up to `kill_threshold` total. If
    the loop is still blocked by then, we cancel the longest-running
    non-internal asyncio task.
    """
    while not stop.is_set():
        # Piggy-backed on the one thread that ticks whether or not the kernel
        # is doing anything: finished tasks' spill entries are otherwise only
        # reclaimed by running *more* cells (tasks.finalize), which an idle
        # kernel never does.
        tasks.maybe_prune()
        # Probe first so `threshold` is the actual hang-detection time
        # (not threshold + interval).
        future = _probe_future(loop)
        try:
            future.result(timeout=threshold)
        except concurrent.futures.TimeoutError:
            active = [tid for tid, t in tasks.items() if not t["done_event"].is_set()]
            active_str = ",".join(active) if active else "none"
            _push(
                f"[repld] event loop blocked > {threshold}s "
                f"(active tasks: {active_str}) — likely sync I/O on the "
                "shared loop; wrap blocking calls in asyncio.to_thread()",
                "loop_blocked",
                threshold_s=str(threshold),
                active_tasks=active_str,
            )
            # Escalate: wait up to kill_threshold total, then cancel the
            # longest-running non-internal task.
            remaining = kill_threshold - threshold
            try:
                future.result(timeout=remaining)
            except concurrent.futures.TimeoutError:
                victim = _pick_victim(loop)
                if victim is not None:
                    victim_name = victim.get_name()
                    loop.call_soon_threadsafe(victim.cancel)
                    # "requested", not "killed": the cancellation is a callback
                    # on the loop we just declared wedged, so it cannot run
                    # until the loop moves again. Reporting a completed kill
                    # would tell the agent a task is gone while it may keep
                    # running for minutes.
                    _push(
                        f"[repld] cancellation requested for blocked task: "
                        f"{victim_name} (takes effect when the loop unblocks)",
                        "loop_kill",
                        task=victim_name,
                    )
        if stop.wait(interval):
            return


# ---------------------------------------------------------------------------
# Cell execution (bg loop coroutines)
# ---------------------------------------------------------------------------

_exec_count = itertools.count(1)


def _maybe_push_done(task_id: str) -> None:
    """Push channel notification for nudged tasks on completion."""
    task = tasks.get(task_id)
    if task is None or not tasks.claim_done_push(task_id):
        return
    cutoff = task.get("nudge_cutoff", 0)
    path = task.get("spill_path")
    delta_preview, _truncated = tasks.preview_since(task, cutoff)
    label = task.get("label")
    label_str = f' "{label}"' if label else ""
    parts = [f"[repld] task {task_id}{label_str} done"]
    if delta_preview.strip():
        parts.append(delta_preview.rstrip())
    if path is not None:
        parts.append(tasks.spill_marker(path))
    if task["exception"]:
        parts.append(str(task["exception"]).rstrip())
    meta_dict: dict[str, str] = {
        "kind": "task_done",
        "task_id": task_id,
        "error": "1" if task["exception"] else "0",
    }
    if label:
        meta_dict["label"] = label
    push_channel("\n".join(parts), meta_dict, session=task.get("origin"))


def _finalize_cell(task_id: str, task: dict, t_start: float) -> None:
    elapsed = (time.monotonic() - t_start) * 1000
    events.emit(CellDone(task_id, elapsed, task.get("exception")))
    tasks.finalize(task_id)
    _maybe_push_done(task_id)


@contextlib.asynccontextmanager
async def _task_scope(task_id: str):
    """Set up per-task lifecycle bookkeeping shared by _run_cell/_run_deferred.

    Sets _current_task ContextVar so that asyncio.create_task() calls inside
    user code inherit it via copy_context() — preserving per-task output
    attribution for fire-and-forget background tasks. Finalizes (emits
    CellDone, pushes channel) on the way out regardless of how the body exits.
    """
    tasks.set_current_task(task_id)
    task = tasks.get(task_id)
    assert task is not None, f"task {task_id} missing from registry"
    # Stash the asyncio.Task handle so cancel_task can call .cancel() on it
    # directly (cf.Future.cancel() on a running threadsafe-launched task
    # doesn't propagate reliably).
    task["asyncio_task"] = asyncio.current_task()
    t_start = time.monotonic()
    try:
        yield task
    finally:
        _finalize_cell(task_id, task, t_start)


async def _run_cell(task_id: str, src: str, n: int, *, wait_ready: bool = True) -> None:
    """Coroutine that runs on the bg asyncio loop.

    Waits for the project bootstrap first. The socket binds before
    `repld_init.py` runs — deliberately, since the bridge only allows a spawn
    5s to become connectable and a bootstrap that raises a tunnel takes longer
    — so without this the first `exec` after a lazy spawn races it and sees a
    bare `__main__`. Waiting here rather than delaying the bind keeps lazy
    spawn working and costs nothing once the bootstrap is done: the cell still
    gets its task_id immediately, and a wait past `exec`'s timeout degrades
    into the ordinary `{task_id, done:false}` plus a completion push.

    `wait_ready=False` for the bootstrap itself, which runs through here and
    would otherwise wait on its own completion.
    """
    from . import runtime

    if wait_ready:
        await _init_done.wait()

    async with _task_scope(task_id) as task:
        try:
            compiled = runtime.compile_cell(src, task_id)
        except SyntaxError:
            tb = traceback.format_exc()
            sys.stderr.write(tb)
            task["exception"] = "SyntaxError"
            return

        try:
            await runtime.run_cell(compiled, __main__.__dict__, n)
        except BaseException as exc:
            task["exception"] = type(exc).__name__


async def _run_deferred(task_id: str, coro) -> None:
    """Await a user-supplied coroutine within the task lifecycle.

    Like _run_cell but skips compile/eval — just awaits the coroutine directly.
    """
    async with _task_scope(task_id) as task:
        try:
            await coro
        except asyncio.CancelledError:
            task["exception"] = "CancelledError"
        except BaseException as exc:
            task["exception"] = type(exc).__name__
            sys.stderr.write(traceback.format_exc())


def _make_defer(loop: asyncio.AbstractEventLoop):
    """Return a defer(coro, label=None) function bound to the kernel's loop."""

    def defer(coro, label: str | None = None) -> str:
        """Schedule a coroutine as a tracked task. Returns task_id immediately.

        The task is visible to get_task and cancel. On completion, a task_done
        channel notification is pushed.
        """
        if not inspect.iscoroutine(coro):
            raise TypeError(
                f"defer() expects a coroutine object, got {type(coro).__name__}. "
                "Call it as: defer(my_async_fn())"
            )
        # Inherit the calling cell's originating session so a background task
        # reports back to whoever asked for it. defer() from an @every body or
        # the project bootstrap has no current task, so it stays ambient.
        parent = tasks.get(tasks.current_task_id() or "")
        task_id, task = tasks.new_task(origin=parent.get("origin") if parent else None)
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
# @every decorator
# ---------------------------------------------------------------------------


@dataclass(eq=False)
class EveryHandle:
    label: str
    seconds: float
    _task: "asyncio.Task[None]"

    def cancel(self) -> None:
        self._task.cancel()
        with _every_lock:
            _every_registry.discard(self)

    def __repr__(self) -> str:
        return f"<every {self.seconds}s: {self.label}>"


# Mutated from the asyncio loop thread (_start_ticker) and from sync-cell
# threads (EveryHandle.cancel() via asyncio.to_thread) — needs a lock like
# every other cross-thread registry in this codebase (tasks._tasks_lock,
# gates._gates_lock). The dashboard's HTTP handler reads it via every_snapshot()
# too, but that runs on the same shared loop (dashboard.start_dashboard
# schedules onto `loop`), not a separate thread.
_every_registry: set[EveryHandle] = set()
_every_lock = threading.Lock()


def every_snapshot() -> list[EveryHandle]:
    """Thread-safe copy of the active @every tickers, for cross-thread readers."""
    with _every_lock:
        return list(_every_registry)


async def _start_ticker(fn, seconds: float, label: str, delay: float = 0.0) -> None:
    """Coroutine that runs on the shared asyncio loop.

    Runs the first tick after `delay` seconds (immediately by default), then
    sleeps `seconds` between ticks. Catches exceptions so one bad tick doesn't
    stop the schedule. Sets fn._handle and fn.cancel once the task is live.
    """
    task = asyncio.current_task()
    assert task is not None
    handle = EveryHandle(label, seconds, task)
    with _every_lock:
        _every_registry.add(handle)
    fn._handle = handle
    fn.cancel = handle.cancel

    # One unregister covering every way out, rather than a discard at each
    # `await` that can be cancelled. It was the latter, and the await a ticker
    # spends nearly all its life in — the inter-tick sleep — was the one
    # missing it: `EveryHandle.cancel()` discards first so the user-facing
    # path looked fine, but the watchdog cancels by task (`_pick_victim`
    # treats an unnamed loop task as fair game and names an @every as the
    # typical one), which left `every.list()` and the dashboard reporting a
    # ticker that no longer existed, exactly when someone is debugging a
    # wedged loop. `except Exception` below cannot swallow the cancellation
    # on its way here — `CancelledError` is a `BaseException`.
    try:
        if delay > 0:
            await asyncio.sleep(delay)

        while True:
            try:
                result = fn()
                if inspect.iscoroutine(result):
                    result = await result
            except Exception as exc:
                _push(
                    f"@every {label}: {type(exc).__name__}: {exc}",
                    "every",
                    label=label,
                    error="1",
                )
            else:
                if result is not None:
                    _push(str(result), "every", label=label)
            await asyncio.sleep(seconds)
    finally:
        with _every_lock:
            _every_registry.discard(handle)


def _make_every(loop: asyncio.AbstractEventLoop):
    """Return an every(seconds, *, label=None)(fn) decorator bound to the kernel's loop."""

    def every(seconds: float, *, label: str | None = None, delay: float = 0.0):
        """Schedule fn to run immediately, then every `seconds` on the kernel loop.

        `delay` holds the *first* tick back that many seconds. The default of 0
        (tick now) is right for polling something that already exists, and wrong
        for watching something you just started: a health check registered the
        moment a resource comes up runs against it at its most fragile point,
        and a false negative there can send a re-raise loop after a resource
        that was about to be fine. `every(60, delay=60)` waits one interval.

        Returns fn unchanged so @every is a pure decorator. Attaches
        fn._handle (EveryHandle) and fn.cancel() shortcut after the first
        loop tick completes.
        """

        def decorator(fn):
            name = label or fn.__name__
            asyncio.run_coroutine_threadsafe(
                _start_ticker(fn, seconds, name, delay), loop
            )
            return fn

        return decorator

    def _list() -> list[EveryHandle]:
        return every_snapshot()

    def _cancel_all() -> None:
        for h in every_snapshot():
            h.cancel()

    every.list = _list  # type: ignore[attr-defined]
    every.cancel_all = _cancel_all  # type: ignore[attr-defined]
    return every


# ---------------------------------------------------------------------------
# KernelContext (implements kernel_context.KernelContext)
# ---------------------------------------------------------------------------


class _Context:
    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop

    def start_task(self, src: str, *, origin=None):
        n = next(_exec_count)
        task_id, task = tasks.new_task(origin=origin)
        events.emit(CellStart(task_id, src, time.time()))
        asyncio.run_coroutine_threadsafe(_run_cell(task_id, src, n), self.loop)
        return task_id, task["done_event"]

    def snapshot(self, task_id: str) -> dict | None:
        return tasks.snapshot(task_id)

    def mark_nudged(self, task_id: str) -> bool:
        return tasks.mark_nudged(task_id)

    def cancel_task(self, task_id: str) -> bool:
        """Attempt to cancel a running cell. Returns True if the cancellation
        request was scheduled. Cannot preempt tight sync loops — only
        await-yielding code is cancellable."""
        task = tasks.get(task_id)
        if task is None:
            return False
        asyncio_task = task.get("asyncio_task")
        if asyncio_task is None or asyncio_task.done():
            return False
        self.loop.call_soon_threadsafe(asyncio_task.cancel)
        return True


# ---------------------------------------------------------------------------
# Project bootstrap
# ---------------------------------------------------------------------------

# Auto-executed into `__main__` at boot when it exists in the kernel's cwd.
# A convention rather than a flag because the kernel that matters is usually
# one nobody started by hand — the bridge spawns it lazily, and `repld
# restart` respawns it — and none of those paths could carry an argument.
# The project directory is the source of truth, exactly as it already is for
# `./.env` (`load_dotenv`) and `./gists` (`gists.install`).
INIT_FILENAME = "repld_init.py"


def _run_init_file(path: Path, loop: asyncio.AbstractEventLoop) -> None:
    """Execute the project bootstrap into `__main__`, blocking up to 30s.

    Never raises: a bootstrap that fails leaves a *live* kernel carrying the
    traceback on channel, which is the state you want to fix it from. Killing
    boot instead would take away the thing that can run the fix. The read is
    inside the `try` for that reason too: an unreadable file must not reach
    `_report_boot_failure` and end the process.
    """
    n = next(_exec_count)
    try:
        src = path.read_text()
        # Set __main__.__file__ so the bootstrap's Path(__file__) works the way
        # `python path/to/script.py` would.
        __main__.__file__ = str(path.resolve())
        task_id, _ = tasks.new_task()
        events.emit(CellStart(task_id, src, time.time()))
        future = asyncio.run_coroutine_threadsafe(
            _run_cell(task_id, src, n, wait_ready=False), loop
        )
        # Block until the bootstrap completes (including any run_until_complete
        # semantics — background tasks it spawned stay alive on the loop).
        future.result(timeout=30)
    except Exception:
        tb = traceback.format_exc()
        sys.stderr.write(f"\033[31m[repld] {path.name} raised:\n{tb}\033[0m\n")
        _push(
            f"[repld] {path.name} raised: {tb.rstrip()}",
            "init_error",
            file=str(path),
        )
        return
    # Only ever emitted when a bootstrap actually ran. On a bridge-spawned
    # kernel this is the only sign that `__main__` arrived pre-populated —
    # nobody watched it boot.
    _push(f"[repld] {path.name} loaded", "init_loaded", file=str(path))


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


async def _drain_loop_tasks() -> None:
    """Cancel and await every non-self loop task.

    Lets `try/finally` blocks in @every bodies, defer() coroutines, and
    in-flight exec cells run their cleanup before the loop halts.
    """
    me = asyncio.current_task()
    targets = [t for t in asyncio.all_tasks() if t is not me and not t.done()]
    if not targets:
        return
    for t in targets:
        t.cancel()
    await asyncio.gather(*targets, return_exceptions=True)
    with _every_lock:
        _every_registry.clear()


def _shutdown(loop: asyncio.AbstractEventLoop) -> None:
    if loop.is_running():
        try:
            asyncio.run_coroutine_threadsafe(_drain_loop_tasks(), loop).result(
                timeout=2.0
            )
        except (concurrent.futures.TimeoutError, RuntimeError):
            pass  # loop wedged or already stopping — best effort
    loop.call_soon_threadsafe(loop.stop)
    ipc.stop_server()


def _confirm_browser_restore(ports: list[int], patterns: list[str]) -> bool:
    """Ask on the real terminal (writing to sys.__stdout__ to bypass the
    tee, which by this point in boot has already redirected sys.stdout for
    task-output capture) whether to reconnect Chrome ports / re-watch
    patterns from the previous kernel run.
    """
    parts = []
    if ports:
        parts.append(f"ports {', '.join(str(p) for p in ports)}")
    if patterns:
        parts.append(f"patterns {', '.join(patterns)}")
    prompt = f"repld: restore previous browser session ({'; '.join(parts)})? [Y/n] "
    answer = gates.tty_prompt(prompt, stream=sys.__stdout__)
    return answer in ("", "y", "yes")


def _restore_browser_state(
    hint: dict, loop: asyncio.AbstractEventLoop, *, interactive: bool
) -> None:
    """Recover browser state from the previous kernel's dashboard hint.

    Reconnects saved Chrome ports, re-watches patterns, and restores the
    console-error suppress list. Best-effort — failures are reported to
    stderr but never block boot. Reconnect/re-watch is opt-in: prompted on
    the real terminal when `interactive`, skipped otherwise (headless boot
    or non-tty stdin can't be prompted, so it defaults to not reconnecting).
    """
    browser = getattr(__main__, "browser", None)
    ports = hint.get("chrome_ports", [])
    patterns = hint.get("patterns", [])
    if (
        browser is not None
        and (ports or patterns)
        and interactive
        and _confirm_browser_restore(ports, patterns)
    ):
        for port in ports:
            try:
                asyncio.run_coroutine_threadsafe(browser.connect(port), loop).result(
                    timeout=5
                )
            except Exception as e:
                print(
                    f"repld: failed to reconnect Chrome port {port}: {e}",
                    file=sys.stderr,
                )
        for pattern in patterns:
            try:
                asyncio.run_coroutine_threadsafe(browser.watch(pattern), loop).result(
                    timeout=5
                )
            except Exception as e:
                print(
                    f"repld: failed to re-watch pattern {pattern!r}: {e}",
                    file=sys.stderr,
                )
        from . import dashboard

        dashboard.save_hint()

    suppress_list = hint.get("suppress", [])
    if suppress_list:
        try:
            from .browser.cdp import _suppress_patterns

            _suppress_patterns.update(suppress_list)
        except ImportError:
            pass


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def _start_loop() -> asyncio.AbstractEventLoop:
    """1. Start the asyncio loop on a daemon thread."""
    loop = asyncio.new_event_loop()
    loop.set_exception_handler(_asyncio_exception_handler)
    threading.Thread(target=loop.run_forever, daemon=True, name="repld-asyncio").start()
    return loop


def _boot_runtime(sock_path: Path, display: bool) -> None:
    """2. Event queue, event log, tee, .env, gists — before any user code runs."""
    # The queue is the TUI's, and only the TUI's. Headless, the event log's
    # sink is the sole consumer, so skip the queue rather than paying a put +
    # a cross-thread wakeup per event to move it nowhere.
    if display:
        events.init_event_queue()
    else:
        events.disable_queue()

    # Whether a pane's stdin reader will exist to answer `ask`/`confirm`/
    # `choose`. Headless there is none, so gates say so in their channel push
    # and point at `repld gate` instead of blocking mutely forever.
    gates.set_terminal(display)

    # 2a. Reclaim what dead kernels left in RUNTIME_DIR. Boot is the only
    # sensible moment: nothing runs on the way out of a SIGKILL, and a kernel
    # can't tidy up after files it hasn't written yet. Best-effort — a failed
    # sweep is never a reason to refuse to start.
    try:
        paths.ensure_runtime_dir()
        state.sweep_dead_pid_files(paths.RUNTIME_DIR)
    except Exception as e:
        print(f"repld: could not sweep stale runtime files: {e}", file=sys.stderr)

    # Installed before the tee so a headless kernel's very first output is
    # already on disk for `repld log`.
    eventlog.install(paths.eventlog_for(sock_path))
    atexit.register(eventlog.close)
    install_tee()

    # 2b. Load .env from project root (same dir as socket/lockfile/gists).
    load_dotenv()

    # 2b-ii. Adopt the project venv, if we're not already running under it.
    # The usual path is that `bind.rebind_exec` re-execed us into it before we
    # got here, making this a no-op; it earns its keep when that couldn't
    # happen (no uv on PATH, or a kernel started by hand). A version mismatch
    # returns None and is left alone — splicing a venv built for a different
    # Python onto sys.path half-works, which is worse than not trying.
    from . import bind as _bind

    _project_venv = _bind.project_venv()
    if _project_venv is not None and not _bind.is_bound(_project_venv):
        if _bind.adopt(_project_venv) is None:
            print(
                f"repld: {_bind.describe(_project_venv)} — its packages are "
                "not importable here; `repld restart` under the project's "
                "interpreter to fix",
                file=sys.stderr,
            )

    # 2c. Set up gist directories on sys.path with auto-reload.
    from . import gists as _gists

    _gists.install(
        [
            Path.home() / ".repld" / "gists",
            Path.cwd() / "gists",
        ]
    )

    # 2d. Check gist dependencies before IPC starts. Prompts when this kernel
    # has a terminal; headless it reports what's missing and installs nothing.
    from . import gist_deps as _gist_deps

    missing = _gist_deps.scan_deps()
    if missing:
        _gist_deps.install_deps(missing)


def _inject_builtins(loop: asyncio.AbstractEventLoop) -> None:
    """3. Inject helpers into __main__ + repld module."""
    from . import runtime
    import pydoc
    import repld as _repld_mod

    _every = _make_every(loop)
    _defer = _make_defer(loop)
    _helpers = {
        "notify": _notify,
        "defer": _defer,
        "every": _every,
        "ask": gates.ask,
        "confirm": gates.confirm,
        "choose": gates.choose,
        "no_display": runtime.no_display,
    }
    for _name, _fn in _helpers.items():
        setattr(__main__, _name, _fn)
        setattr(_repld_mod, _name, _fn)
    # Pager-free help — pydoc's default pager forks less(1) on the kernel tty,
    # bypassing _Tee and deadlocking the asyncio loop. Helper(output=...) writes
    # directly through sys.stdout (the _Tee) so output flows to exec clients.
    setattr(__main__, "help", pydoc.Helper(output=sys.stdout))

    # Inject lazy browser builtin (zero import cost until first browser.watch()).
    try:
        from .browser import LazyBrowser

        _lazy_browser = LazyBrowser()
        setattr(__main__, "browser", _lazy_browser)
        setattr(_repld_mod, "browser", _lazy_browser)
        # No atexit disconnect hook, and adding one cannot work: `_shutdown`
        # stops the loop before atexit runs, so a coroutine scheduled there is
        # never driven and the wait just burns its timeout. Tabs clean
        # themselves up anyway — the pill's staleness check removes it, and the
        # beforeunload guard with it, once Python stops heartbeating, which is
        # exactly what a stopped loop looks like from the page.
    except ImportError:
        pass  # repld[browser] not installed — no browser builtin


def _start_services(
    loop: asyncio.AbstractEventLoop, sock_path: Path, display: bool
) -> int | None:
    """4. IPC, dashboard, browser restore, lockfile, session registry.

    Returns the dashboard port (None if the dashboard failed to start).
    """
    global _active_lock_path
    ctx = _Context(loop)
    dispatcher = Dispatcher(ctx)
    ipc.start_server(sock_path, dispatcher.handle)

    # 4b. Dashboard HTTP server — reuse previous state from persistent hint file.
    from . import dashboard

    _kernel_start_time = time.monotonic()
    dash_hint = paths.hint_for(sock_path)
    hint: dict = {}
    try:
        loaded = json.loads(dash_hint.read_text())
        # Narrowing, not migration: `_restore_browser_state` calls .get() on
        # this outside any try, so a hand-mangled hint would take boot down.
        if isinstance(loaded, dict):
            hint = loaded
    except (OSError, json.JSONDecodeError):
        pass

    dashboard_port: int | None = None
    try:
        dashboard_port = dashboard.start_dashboard(
            loop,
            str(sock_path),
            _kernel_start_time,
            preferred_port=hint.get("dashboard_port", 0),
            hint_path=dash_hint,
        )
        atexit.register(dashboard.stop_dashboard)
    except Exception as e:
        print(f"repld: dashboard failed to start: {e}", file=sys.stderr)

    _restore_browser_state(hint, loop, interactive=display and sys.stdin.isatty())

    _write_lockfile(sock_path, dashboard_port=dashboard_port)
    _active_lock_path = lock_for(sock_path)
    atexit.register(_cleanup_lockfile)
    # _shutdown() also stops the server; this covers abnormal exits that
    # never reach it. stop_server is idempotent, so the overlap is safe.
    atexit.register(ipc.stop_server)

    sessions.register(os.getcwd(), str(sock_path), dashboard_port)
    atexit.register(sessions.unregister)
    return dashboard_port


def _start_watchdog(
    loop: asyncio.AbstractEventLoop, sock_path: Path, dashboard_port: int | None
) -> threading.Event:
    """5+6. Loop watchdog + banner. Returns the kernel's stop event."""
    # 5. Loop watchdog — channel-push if the bg loop wedges (typically a
    #    cell doing sync I/O while uvicorn or similar lives on the loop).
    #    Tunable via REPLD_LOOP_BLOCK_THRESHOLD (seconds, default 5).
    #    Kill threshold: cancel longest-running task after REPLD_LOOP_KILL_THRESHOLD (default 30s).
    stop = threading.Event()
    threshold = float(os.environ.get("REPLD_LOOP_BLOCK_THRESHOLD", "5.0"))
    kill_threshold = float(os.environ.get("REPLD_LOOP_KILL_THRESHOLD", "30.0"))

    # 6. Print banner (goes to sys.__stderr__ directly so it's visible even
    #    in --no-display mode before the tee is fully wired). Includes the
    #    active watchdog threshold so users know what to expect.
    stderr = sys.__stderr__
    if stderr is not None:
        stderr.write(
            _banner(sock_path, threshold, kill_threshold, dashboard_port) + "\n"
        )
        stderr.flush()
    threading.Thread(
        target=_loop_watchdog,
        args=(loop, stop, threshold, kill_threshold, 1.0),
        daemon=True,
        name="repld-watchdog",
    ).start()
    return stop


def _report_boot_failure() -> None:
    """Put the in-flight exception on the *real* stderr, bypassing the tee.

    `install_tee()` runs partway through `_boot_runtime`, and `_Tee.write`
    deliberately never touches `self.real` — the display thread owns the
    terminal. Anything that dies after that point therefore reaches only the
    event log, and there is no display thread yet to render it, so a kernel
    that fails to boot exits non-zero having printed nothing at all. That is
    how a too-long `--socket` path or an already-bound port presents: a silent
    exit 1.

    The exception is re-raised by the caller, so the event log still gets its
    copy through the normal unhandled-exception path — this only adds the
    destination a human is actually looking at.
    """
    import traceback

    real = sys.__stderr__
    if real is None:  # stderr closed (detached spawn) — the event log has it
        return
    try:
        real.write("\nrepld: kernel failed to start\n")
        traceback.print_exc(file=real)
        real.flush()
    except (OSError, ValueError):
        pass


def run_kernel(
    socket_path: str | None = None,
    *,
    display: bool = True,
) -> int:
    global _project_lock_fd
    sock_path = Path(socket_path) if socket_path else default_socket_path()
    _project_lock_fd = _claim_project(sock_path)

    loop = _start_loop()
    try:
        _boot_runtime(sock_path, display)
        _inject_builtins(loop)
        dashboard_port = _start_services(loop, sock_path, display)
        _write_cache(sock_path)
        stop = _start_watchdog(loop, sock_path, dashboard_port)

        # 7. Project bootstrap, if this project has one. Deliberately *after*
        #    `_start_services` bound the socket: the bridge gives a spawn 5s to
        #    become connectable, and a bootstrap that raises a tunnel or waits
        #    on a server takes longer than that. Booting it first would make
        #    lazy spawn time out on exactly the projects that have one.
        init_path = Path.cwd() / INIT_FILENAME
        if init_path.is_file():
            _run_init_file(init_path, loop)
        # Release the cells that queued behind the bootstrap. Unconditional and
        # in a `finally`-shaped position: `_run_init_file` never raises, but a
        # bootstrap that fails, times out, or doesn't exist must still let exec
        # through — a kernel nobody can run code in cannot be repaired.
        loop.call_soon_threadsafe(_init_done.set)
    except Exception:
        # Not BaseException: a Ctrl-C during boot needs no banner, and the
        # operator already knows why it stopped.
        _report_boot_failure()
        raise

    # 8. Main thread: display or headless.
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    if display:
        from .display import run_display

        run_display(stop)
    else:
        # Nothing to consume — _boot_runtime disabled the queue. Just park.
        stop.wait()

    _shutdown(loop)
    return 0
