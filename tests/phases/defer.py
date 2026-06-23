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
    finally:
        b.close()
