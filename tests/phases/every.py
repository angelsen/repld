"""Phase 10: @every decorator — periodic ticker via the kernel's shared loop."""

from harness import Bridge, Kernel, assert_eq, assert_true


def phase_10_every(kernel: Kernel) -> None:
    """@every fires immediately, pushes channel, cancel stops it, errors survive."""
    b = Bridge(kernel.cwd)
    try:
        b.call("initialize", {"protocolVersion": "2024-11-05"})
        b.send("notifications/initialized", {}, notif=True)

        # --- 1. Immediate first tick + channel push ---
        resp = b.call(
            "tools/call",
            {
                "name": "exec",
                "arguments": {
                    "code": (
                        "import asyncio\n"
                        "@every(0.2)\n"
                        "def _ticker():\n"
                        "    return 'tick'\n"
                    ),
                },
            },
            timeout=5.0,
        )
        # Exec should succeed (returns the decorated function)
        is_error = resp["result"].get("isError", False)
        assert_true(
            not is_error,
            f"@every decoration raised: {resp['result']['content'][0]['text']!r}",
        )
        print("  ✓ every: decorated without error")

        # First tick fires immediately → channel push
        notif = b.wait_notification(
            "notifications/claude/channel", kind="every", timeout=5.0
        )
        params = notif["params"]
        assert_eq(params["meta"]["kind"], "every", "first tick kind=every")
        assert_eq(params["meta"]["label"], "_ticker", "first tick label=_ticker")
        assert_eq(params["content"], "tick", "first tick content")
        print("  ✓ every: immediate first tick pushed to channel")

        # --- 2. every.list() shows the handle ---
        resp2 = b.call(
            "tools/call",
            {
                "name": "exec",
                "arguments": {"code": "[(h.label, h.seconds) for h in every.list()]"},
            },
            timeout=3.0,
        )
        content2 = resp2["result"]["content"][0]["text"]
        assert_true(
            "_ticker" in content2, f"every.list() shows handle (got {content2!r})"
        )
        print("  ✓ every: every.list() shows active handle")

        # --- 3. cancel() stops the ticker ---
        resp3 = b.call(
            "tools/call",
            {
                "name": "exec",
                "arguments": {
                    "code": (
                        "_ticker.cancel()\n"
                        "import asyncio\n"
                        "await asyncio.sleep(0.05)\n"
                        "len(every.list())"
                    ),
                },
            },
            timeout=3.0,
        )
        content3 = resp3["result"]["content"][0]["text"]
        assert_true(
            content3.strip() == "0",
            f"every.list() empty after cancel (got {content3!r})",
        )
        print("  ✓ every: cancel() removes handle, registry empty")

        # --- 4. Error in tick doesn't kill the loop; pushes error=1 ---
        resp4 = b.call(
            "tools/call",
            {
                "name": "exec",
                "arguments": {
                    "code": (
                        "_call_count = 0\n"
                        "@every(0.2, label='error_ticker')\n"
                        "def _err_ticker():\n"
                        "    global _call_count\n"
                        "    _call_count += 1\n"
                        "    raise ValueError('boom')\n"
                    ),
                },
            },
            timeout=5.0,
        )
        assert_true(
            not resp4["result"].get("isError", False), "@every error_ticker defined ok"
        )

        # First tick fires immediately → error channel push
        notif4 = b.wait_notification(
            "notifications/claude/channel", kind="every", timeout=5.0
        )
        params4 = notif4["params"]
        assert_eq(params4["meta"]["kind"], "every", "error tick kind=every")
        assert_eq(params4["meta"]["label"], "error_ticker", "error tick label")
        assert_eq(params4["meta"]["error"], "1", "error tick error=1")
        assert_true(
            "ValueError" in params4["content"],
            f"error message in content (got {params4['content']!r})",
        )
        print("  ✓ every: error in tick pushes kind=every error=1, loop survives")

        # Loop still alive — second tick should fire and also push error
        notif4b = b.wait_notification(
            "notifications/claude/channel", kind="every", timeout=5.0
        )
        assert_eq(
            notif4b["params"]["meta"]["kind"], "every", "second error tick kind=every"
        )
        assert_eq(notif4b["params"]["meta"]["error"], "1", "second error tick error=1")
        print("  ✓ every: loop continues after error tick")

        # --- 5. Async decorated function works ---
        resp5 = b.call(
            "tools/call",
            {
                "name": "exec",
                "arguments": {
                    "code": (
                        "_err_ticker.cancel()\n"  # stop previous ticker first
                        "import asyncio\n"
                        "@every(0.2, label='async_ticker')\n"
                        "async def _async_ticker():\n"
                        "    await asyncio.sleep(0)\n"
                        "    return 'async_tick'\n"
                    ),
                },
            },
            timeout=5.0,
        )
        assert_true(
            not resp5["result"].get("isError", False), "@every async_ticker defined ok"
        )

        notif5 = b.wait_notification(
            "notifications/claude/channel", kind="every", timeout=5.0
        )
        params5 = notif5["params"]
        assert_eq(params5["meta"]["kind"], "every", "async tick kind=every")
        assert_eq(params5["meta"]["label"], "async_ticker", "async tick label")
        assert_eq(params5["content"], "async_tick", "async tick content")
        print("  ✓ every: async decorated function works")

        # --- 6. cancel_all() clears registry ---
        resp6 = b.call(
            "tools/call",
            {
                "name": "exec",
                "arguments": {
                    "code": (
                        "every.cancel_all()\n"
                        "import asyncio\n"
                        "await asyncio.sleep(0.05)\n"
                        "len(every.list())"
                    ),
                },
            },
            timeout=3.0,
        )
        content6 = resp6["result"]["content"][0]["text"]
        assert_true(
            content6.strip() == "0",
            f"every.list() empty after cancel_all (got {content6!r})",
        )
        print("  ✓ every: cancel_all() clears registry")

    finally:
        b.close()
