"""Phase 7: defer() — fire-and-forget with channel push on completion."""

from harness import Bridge, Kernel, assert_eq, assert_true, content_text


def phase_7_defer(kernel: Kernel) -> None:
    """defer() from exec → task_id returned, channel push on completion."""
    b = Bridge(kernel.cwd)
    try:
        b.handshake()
        _completion_push(b)
        _rejects_non_coroutines(b)
        _callable_factory(b)
        _return_value(b)
    finally:
        b.close()


def _done(b: Bridge, label: str) -> dict:
    """The task_done push for the deferred task labelled *label*."""
    return b.wait_notification(
        "notifications/claude/channel",
        kind="task_done",
        where=lambda m: m["params"]["meta"].get("label") == label,
        timeout=5.0,
    )["params"]


def _defer(b: Bridge, code: str, label: str) -> str:
    """Run a cell that defers with `label` and prints `task_id=`; return the id."""
    text = content_text(b.exec(code))
    assert_true("task_id=" in text, f"defer {label!r} returned task_id (got {text!r})")
    return text.split("task_id=")[1].strip()


def _completion_push(b: Bridge) -> None:
    _defer(
        b,
        "import asyncio\n"
        "async def _slow():\n"
        "    await asyncio.sleep(0.3)\n"
        "    print('deferred done')\n"
        "tid = defer(_slow(), label='test-defer')\n"
        "print(f'task_id={tid}')",
        "test-defer",
    )
    print("  ✓ defer: returned task_id inline")

    params = _done(b, "test-defer")
    assert_true(
        "deferred done" in params["content"],
        f"defer output in channel (got {params['content']!r})",
    )
    assert_true(
        "test-defer" in params["content"],
        f"label in channel content (got {params['content']!r})",
    )
    assert_eq(params["meta"]["error"], "0", "defer success error=0")
    print("  ✓ defer: channel push with label + output")

    b.exec(
        "async def _boom():\n    raise ValueError('kaboom')\ndefer(_boom(), label='error-test')"
    )
    params = _done(b, "error-test")
    assert_eq(params["meta"]["error"], "1", "defer error case")
    print("  ✓ defer error case: error=1, label preserved")


def _rejects_non_coroutines(b: Bridge) -> None:
    resp = b.exec("defer(42)")
    text = content_text(resp)
    assert_true(resp["result"].get("isError", False), "defer(42) is an error")
    assert_true("TypeError" in text, f"defer(42) raises TypeError (got {text!r})")
    print("  ✓ defer(non-coroutine): TypeError")

    # Forgetting the parens on an async function must still be the friendly,
    # synchronous TypeError — the callable-factory path can't be allowed to
    # silently auto-call it with no arguments instead.
    resp = b.exec("async def _slow_fn():\n    pass\ndefer(_slow_fn)")
    text = content_text(resp)
    assert_true(
        resp["result"].get("isError", False),
        "defer(async_fn) without calling it is an error",
    )
    assert_true(
        "_slow_fn" in text and "TypeError" in text,
        f"defer(fn) names the function in the error (got {text!r})",
    )
    print("  ✓ defer(async_fn without parens): friendly TypeError, not auto-called")


def _callable_factory(b: Bridge) -> None:
    """defer(lambda: ...) builds the awaitable on the kernel's loop.

    Regression: asyncio.gather() built eagerly as a defer() argument, in a
    cell with no top-level await. The cell runs in a worker thread
    (asyncio.to_thread) to keep the kernel's loop responsive, so gather() has
    no loop to attach to *before* defer() is ever entered — the RuntimeError
    hint used to say "use defer()" for a cell that already was.
    """
    resp = b.exec(
        "import asyncio\n"
        "async def _one(k):\n"
        "    return k\n"
        "defer(asyncio.gather(_one('a'), _one('b')))"
    )
    text = content_text(resp)
    assert_true(
        resp["result"].get("isError", False),
        "eager gather() in a sync cell is an error",
    )
    assert_true(
        "defer(lambda:" in text, f"hint points at the lambda-factory fix (got {text!r})"
    )
    print("  ✓ defer: misleading 'use defer()' hint replaced")

    # The report's own repro: two coroutines fanned out via gather in one
    # deferred task.
    _defer(
        b,
        "import asyncio\n"
        "async def _one(k):\n"
        "    await asyncio.sleep(0.05)\n"
        "    return k\n"
        "tid = defer(lambda: asyncio.gather("
        "_one('a'), _one('b'), return_exceptions=True), "
        "label='gather-factory')\n"
        "print(f'task_id={tid}')",
        "gather-factory",
    )
    params = _done(b, "gather-factory")
    assert_eq(params["meta"]["error"], "0", "defer(lambda: gather(...)) success")
    print("  ✓ defer(lambda: asyncio.gather(...)): builds on the loop, completes")

    # A factory that doesn't return an awaitable fails the deferred task, not
    # the kernel or the defer() call itself.
    b.exec("defer(lambda: 42, label='bad-factory')")
    params = _done(b, "bad-factory")
    assert_eq(
        params["meta"]["error"],
        "1",
        "non-awaitable factory result surfaces as a task error",
    )
    print("  ✓ defer(lambda: non-awaitable): fails the task, not the kernel")


def _return_value(b: Bridge) -> None:
    """The awaited value surfaces both ways an exec cell's would.

    Regression: it used to be thrown away outright — no task["result"] field
    existed, and only the exception path recorded anything. A coroutine that
    *returns* something must be recoverable from get_task after the fact, the
    same way it'd land in `_`/`_N` for an ordinary exec cell — and it should
    also print, into the spill and so into the task_done push, since defer
    has no cell number to bind instead.
    """
    task_id = _defer(
        b,
        "async def _computes():\n"
        "    return {'answer': 42}\n"
        "tid = defer(_computes(), label='result-test')\n"
        "print(f'task_id={tid}')",
        "result-test",
    )
    params = _done(b, "result-test")
    assert_eq(params["meta"]["error"], "0", "defer(returns value) success")
    assert_true(
        "{'answer': 42}" in params["content"],
        "deferred return value printed into the channel push "
        f"(got {params['content']!r})",
    )
    snap = _snapshot(b, task_id)
    assert_true(
        bool(snap.get("result")) and "42" in snap["result"],
        f"get_task surfaces the deferred return value (got {snap.get('result')!r})",
    )
    print("  ✓ defer: return value surfaced via get_task and the channel push")

    # no_display() is respected the same way it is for an exec cell's trailing
    # expression: still recoverable from get_task, suppressed from the
    # printed/pushed side.
    task_id = _defer(
        b,
        "async def _quiet():\n"
        "    return no_display('shh')\n"
        "tid = defer(_quiet(), label='quiet-test')\n"
        "print(f'task_id={tid}')",
        "quiet-test",
    )
    params = _done(b, "quiet-test")
    assert_true(
        "shh" not in params["content"],
        f"no_display() suppresses the printed repr (got {params['content']!r})",
    )
    assert_eq(
        _snapshot(b, task_id).get("result"),
        "'shh'",
        "get_task still recovers the no_display()-wrapped value",
    )
    print(
        "  ✓ defer: no_display() suppresses the print, "
        "get_task still recovers the value"
    )


def _snapshot(b: Bridge, task_id: str) -> dict:
    resp = b.call("tools/call", {"name": "get_task", "arguments": {"task_id": task_id}})
    return resp["result"]["_meta"]
