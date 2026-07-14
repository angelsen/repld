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

from . import events, ipc, sessions, tasks
from .events import CellDone, CellStart, ChannelPush
from .ipc import atomic_write_json, default_socket_path, lock_for, read_lock
from .protocol import Dispatcher
from .tasks import install_tee

# ---------------------------------------------------------------------------
# Lock file
# ---------------------------------------------------------------------------


def _check_existing_kernel(socket_path: Path) -> None:
    """Refuse to start if another repld kernel owns this socket."""
    lock_path = lock_for(socket_path)
    lock = read_lock(lock_path)
    if isinstance(lock, dict):
        raise SystemExit(
            f"\033[31m[repld] another kernel (pid={lock.get('pid')}) is running "
            f"({lock_path}). Stop it or remove the lock file if stale.\033[0m"
        )


def _write_lockfile(socket_path: Path, dashboard_port: int | None = None) -> None:
    info: dict[str, object] = {
        "pid": os.getpid(),
        "socket_path": str(socket_path),
        "cwd": os.getcwd(),
        "started_at": time.time(),
    }
    if dashboard_port is not None:
        info["dashboard_port"] = dashboard_port
    atomic_write_json(lock_for(socket_path), info)


_active_lock_path: Path | None = None


# ---------------------------------------------------------------------------
# .env loading
# ---------------------------------------------------------------------------


def _load_dotenv() -> None:
    """Load KEY=VALUE pairs from ./.env into os.environ (stdlib only).

    Skips comments, blank lines, and export prefixes. Strips surrounding
    quotes. Does NOT override existing env vars.
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
        f"\033[90m[repld] pid={os.getpid()}  socket={socket_path}  (lock: {lock_for(socket_path).name})",
        f"  watchdog:  loop_blocked channel push if cell holds the loop > {watchdog_threshold}s "
        f"(REPLD_LOOP_BLOCK_THRESHOLD)",
        f"  kill:      longest-running task cancelled if loop blocked > {kill_threshold}s "
        f"(REPLD_LOOP_KILL_THRESHOLD)",
    ]
    if dashboard_port is not None:
        lines.append(
            f"  dashboard: \033[0m\033[4mhttp://localhost:{dashboard_port}\033[0m\033[90m"
        )
    lines += [
        "  register:  claude mcp add -s project repld -- repld bridge",
        "  launch:    claude --dangerously-load-development-channels server:repld",
        "  human:     repld exec   # interactive REPL (state shared with agent)\033[0m",
    ]
    return "\n".join(lines)


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
    events.emit(ChannelPush(content, {k: str(v) for k, v in meta.items()}))


def _notify(content, **meta) -> None:
    """Push a channel notification to all connected MCP sessions. meta keys become XML attributes."""
    push_channel(str(content), meta)


def _push(content: str, kind: str, **meta: str) -> None:
    """push_channel with the ubiquitous {"kind": ...} meta shape spelled out once."""
    push_channel(content, {"kind": kind, **meta})


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
        # Probe first so `threshold` is the actual hang-detection time
        # (not threshold + interval).
        future = asyncio.run_coroutine_threadsafe(asyncio.sleep(0), loop)
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
                    _push(
                        f"[repld] killed blocked task: {victim_name}",
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
    if task is None or not task.get("nudged"):
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
    push_channel("\n".join(parts), meta_dict)


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


async def _run_cell(task_id: str, src: str, n: int) -> None:
    """Coroutine that runs on the bg asyncio loop."""
    from . import runtime

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


async def _start_ticker(fn, seconds: float, label: str) -> None:
    """Coroutine that runs on the shared asyncio loop.

    Runs the first tick immediately, then sleeps `seconds` between ticks.
    Catches exceptions so one bad tick doesn't stop the schedule.
    Sets fn._handle and fn.cancel once the task is live.
    """
    task = asyncio.current_task()
    assert task is not None
    handle = EveryHandle(label, seconds, task)
    with _every_lock:
        _every_registry.add(handle)
    fn._handle = handle
    fn.cancel = handle.cancel

    while True:
        try:
            result = fn()
            if inspect.iscoroutine(result):
                result = await result
        except asyncio.CancelledError:
            with _every_lock:
                _every_registry.discard(handle)
            raise
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


def _make_every(loop: asyncio.AbstractEventLoop):
    """Return an every(seconds, *, label=None)(fn) decorator bound to the kernel's loop."""

    def every(seconds: float, *, label: str | None = None):
        """Schedule fn to run immediately, then every `seconds` on the kernel loop.

        Returns fn unchanged so @every is a pure decorator. Attaches
        fn._handle (EveryHandle) and fn.cancel() shortcut after the first
        loop tick completes.
        """

        def decorator(fn):
            name = label or fn.__name__
            asyncio.run_coroutine_threadsafe(_start_ticker(fn, seconds, name), loop)
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

    def start_task(self, src: str):
        n = next(_exec_count)
        task_id, task = tasks.new_task()
        events.emit(CellStart(task_id, src, time.time()))
        asyncio.run_coroutine_threadsafe(_run_cell(task_id, src, n), self.loop)
        return task_id, task["done_event"]

    def snapshot(self, task_id: str) -> dict | None:
        return tasks.snapshot(task_id)

    def mark_nudged(self, task_id: str) -> None:
        tasks.mark_nudged(task_id)

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
# Init file
# ---------------------------------------------------------------------------


