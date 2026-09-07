"""Phase 10: @every decorator — periodic ticker via the kernel's shared loop."""

from harness import Bridge, Kernel, assert_eq, assert_true, content_text


def phase_10_every(kernel: Kernel) -> None:
    """@every fires immediately, pushes channel, cancel stops it, errors survive."""
    b = Bridge(kernel.cwd)
    try:
        b.handshake()
        _immediate_tick_list_cancel(b)
        _error_tick_survives(b)
        _async_fn_and_cancel_all(b)
        _delay_defers_first_tick(b)
        _tick_output_is_ambient(b)
        _tab_without_browser_refuses(b)
    finally:
        b.close()


def _tick(b: Bridge, label: str, timeout: float = 5.0) -> dict:
    """The next kind=every push from *label*'s ticker.

    Filtered by label: a just-cancelled ticker may have pushed once more, and
    the stash would hand that tick back to an unfiltered wait.
    """
    return b.wait_notification(
        "notifications/claude/channel",
        kind="every",
        where=lambda m: m["params"]["meta"].get("label") == label,
        timeout=timeout,
    )["params"]


def _registry_size(b: Bridge) -> str:
    resp = b.exec("import asyncio\nawait asyncio.sleep(0.05)\nlen(every.list())")
    return content_text(resp).strip()


def _immediate_tick_list_cancel(b: Bridge) -> None:
    """Register → the first tick pushes at once → list shows it → cancel empties."""
    resp = b.exec("@every(0.2)\ndef _ticker():\n    return 'tick'\n")
    assert_true(
        not resp["result"].get("isError", False),
        f"@every decoration raised: {content_text(resp)!r}",
    )
    print("  ✓ every: decorated without error")

    params = _tick(b, "_ticker")
    assert_eq(params["meta"]["kind"], "every", "first tick kind=every")
    assert_eq(params["meta"]["label"], "_ticker", "first tick label=_ticker")
    assert_eq(params["content"], "tick", "first tick content")
    print("  ✓ every: immediate first tick pushed to channel")

    listed = content_text(b.exec("[(h.label, h.seconds) for h in every.list()]"))
    assert_true("_ticker" in listed, f"every.list() shows handle (got {listed!r})")
    print("  ✓ every: every.list() shows active handle")

    b.exec("_ticker.cancel()")
    size = _registry_size(b)
    assert_true(size == "0", f"every.list() empty after cancel (got {size!r})")
    print("  ✓ every: cancel() removes handle, registry empty")


def _error_tick_survives(b: Bridge) -> None:
    """An exception in a tick pushes error=1 and the ticker keeps going."""
    resp = b.exec(
        "@every(0.2, label='error_ticker')\n"
        "def _err_ticker():\n"
        "    raise ValueError('boom')\n"
    )
    assert_true(
        not resp["result"].get("isError", False), "@every error_ticker defined ok"
    )
    try:
        params = _tick(b, "error_ticker")
        assert_eq(params["meta"]["kind"], "every", "error tick kind=every")
        assert_eq(params["meta"]["error"], "1", "error tick error=1")
        assert_true(
            "ValueError" in params["content"],
            f"error message in content (got {params['content']!r})",
        )
        print("  ✓ every: error in tick pushes kind=every error=1, loop survives")

        again = _tick(b, "error_ticker")
        assert_eq(again["meta"]["error"], "1", "second error tick error=1")
        print("  ✓ every: loop continues after error tick")
    finally:
        b.exec("_err_ticker.cancel()")


def _async_fn_and_cancel_all(b: Bridge) -> None:
    resp = b.exec(
        "import asyncio\n"
        "@every(0.2, label='async_ticker')\n"
        "async def _async_ticker():\n"
        "    await asyncio.sleep(0)\n"
        "    return 'async_tick'\n"
    )
    assert_true(
        not resp["result"].get("isError", False), "@every async_ticker defined ok"
    )
    params = _tick(b, "async_ticker")
    assert_eq(params["meta"]["label"], "async_ticker", "async tick label")
    assert_eq(params["content"], "async_tick", "async tick content")
    print("  ✓ every: async decorated function works")

    b.exec("every.cancel_all()")
    size = _registry_size(b)
    assert_true(size == "0", f"every.list() empty after cancel_all (got {size!r})")
    print("  ✓ every: cancel_all() clears registry")


