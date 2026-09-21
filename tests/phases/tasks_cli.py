"""Phase 18: `repld tasks wait` / `repld tasks cancel` on a headless kernel.

Both talk to the kernel over the same IPC socket `repld gate` uses (bare
JSON-RPC, not MCP tools) rather than the dashboard HTTP path `repld tasks`
(the listing form) uses — see `tasks_cmd.py`'s module docstring.
"""

import json
import subprocess
import time

from harness import REPO, Bridge, Kernel, assert_eq, assert_true, content_text


def _tasks_cli(kernel: Kernel, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["uv", "run", "--project", str(REPO), "repld", "tasks", *args],
        cwd=str(kernel.cwd),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _defer_expr(b: Bridge, coro_expr: str) -> str:
    """Run a cell that defers the coroutine expression; return its task_id."""
    text = content_text(
        b.exec(f"import asyncio\ntid = defer({coro_expr})\nprint(f'task_id={{tid}}')")
    )
    assert_true("task_id=" in text, f"defer returned task_id (got {text!r})")
    return text.split("task_id=")[1].strip()


def _defer_code(b: Bridge, code: str) -> str:
    """Run a multi-line cell that defines + defers a coroutine; return its
    task_id. *code* must bind `tid` and print `task_id={tid}` itself."""
    text = content_text(b.exec(f"import asyncio\n{code}"))
    assert_true("task_id=" in text, f"defer returned task_id (got {text!r})")
    return text.split("task_id=")[1].strip()


def _poll_done(b: Bridge, task_id: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = b.call(
            "tools/call", {"name": "get_task", "arguments": {"task_id": task_id}}
        )
        if resp["result"]["_meta"]["done"]:
            return
        time.sleep(0.1)
    raise AssertionError(f"task {task_id} never completed")


def phase_18_tasks_cli(kernel: Kernel) -> None:
    b = Bridge(kernel.cwd)
    try:
        b.handshake()
        _wait_already_done(b, kernel)
        _wait_blocks_until_done(b, kernel)
        _wait_exception(b, kernel)
        _wait_unknown_id(kernel)
        _cancel_running(b, kernel)
        _cancel_already_done(b, kernel)
        _cancel_unknown_id(kernel)
        _list_still_works(kernel)
    finally:
        b.close()


def _wait_already_done(b: Bridge, kernel: Kernel) -> None:
    task_id = _defer_expr(b, "asyncio.sleep(0)")
    _poll_done(b, task_id)

    start = time.monotonic()
    proc = _tasks_cli(kernel, "wait", task_id)
    elapsed = time.monotonic() - start
    assert_eq(
        proc.returncode, 0, f"wait on a done task exits 0 (stderr: {proc.stderr})"
    )
    assert_true(
        elapsed < 3.0, f"an already-done wait returns immediately (took {elapsed}s)"
    )
    assert_true(
        task_id[:8] in proc.stdout, f"wait output names the task (got {proc.stdout!r})"
    )
    print("  ✓ tasks wait: already-done task returns immediately")


def _wait_blocks_until_done(b: Bridge, kernel: Kernel) -> None:
    task_id = _defer_code(
        b,
        "async def _slow():\n"
        "    await asyncio.sleep(1.5)\n"
        "    return 'slow-done'\n"
        "tid = defer(_slow())\n"
        "print(f'task_id={tid}')",
    )

    start = time.monotonic()
    proc = _tasks_cli(kernel, "wait", task_id)
    elapsed = time.monotonic() - start
    assert_eq(
        proc.returncode, 0, f"wait on a slow task exits 0 (stderr: {proc.stderr})"
    )
    assert_true(elapsed >= 1.3, f"wait actually blocked (took {elapsed}s)")
    assert_true(
        "slow-done" in proc.stdout,
        f"wait output carries the result (got {proc.stdout!r})",
    )
    print("  ✓ tasks wait: blocks until a running task finishes")


def _wait_exception(b: Bridge, kernel: Kernel) -> None:
    task_id = _defer_code(
        b,
        "async def _boom():\n"
        "    raise ValueError('kaboom')\n"
        "tid = defer(_boom())\n"
        "print(f'task_id={tid}')",
    )
    _poll_done(b, task_id)

    proc = _tasks_cli(kernel, "wait", task_id)
    assert_eq(
        proc.returncode, 1, f"wait on a failed task exits 1 (stderr: {proc.stderr})"
    )
    assert_true(
        "kaboom" in (proc.stdout + proc.stderr),
        f"wait output names the exception (got stdout={proc.stdout!r} stderr={proc.stderr!r})",
    )
    print("  ✓ tasks wait: exit 1 + exception text on a failed task")


def _wait_unknown_id(kernel: Kernel) -> None:
    proc = _tasks_cli(kernel, "wait", "deadbeef0000")
    assert_true(proc.returncode != 0, "waiting on an unknown task_id fails")
    assert_true(
        "deadbeef0000" in (proc.stderr + proc.stdout),
        "the unknown-task error names the id",
    )
    print("  ✓ tasks wait: unknown task_id reported, not silently swallowed")


def _cancel_running(b: Bridge, kernel: Kernel) -> None:
    task_id = _defer_code(
        b,
        "async def _long():\n"
        "    await asyncio.sleep(10)\n"
        "tid = defer(_long())\n"
        "print(f'task_id={tid}')",
    )

    proc = _tasks_cli(kernel, "cancel", task_id)
    assert_eq(
        proc.returncode, 0, f"cancelling a running task exits 0 (stderr: {proc.stderr})"
    )
    assert_true(
        "accepted" in proc.stdout, f"cancel reports accepted (got {proc.stdout!r})"
    )

    # Cancellation lands async — `wait` for it, well under the 10s sleep, to
    # confirm the task actually ended cancelled rather than slept out.
    waited = _tasks_cli(kernel, "wait", task_id)
    assert_eq(waited.returncode, 1, "the cancelled task's own wait reports failure")
    assert_true(
        "CancelledError" in (waited.stdout + waited.stderr),
        f"cancellation reached the task (got stdout={waited.stdout!r} stderr={waited.stderr!r})",
    )
    print("  ✓ tasks cancel: accepted, and the task actually stops")


def _cancel_already_done(b: Bridge, kernel: Kernel) -> None:
    task_id = _defer_expr(b, "asyncio.sleep(0)")
    _poll_done(b, task_id)

    proc = _tasks_cli(kernel, "cancel", task_id)
    assert_eq(proc.returncode, 1, "cancelling a done task exits 1")
    assert_true("no-op" in proc.stdout, f"cancel reports no-op (got {proc.stdout!r})")
    print("  ✓ tasks cancel: no-op on an already-finished task")


def _cancel_unknown_id(kernel: Kernel) -> None:
    proc = _tasks_cli(kernel, "cancel", "deadbeef0000")
    assert_eq(proc.returncode, 1, "cancelling an unknown task_id exits 1")
    print("  ✓ tasks cancel: no-op on an unknown task_id")


def _list_still_works(kernel: Kernel) -> None:
    proc = _tasks_cli(kernel, "--json")
    assert_eq(proc.returncode, 0, f"tasks --json still exits 0 (stderr: {proc.stderr})")
    data = json.loads(proc.stdout)
    assert_true("tasks" in data and "tickers" in data, "listing shape is unchanged")
    print("  ✓ tasks: plain listing unaffected by the new verbs")