def _run_init_file(path: Path, loop: asyncio.AbstractEventLoop) -> None:
    if not path.exists():
        sys.stderr.write(f"\033[31m[repld] --init file not found: {path}\033[0m\n")
        _push(
            f"[repld] --init file not found: {path}",
            "init_error",
            file=str(path),
            reason="not_found",
        )
        return
    n = next(_exec_count)
    src = path.read_text()
    # Set __main__.__file__ so the init file's Path(__file__) works the way
    # `python path/to/script.py` would.
    __main__.__file__ = str(path.resolve())
    task_id, _ = tasks.new_task()
    events.emit(CellStart(task_id, src, time.time()))
    future = asyncio.run_coroutine_threadsafe(_run_cell(task_id, src, n), loop)
    # Block until the init file completes (including any run_until_complete
    # semantics — background tasks it spawned stay alive on the loop).
    try:
        future.result(timeout=30)
    except Exception:
        tb = traceback.format_exc()
        sys.stderr.write(f"\033[31m[repld] --init {path.name} raised:\n{tb}\033[0m\n")
        _push(
            f"[repld] --init {path.name} raised: {tb.rstrip()}",
            "init_error",
            file=str(path),
        )


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
    answer = ipc.tty_prompt(prompt, stream=sys.__stdout__)
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


def _boot_runtime() -> None:
    """2. Event queue, tee, .env, gists — before any user code runs."""
    events.init_event_queue()
    install_tee()

    # 2b. Load .env from project root (same dir as socket/lockfile/gists).
    _load_dotenv()

    # 2c. Set up gist directories on sys.path with auto-reload.
    from . import gists as _gists

    _gists.install(
        [
            Path.home() / ".repld" / "gists",
            Path.cwd() / "gists",
        ]
    )

    # 2d. Check gist dependencies before IPC starts (interactive prompt).
    from . import gist_deps as _gist_deps

    missing = _gist_deps.scan_deps()
    if missing:
        _gist_deps.install_deps(missing)


def _inject_builtins(loop: asyncio.AbstractEventLoop) -> None:
    """3. Inject helpers into __main__ + repld module."""
    from . import gates, runtime
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

        def _browser_cleanup() -> None:
            b = getattr(__main__, "browser", None)
            real = getattr(b, "_real", b)
            if real is not None and hasattr(real, "disconnect"):
                try:
                    fut = asyncio.run_coroutine_threadsafe(real.disconnect(), loop)
                    fut.result(timeout=5)
                except Exception:
                    pass

        atexit.register(_browser_cleanup)
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
    dash_hint = sock_path.with_suffix(".dashboard")
    hint: dict = {}
    try:
        loaded = json.loads(dash_hint.read_text())
        # Older kernels wrote a bare port int here; current code expects an object.
        # Ignore anything that isn't a dict so a stale hint can't crash boot.
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


def run_kernel(
    socket_path: str | None = None,
    *,
    display: bool = True,
    init_file: str | None = None,
) -> int:
    sock_path = Path(socket_path) if socket_path else default_socket_path()
    _check_existing_kernel(sock_path)

    loop = _start_loop()
    _boot_runtime()
    _inject_builtins(loop)
    dashboard_port = _start_services(loop, sock_path, display)
    stop = _start_watchdog(loop, sock_path, dashboard_port)

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