def _delay_defers_first_tick(b: Bridge) -> None:
    """delay= holds the first tick back.

    The default (tick now) health-checks a resource at its most fragile moment
    when the ticker is registered right after starting it — a false negative
    there can send a re-raise loop after something that was about to be fine.
    """
    b.exec(
        "DELAYED = []\n"
        "@every(0.2, delay=1.5, label='delayed')\n"
        "def _d():\n"
        "    DELAYED.append(1)\n"
    )
    try:
        assert_eq(
            content_text(b.exec("print(len(DELAYED))")).strip(),
            "0",
            "delay= suppresses the immediate first tick",
        )
        # ...and it does eventually tick, so delay isn't just "never run".
        resp = b.exec(
            "import asyncio\nawait asyncio.sleep(2.0)\nprint(len(DELAYED) > 0)",
            timeout=6,
            call_timeout=15.0,
        )
        assert_true(
            "True" in content_text(resp),
            "the delayed ticker does fire once the delay elapses",
        )
    finally:
        b.exec("every.cancel_all()")
    print("  ✓ every: delay= defers the first tick, then ticks normally")


def _tick_output_is_ambient(b: Bridge) -> None:
    """A ticker's output is ambient, not the registering cell's.

    `every(...)` applied inside an exec cell schedules the ticker from that
    cell's context, so `copy_context()` hands the ticker task the cell's
    `_current_task` — and unlike a `defer()`, the ticker outlives the cell by
    weeks. Every cap downstream is keyed on that id and reset only by the
    cell's `CellDone`, which fires long before the ticker's second tick: the
    pane (`display._truncated_tasks`) and the event log
    (`eventlog._chunk_capped`) each dropped everything past 4 KB
    *permanently*, and on a headless kernel the event log is the only surface
    there is.

    Asserted through `get_task` on the *registering* cell, because
    `snapshot()` re-reads that cell's spill file live — and `finalize`
    deliberately leaves the handle open so background work can keep writing
    to it. So the spill is where the misattribution is visible from outside:
    the cell's own print has to be there (the control, or the absence below
    proves nothing) and the ticker's must not.
    """
    resp = b.exec(
        "from repld import tasks as _t\n"
        "TICK_CTX = []\n"
        "print('CELL-OUTPUT')\n"
        "@every(0.2, label='ctx_probe')\n"
        "def _ctx():\n"
        "    TICK_CTX.append(_t.current_task_id())\n"
        "    print('TICK-OUTPUT')\n"
    )
    assert_true(
        not resp["result"].get("isError", False),
        f"@every ctx_probe defined ok: {content_text(resp)!r}",
    )
    cell_tid = resp["result"]["_meta"]["task_id"]
    try:
        # Let it tick a few times, from a cell of its own (whose task id is
        # unrelated to the one under test).
        b.exec("import asyncio\nawait asyncio.sleep(0.7)", timeout=5, call_timeout=10.0)

        # The damage first...
        resp = b.call(
            "tools/call", {"name": "get_task", "arguments": {"task_id": cell_tid}}
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
        probe = b.exec("print(len(TICK_CTX) >= 2, all(x is None for x in TICK_CTX))")
        assert_eq(
            content_text(probe).strip(),
            "True True",
            "a tick body runs with no current task id (ticked >=2x, all None)",
        )
    finally:
        b.exec("every.cancel_all()")
    print("  ✓ every: tick output is ambient, not the registering cell's")


def _tab_without_browser_refuses(b: Bridge) -> None:
    """every(tab=) refuses at registration when browser is absent.

    `browser` is injected once at boot, so a kernel without it can never grow
    one mid-life — a per-tick check would push the same error every `seconds`
    forever with no way to self-heal. The dev kernel has the extra installed,
    so absence is simulated by deleting the builtin.
    """
    resp = b.exec(
        "import __main__ as _m\n"
        "_saved_browser = getattr(_m, 'browser', None)\n"
        "if _saved_browser is not None:\n"
        "    del _m.browser\n"
        "try:\n"
        "    every(0.2, tab='*nope*')(lambda t: None)\n"
        "finally:\n"
        "    if _saved_browser is not None:\n"
        "        _m.browser = _saved_browser\n"
    )
    text = content_text(resp)
    assert_true(
        resp["result"].get("isError", False),
        f"every(tab=) without browser errors the registering cell (got {text!r})",
    )
    assert_true("repld browser" in text, f"the refusal names the fix (got {text!r})")
    assert_eq(
        content_text(b.exec("len(every.list())")).strip(),
        "0",
        "the refused registration left no ticker behind",
    )
    print("  ✓ every: tab= without the browser builtin refuses at registration")
