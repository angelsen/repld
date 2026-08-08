"""Phase 7: defer() — fire-and-forget with channel push on completion."""

from harness import Bridge, Kernel, assert_eq, assert_true


def phase_7_defer(kernel: Kernel) -> None:
    """defer() from exec → task_id returned, channel push on completion."""
    b = Bridge(kernel.cwd)
    try:
        b.call("initialize", {"protocolVersion": "2024-11-05"})
        b.send("notifications/initialized", {}, notif=True)

        # defer a coroutine — should return task_id inline, then push channel
        resp = b.call(
            "tools/call",
            {
                "name": "exec",
                "arguments": {
                    "code": (
                        "import asyncio\n"
                        "async def _slow():\n"
                        "    await asyncio.sleep(0.3)\n"
                        "    print('deferred done')\n"
                        "tid = defer(_slow(), label='test-defer')\n"
                        "print(f'task_id={tid}')"
                    ),
                },
            },
            timeout=3.0,
        )
        content = resp["result"]["content"][0]["text"]
        assert_true("task_id=" in content, f"defer returned task_id (got {content!r})")
        print("  ✓ defer: returned task_id inline")

        # Wait for channel notification
        notif = b.wait_notification(
            "notifications/claude/channel", kind="task_done", timeout=5.0
        )
        params = notif["params"]
        assert_eq(params["meta"]["kind"], "task_done", "defer channel kind")
        assert_true(
            "deferred done" in params["content"],
            f"defer output in channel (got {params['content']!r})",
        )
        assert_true(
            "test-defer" in params["content"],
            f"label in channel content (got {params['content']!r})",
        )
        assert_true(
            params["meta"].get("label") == "test-defer",
            f"label in channel meta (got {params['meta']!r})",
        )
        assert_eq(params["meta"]["error"], "0", "defer success error=0")
        print("  ✓ defer: channel push with label + output")

        # Error case
        resp = b.call(
            "tools/call",
            {
                "name": "exec",
                "arguments": {
                    "code": (
                        "async def _boom():\n"
                        "    raise ValueError('kaboom')\n"
                        "defer(_boom(), label='error-test')"
                    ),
                },
            },
            timeout=3.0,
        )
        notif = b.wait_notification(
            "notifications/claude/channel", kind="task_done", timeout=5.0
        )
        assert_eq(notif["params"]["meta"]["error"], "1", "defer error case")
        assert_eq(
            notif["params"]["meta"].get("label"),
            "error-test",
            "error case label in meta",
        )
        print("  ✓ defer error case: error=1, label preserved")

        # TypeError on non-coroutine
        resp = b.call(
            "tools/call",
            {
                "name": "exec",
                "arguments": {
                    "code": "defer(42)",
                },
            },
            timeout=3.0,
        )
        is_error = resp["result"].get("isError", False)
        content = resp["result"]["content"][0]["text"]
        assert_true(is_error, "defer(42) is an error")
        assert_true(
            "TypeError" in content, f"defer(42) raises TypeError (got {content!r})"
        )
        print("  ✓ defer(non-coroutine): TypeError")

        # Regression: asyncio.gather() built eagerly as a defer() argument,
        # in a cell with no top-level await. The cell runs in a worker
        # thread (asyncio.to_thread) to keep the kernel's loop responsive,
        # so gather() has no loop to attach to *before* defer() is ever
        # entered — the RuntimeError hint used to say "use defer()" for a
        # cell that already was. It should now point at the actual fix.
        resp = b.call(
            "tools/call",
            {
                "name": "exec",
                "arguments": {
                    "code": (
                        "import asyncio\n"
                        "async def _one(k):\n"
                        "    return k\n"
                        "defer(asyncio.gather(_one('a'), _one('b')))"
                    ),
                },
            },
            timeout=3.0,
        )
        is_error = resp["result"].get("isError", False)
        content = resp["result"]["content"][0]["text"]
        assert_true(is_error, "eager gather() in a sync cell is an error")
        assert_true(
            "defer(lambda:" in content,
            f"hint points at the lambda-factory fix (got {content!r})",
        )
        print("  ✓ defer: misleading 'use defer()' hint replaced")

        # Fix: defer() accepts a zero-arg callable and constructs it inside
        # the deferred task, i.e. on the kernel's loop — the report's own
        # repro (two coroutines fanned out via gather in one deferred task).
        resp = b.call(
            "tools/call",
            {
                "name": "exec",
                "arguments": {
                    "code": (
                        "import asyncio\n"
                        "async def _one(k):\n"
                        "    await asyncio.sleep(0.05)\n"
                        "    return k\n"
                        "tid = defer(lambda: asyncio.gather("
                        "_one('a'), _one('b'), return_exceptions=True), "
                        "label='gather-factory')\n"
                        "print(f'task_id={tid}')"
                    ),
                },
            },
            timeout=3.0,
        )
        content = resp["result"]["content"][0]["text"]
        assert_true(
            "task_id=" in content,
            f"defer(lambda: gather(...)) returned task_id (got {content!r})",
        )
        notif = b.wait_notification(
            "notifications/claude/channel", kind="task_done", timeout=5.0
        )
        assert_eq(
            notif["params"]["meta"]["error"], "0", "defer(lambda: gather(...)) success"
        )
        assert_eq(
            notif["params"]["meta"].get("label"),
            "gather-factory",
            "gather-factory label preserved",
        )
        print("  ✓ defer(lambda: asyncio.gather(...)): builds on the loop, completes")

        # Forgetting the parens on an async function must still be the
        # friendly, synchronous TypeError — the callable-factory path can't
        # be allowed to silently auto-call it with no arguments instead.
        resp = b.call(
            "tools/call",
            {
                "name": "exec",
                "arguments": {
                    "code": "async def _slow_fn():\n    pass\ndefer(_slow_fn)",
                },
            },
            timeout=3.0,
        )
        is_error = resp["result"].get("isError", False)
        content = resp["result"]["content"][0]["text"]
        assert_true(is_error, "defer(async_fn) without calling it is an error")
        assert_true(
            "_slow_fn" in content and "TypeError" in content,
            f"defer(fn) names the function in the error (got {content!r})",
        )
        print("  ✓ defer(async_fn without parens): friendly TypeError, not auto-called")

        # A factory that doesn't return an awaitable fails the deferred
        # task, not the kernel or the defer() call itself.
        resp = b.call(
            "tools/call",
            {
                "name": "exec",
                "arguments": {
                    "code": "defer(lambda: 42, label='bad-factory')",
                },
            },
            timeout=3.0,
        )
        notif = b.wait_notification(
            "notifications/claude/channel", kind="task_done", timeout=5.0
        )
        assert_eq(
            notif["params"]["meta"]["error"],
            "1",
            "non-awaitable factory result surfaces as a task error",
        )
        assert_eq(
            notif["params"]["meta"].get("label"),
            "bad-factory",
            "bad-factory label preserved",
        )
        print("  ✓ defer(lambda: non-awaitable): fails the task, not the kernel")
    finally:
        b.close()
