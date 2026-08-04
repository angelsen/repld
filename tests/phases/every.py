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

        # delay= holds the first tick back. The default (tick now) health-checks
        # a resource at its most fragile moment when the ticker is registered
        # right after starting it — a false negative there can send a re-raise
        # loop after something that was about to be fine.
        b.call(
            "tools/call",
            {
                "name": "exec",
                "arguments": {
                    "code": (
                        "DELAYED = []\n"
                        "@every(0.2, delay=1.5, label='delayed')\n"
                        "def _d():\n"
                        "    DELAYED.append(1)\n"
                    )
                },
            },
            timeout=5.0,
        )
        resp = b.call(
            "tools/call",
            {"name": "exec", "arguments": {"code": "print(len(DELAYED))"}},
            timeout=5.0,
        )
        assert_eq(
            resp["result"]["content"][0]["text"].strip(),
            "0",
            "delay= suppresses the immediate first tick",
        )
        # ...and it does eventually tick, so delay isn't just "never run".
        resp = b.call(
            "tools/call",
            {
                "name": "exec",
                "arguments": {
                    "code": "import asyncio\nawait asyncio.sleep(2.0)\nprint(len(DELAYED) > 0)",
                    "timeout": 6,
                },
            },
            timeout=15.0,
        )
        assert_true(
            "True" in resp["result"]["content"][0]["text"],
            "the delayed ticker does fire once the delay elapses",
        )
        b.call(
            "tools/call",
            {"name": "exec", "arguments": {"code": "every.cancel_all()"}},
            timeout=5.0,
        )
        print("  ✓ every: delay= defers the first tick, then ticks normally")

        # --- 7. A ticker's output is ambient, not the registering cell's ---
        #
        # `every(...)` applied inside an exec cell schedules the ticker from
        # that cell's context, so `copy_context()` hands the ticker task the
        # cell's `_current_task` — and unlike a `defer()`, the ticker outlives
        # the cell by weeks. Every cap downstream is keyed on that id and reset
        # only by the cell's `CellDone`, which fires long before the ticker's
        # second tick: the pane (`display._truncated_tasks`) and the event log
        # (`eventlog._chunk_capped`) each dropped everything past 4 KB
        # *permanently*, and on a headless kernel the event log is the only
        # surface there is.
        #
        # Asserted through `get_task` on the *registering* cell, because
        # `snapshot()` re-reads that cell's spill file live — and `finalize`
        # deliberately leaves the handle open so background work can keep
        # writing to it. So the spill is where the misattribution is visible
        # from outside: the cell's own print has to be there (the control, or
        # the absence below proves nothing) and the ticker's must not.
        resp7 = b.call(
            "tools/call",
            {
                "name": "exec",
                "arguments": {
                    "code": (
                        "from repld import tasks as _t\n"
                        "TICK_CTX = []\n"
                        "print('CELL-OUTPUT')\n"
                        "@every(0.2, label='ctx_probe')\n"
                        "def _ctx():\n"
                        "    TICK_CTX.append(_t.current_task_id())\n"
                        "    print('TICK-OUTPUT')\n"
                    )
                },
            },
            timeout=5.0,
        )
        assert_true(
            not resp7["result"].get("isError", False),
            f"@every ctx_probe defined ok: {resp7['result']['content'][0]['text']!r}",
        )
        cell_tid = resp7["result"]["_meta"]["task_id"]

        # Let it tick a few times, from a cell of its own (whose task id is
        # unrelated to the one under test).
        b.call(
            "tools/call",
            {
                "name": "exec",
                "arguments": {
                    "code": "import asyncio\nawait asyncio.sleep(0.7)",
                    "timeout": 5,
                },
            },
            timeout=10.0,
        )

        # The damage first...
        resp = b.call(
            "tools/call",
            {"name": "get_task", "arguments": {"task_id": cell_tid}},
            timeout=5.0,
        )
        spill = resp["result"]["_meta"]["text"]
        assert_true(
            "CELL-OUTPUT" in spill,
            f"control: the registering cell's own print is in its spill (got {spill!r})",
        )
        assert_true(
            "TICK-OUTPUT" not in spill,
            f"ticker output is not written to the registering cell's spill "
            f"(got {spill!r})",
        )

        # ...then the cause, so a failure says which of the two broke.
        resp = b.call(
            "tools/call",
            {
                "name": "exec",
                "arguments": {
                    "code": (
                        "print(len(TICK_CTX) >= 2, all(x is None for x in TICK_CTX))"
                    )
                },
            },
            timeout=5.0,
        )
        assert_eq(
            resp["result"]["content"][0]["text"].strip(),
            "True True",
            "a tick body runs with no current task id (ticked >=2x, all None)",
        )
        b.call(
            "tools/call",
            {"name": "exec", "arguments": {"code": "every.cancel_all()"}},
            timeout=5.0,
        )
        print("  ✓ every: tick output is ambient, not the registering cell's")

    finally:
        b.close()
