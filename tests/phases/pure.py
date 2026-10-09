"""Phase 2: pure logic, called directly — no kernel, bridge or Chrome."""

import ast
import asyncio
import contextlib
import io
import re
import subprocess
import threading
import time
import traceback
from pathlib import Path
from typing import Annotated, Optional

from harness import assert_eq, assert_true

from repld import (
    cli_args,
    core_schemas,
    gate_cmd,
    gates,
    gist_api,
    gist_lint,
    gists,
    kernel,
    lifecycle_cmd,
    render,
    tasks,
)

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(s: str) -> str:
    return _ANSI.sub("", s)


def _raises(fn, *args) -> str | None:
    try:
        fn(*args)
    except ValueError as exc:
        return str(exc)
    return None


def _gate_coercion() -> None:
    for word in ("y", "YES", " 1 ", "true"):
        assert_eq(gates.parse_response("confirm", word), True, f"confirm {word!r}")
    for word in ("n", "No", "0", "false"):
        assert_eq(gates.parse_response("confirm", word), False, f"confirm {word!r}")
    assert_true(_raises(gates.parse_response, "confirm", "maybe"), "confirm rejects")
    assert_true(_raises(gates.parse_response, "confirm", ""), "confirm has no default")

    opts = ["alpha", "beta", "2"]
    assert_eq(gates.parse_response("choose", "beta", opts), "beta", "choose by name")
    assert_eq(gates.parse_response("choose", "1", opts), "alpha", "choose by index")
    # An option literally named "2" wins over index 2: the name match comes first.
    assert_eq(gates.parse_response("choose", "2", opts), "2", "name beats index")
    for bad in ("0", "4", "gamma"):
        assert_true(_raises(gates.parse_response, "choose", bad, opts), f"choose {bad}")
    assert_eq(gates.parse_response("choose", "x", None), "x", "optionless choose")
    assert_eq(gates.parse_response("ask", "  free text "), "free text", "ask strips")

    for word, want in (
        (None, False),
        ("", True),
        ("  ", True),
        ("TRUE", True),
        ("n", False),
        ("sure", False),
    ):
        assert_eq(gates.yes_by_default(word), want, f"yes_by_default({word!r})")
    print(
        "  ✓ gate coercion: confirm/choose/ask tables, [Y/n] default, None is not consent"
    )


def _answer_split() -> None:
    split = gate_cmd._split_answer
    assert_eq(split(["--json"]), (["--json"], None), "listing form")
    assert_eq(
        split(
            ["--socket", "/s", "answer", "g1", "deploy", "--socket", "now", "--help"]
        ),
        (["--socket", "/s", "answer", "g1"], ["deploy", "--socket", "now", "--help"]),
        "flags after the id are the answer, verbatim",
    )
    assert_eq(
        split(["answer", "g1", "answer", "this"])[1], ["answer", "this"], "first verb"
    )
    assert_eq(split(["answer", "g1"]), (["answer", "g1"], []), "no value yet")
    assert_eq(split(["answer"]), (["answer"], []), "no id yet")
    print("  ✓ _split_answer: nothing past `answer <id>` reaches a flag parser")


def _preview() -> None:
    assert_eq(tasks._make_preview(""), ("", False), "empty")
    small = "x\n" * 10
    assert_eq(tasks._make_preview(small), (small, False), "under budget is verbatim")

    n = tasks.PREVIEW_HEAD_LINES + tasks.PREVIEW_TAIL_LINES + 7
    pad = "y" * (tasks.PREVIEW_MAX_BYTES // n + 1)
    many = "".join(f"{i} {pad}\n" for i in range(n))
    out, truncated = tasks._make_preview(many)
    assert_true(truncated, "many lines truncate")
    assert_true("… 7 lines elided …" in out, "elision counts the dropped lines")
    assert_true(
        out.startswith("0 ") and out.rstrip().splitlines()[-1].startswith(f"{n - 1} "),
        "head and tail kept",
    )

    wide = "z" * (tasks.PREVIEW_MAX_BYTES + 1) + "\n"
    out, truncated = tasks._make_preview(wide)
    assert_true(
        truncated and len(out) <= tasks.PREVIEW_MAX_LINE + 1, "one wide line is clamped"
    )
    assert_true(
        str(len(wide)) in out and out.endswith("\n"),
        "clamp reports the length, keeps the newline",
    )
    print("  ✓ _make_preview: verbatim, head+tail elision, per-line clamp")


def _render() -> None:
    assert_eq(render.gate_hint("confirm", None), "[y/n]", "confirm hint")
    assert_eq(render.gate_hint("choose", ["a", "b"]), "[1=a, 2=b]", "choose hint")
    assert_eq(render.gate_hint("choose", []), "", "optionless choose hint")
    assert_eq(render.gate_hint("ask", None), "", "ask hint")

    assert_eq(
        _plain(render.prompt_open("confirm", "Ship?", None, "g7")),
        "? Ship? [y/n] #g7: ",
        "prompt_open",
    )
    assert_eq(
        _plain(render.prompt_open("ask", "Name?", None)),
        "? Name?: ",
        "prompt_open, bare",
    )

    ok = _plain(render.cell_done_line("task-abc", 12.6, None))
    bad = _plain(render.cell_done_line("task-abc", 12.6, "ValueError"))
    assert_true(ok.startswith("✓") and "done" in ok and "13ms" in ok, "done line")
    assert_true(bad.startswith("✗") and "err(ValueError)" in bad, "error line")

    block = render.channel_block("one\ntwo\n", {"kind": "push", "n": 2})
    body = [ln for ln in block.splitlines() if "one" in ln or "two" in ln]
    # Only the border is coloured: the body is someone else's output.
    assert_eq([_plain(ln) for ln in body], ["│ one", "│ two"], "channel body lines")
    assert_true("kind=push  n=2" in _plain(block), "meta line")
    assert_eq(
        len(_plain(render.channel_block("x", {})).splitlines()),
        3,
        "no meta line when empty",
    )
    assert_true(
        _plain(render.channel_block("x", {}, t=0.0)).startswith("┌─ channel · "),
        "replay stamp",
    )
    print("  ✓ render: gate hints, prompt, done line, channel block")


def _format_args() -> None:
    def fmt(src: str, skip_self: bool = False) -> str:
        fn = ast.parse(src).body[0]
        assert isinstance(fn, ast.FunctionDef)
        return gist_api._format_args(fn.args, skip_self=skip_self)

    assert_eq(
        fmt("def f(a, b: int, c=1): ..."), "a, b: int, c=", "defaults align right"
    )
    assert_eq(
        fmt("def f(a, /, b=2, *, k, o: str = 'x'): ..."),
        "a, b=, *, k, o: str=",
        "posonly + kwonly",
    )
    assert_eq(
        fmt("def f(*args, k=None, **kw): ..."),
        "*args, k=, **kw",
        "vararg replaces bare *",
    )
    assert_eq(
        fmt("def f(self, x=1): ...", skip_self=True),
        "x=",
        "skip_self keeps default alignment",
    )
    assert_eq(fmt("def f(): ..."), "", "no args")
    print("  ✓ gist_api._format_args: positional, keyword-only, variadic, skip_self")


def _envelopes() -> None:
    assert_eq(
        core_schemas.response(7, {"ok": 1}),
        {"jsonrpc": "2.0", "id": 7, "result": {"ok": 1}},
        "response",
    )
    assert_eq(
        core_schemas.error(None, -32601, "nope"),
        {"jsonrpc": "2.0", "id": None, "error": {"code": -32601, "message": "nope"}},
        "error keeps a null id",
    )
    assert_true("params" not in core_schemas.notification("m"), "absent params")
    # MCP clients tell an absent `params` from an empty one, so {} must survive.
    assert_eq(core_schemas.notification("m", {})["params"], {}, "empty params kept")
    assert_true("id" not in core_schemas.notification("m", {"a": 1}), "no id")

    src = [{"uri": "u", "_attr": "GUIDE", "name": "n"}]
    assert_eq(core_schemas.wire(src), [{"uri": "u", "name": "n"}], "wire strips _keys")
    assert_true("_attr" in src[0], "wire leaves its input alone")
    print("  ✓ core_schemas: response/error/notification envelopes, wire()")


def _cli_args() -> None:
    assert_true(cli_args.wants_help(["foo", "--help"]), "help after a positional")
    assert_true(cli_args.wants_help(["-h"]), "-h")
    assert_true(not cli_args.wants_help(["--helpful", "help"]), "exact match only")

    def check(argv, **kw) -> tuple[int | None, str]:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli_args.check_args("repld x", argv, "usage: x", **kw)
        return code, err.getvalue()

    assert_eq(check(["a", "--json"], flags=("--json",)), (None, ""), "known flag")
    code, err = check(["--jsno"], flags=("--json",))
    assert_true(code == 2 and "'--jsno'" in err and "usage: x" in err, "unknown flag")
    code, err = check(["a", "b"])
    assert_true(code == 2 and "unexpected argument 'b'" in err, "surplus positional")
    assert_eq(check(["a", "b", "c"], positionals=None)[0], None, "any number of names")
    # Surplus only: a verb that *requires* a positional checks for it itself.
    assert_eq(check([])[0], None, "a missing positional is the caller's to refuse")
    code, err = check(["--bad", "a", "b"])
    assert_true("'--bad'" in err and "'b'" not in err, "unknown flag reported first")
    print("  ✓ cli_args: wants_help scans every arg; check_args refuses flags, surplus")


def _json_types() -> None:
    parts, resolve = gists._annotation_parts, gists._resolve_json_type
    assert_eq(parts(int), (int, None), "bare type")
    assert_eq(parts(Annotated[str, "an id"]), (str, "an id"), "Annotated description")
    assert_eq(
        parts(Annotated[str, 3, "late"]), (str, "late"), "non-str metadata skipped"
    )
    assert_eq(parts(Annotated[str, 3]), (str, None), "no str metadata")

    for annotation, want in (
        (str, "string"),
        (int, "integer"),
        (float, "number"),
        (bool, "boolean"),
        (list[str], "array"),
        (dict[str, int], "object"),
        (int | None, "integer"),
        (Optional[list[int]], "array"),             # noqa: UP045 — the typing.Union spelling is the case
        (Optional[Annotated[str, "x"]], "string"),  # noqa: UP045
        (int | str, None),
        (Path, None),
        (tuple[int, ...], None),
    ):
        assert_eq(resolve(annotation), want, f"_resolve_json_type({annotation!r})")

    def _tool_lookup(
        org: Annotated[str, "9-digit org number"], limit: int = 5, deep=False
    ):
        """Look a company up.

        More prose.
        """

    schema = gists._schema_from_signature(_tool_lookup, "lookup")
    assert_eq(schema["description"], "Look a company up.", "docstring first line")
    assert_eq(
        schema["inputSchema"],
        {
            "type": "object",
            "properties": {
                "org": {"type": "string", "description": "9-digit org number"},
                "limit": {"type": "integer", "default": 5},
                "deep": {"type": "string", "default": False},
            },
            "required": ["org"],
        },
        "schema from signature",
    )

    def _tool_bare():
        pass

    bare = gists._schema_from_signature(_tool_bare, "bare")
    assert_eq(bare["description"], "bare", "no docstring falls back to the tool name")
    assert_true("required" not in bare["inputSchema"], "no required key when empty")
    print("  ✓ gists: Annotated split, JSON type resolution, schema from signature")


def _lint_helpers() -> None:
    src = (
        "x = 1  # gistlint: ignore=deps,shape\n"
        "y = '# gistlint: ignore=all'\n"
        "# gistlint:ignore=legacy\n"
        "z = 3\n"
    )
    ignores = gist_lint._parse_ignores(src)
    # Tokenized, not grepped: the directive inside the string on line 2 is not one.
    assert_eq(ignores, {1: {"deps", "shape"}, 3: {"legacy"}}, "_parse_ignores")
    assert_true(gist_lint._is_ignored(1, "deps", ignores), "same line")
    assert_true(gist_lint._is_ignored(4, "legacy", ignores), "line above")
    assert_true(not gist_lint._is_ignored(5, "legacy", ignores), "two above is too far")
    assert_true(not gist_lint._is_ignored(1, "legacy", ignores), "other rule")
    assert_true(gist_lint._is_ignored(9, "x", {9: {"all"}}), "all covers any rule")
    assert_eq(
        gist_lint._parse_ignores("x = (  # gistlint: ignore=deps\n"),
        {1: {"deps"}},
        "unterminated source keeps what it saw",
    )

    def firstline(doc: str, ignores: dict | None = None) -> int:
        tree = ast.parse(f'"""{doc}"""\n')
        return len(gist_lint._check_firstline(Path("g.py"), tree, ignores or {}))

    assert_eq(
        firstline("Wraps the thing\nand more."), 1, "unterminated first line continues"
    )
    assert_eq(firstline("Wraps the thing.\nMore."), 0, "terminated")
    assert_eq(firstline("Wraps the thing:\n- a"), 0, "colon terminates")
    assert_eq(firstline("Wraps the thing"), 0, "single line")
    assert_eq(firstline("Wraps the thing\n\n"), 0, "nothing after it to truncate")
    assert_eq(
        firstline("Wraps\nmore", {40: {"firstline"}}), 0, "ignore applies file-wide"
    )
    assert_eq(
        len(gist_lint._check_firstline(Path("g.py"), ast.parse("x = 1"), {})),
        0,
        "no docstring",
    )

    def needs(sig: str) -> bool:
        fn = ast.parse(f"{sig}: ...").body[0]
        assert isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef)
        return gist_lint._needs_shape_doc(fn)

    for sig, want in (
        ("def f()", False),
        ("def f() -> None", False),
        ("def f() -> str", False),
        ("def f() -> dict", True),
        ("async def f() -> list[dict[str, Any]]", True),
        ("def f() -> 'Rows | None'", False),
        ("def f() -> Company", False),
        ("def f() -> Playlist", False),
        ("def f() -> typing.Any", True),
        ("def f() -> List[Company]", True),
    ):
        assert_eq(needs(sig), want, f"_needs_shape_doc({sig})")
    print("  ✓ gist_lint: ignore directives, firstline rule, shape-doc trigger")


def _sibling_counts_concurrency() -> None:
    """`_fetch_sibling_counts` fans siblings out concurrently — a wedged
    dashboard costs its own delay once, not once per sibling queued behind
    it in a serial loop (`repld status --json --counts`, reported live
    against 24 siblings by claude_code_research-5c)."""
    real = lifecycle_cmd._sibling_counts
    delay = 0.3

    def fake(sibling: dict):
        if sibling["cwd"] == "slow":
            time.sleep(delay)
            return None  # simulates a dashboard that never answers
        return (1, 2)

    lifecycle_cmd._sibling_counts = fake
    try:
        siblings = [{"cwd": "slow"}] + [{"cwd": f"fast{i}"} for i in range(5)]
        start = time.monotonic()
        lifecycle_cmd._fetch_sibling_counts(siblings)
        elapsed = time.monotonic() - start
        assert_true(
            elapsed < delay * 3,
            f"siblings fanned out concurrently, not serially "
            f"(took {elapsed:.2f}s for one {delay}s straggler among {len(siblings)})",
        )
        assert_true(
            all(s.get("tasks_active") == 1 for s in siblings if s["cwd"] != "slow"),
            "fast siblings got their counts",
        )
        slow = next(s for s in siblings if s["cwd"] == "slow")
        assert_true(
            "tasks_active" not in slow, "unreachable sibling keeps its keys absent"
        )
    finally:
        lifecycle_cmd._sibling_counts = real
    print("  ✓ _fetch_sibling_counts fans siblings out concurrently, not serially")


def _hold_loop_synchronously(seconds: float) -> None:
    time.sleep(seconds)


def _watchdog_run(scenario, *, threshold: float, kill: float | None) -> list[dict]:
    """Run `kernel._loop_watchdog` against a private loop while *scenario*
    (a coroutine function) runs there; return the pushes it made."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    assert thread.ident is not None
    pushes: list[dict] = []
    real = kernel.push_channel
    kernel.push_channel = lambda content, meta, **kw: pushes.append(
        {"content": content, **meta, "session": kw.get("session")}
    )
    stop = threading.Event()
    dog = threading.Thread(
        target=kernel._loop_watchdog,
        args=(loop, thread.ident, stop, threshold, kill, 0.05),
        daemon=True,
    )
    try:
        dog.start()
        asyncio.run_coroutine_threadsafe(scenario(), loop).result(timeout=10)
        # Let the watchdog observe the unblock before stopping it.
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not any(
            p["kind"] == "loop_unblocked" for p in pushes
        ):
            time.sleep(0.02)
    finally:
        stop.set()
        dog.join(2)
        kernel.push_channel = real
        loop.call_soon_threadsafe(loop.stop)
        thread.join(2)
    return pushes


def _kinds(pushes: list[dict]) -> list[str]:
    return [p["kind"] for p in pushes]


def _loop_watchdog() -> None:
    """The watchdog names the task holding the loop, not bystanders; one wedge
    is one `loop_blocked` + one `loop_unblocked`; the kill cancels only the
    holder, never a `repld-` task; `REPLD_LOOP_KILL_THRESHOLD` has an off switch."""
    for raw, want in (("30", 30.0), ("0", None), ("-1", None), ("inf", None)):
        assert_eq(kernel._kill_threshold(raw), want, f"kill threshold {raw!r}")

    task_id, _ = tasks.new_task()
    outcome: dict = {}

    async def named_holder() -> None:
        bystander = asyncio.create_task(asyncio.sleep(30), name="bystander")
        await asyncio.sleep(0.1)  # let a probe land before the block

        async def holder() -> None:
            tasks.set_current_task(task_id)
            # Longer than threshold + several intervals: a re-probing
            # watchdog would warn more than once.
            _hold_loop_synchronously(0.8)

        await asyncio.create_task(holder(), name="holder")
        outcome["bystander_cancelled"] = bystander.cancelled()
        bystander.cancel()

    pushes = _watchdog_run(named_holder, threshold=0.15, kill=None)
    assert_eq(
        _kinds(pushes), ["loop_blocked", "loop_unblocked"], "one wedge, one report"
    )
    blocked = pushes[0]
    assert_eq(blocked["task"], "holder", "names the task holding the loop")
    assert_eq(blocked["task_id"], task_id, "and the repld task it runs for")
    assert_true(
        "_hold_loop_synchronously" in blocked["content"], "stack names the blocker"
    )
    assert_true("bystander" not in blocked["content"], "bystanders go unnamed")
    assert_true("asyncio/" not in blocked["content"], "loop machinery is trimmed")
    assert_true(float(pushes[1]["blocked_s"]) >= 0.5, "unblocked carries the duration")

    async def killable() -> None:
        bystander = asyncio.create_task(asyncio.sleep(30), name="bystander")
        await asyncio.sleep(0.1)

        async def holder() -> None:
            _hold_loop_synchronously(0.5)
            await asyncio.sleep(30)  # the cancel lands here

        victim = asyncio.create_task(holder(), name="holder")
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(victim), 2)
        await asyncio.sleep(0)
        outcome["victim_cancelled"] = victim.cancelled()
        outcome["bystander_cancelled"] = bystander.cancelled()
        bystander.cancel()

    pushes = _watchdog_run(killable, threshold=0.1, kill=0.25)
    assert_eq(
        _kinds(pushes), ["loop_blocked", "loop_kill", "loop_unblocked"], "kill sequence"
    )
    assert_eq(pushes[1]["task"], "holder", "loop_kill names the holder")
    assert_eq(outcome["victim_cancelled"], True, "the holder is cancelled")
    assert_eq(outcome["bystander_cancelled"], False, "the bystander is not")

    async def in_stdlib() -> None:
        await asyncio.sleep(0.1)

        async def holder() -> None:
            subprocess.run(["sleep", "0.5"], check=True)  # noqa: ASYNC221 -- the block under test

        await asyncio.create_task(holder(), name="stdlib-holder")

    pushes = _watchdog_run(in_stdlib, threshold=0.15, kill=None)
    blocked_at = next(
        (ln for ln in pushes[0]["content"].splitlines() if "blocked at:" in ln), ""
    )
    assert_true(
        "pure.py" in blocked_at and "in holder" in blocked_at,
        f"blocked at: names the caller, not subprocess.py (got {blocked_at!r})",
    )
    assert_true("subprocess.py" in pushes[0]["content"], "library frames stay listed")

    async def internal() -> None:
        await asyncio.sleep(0.1)

        async def holder() -> None:
            _hold_loop_synchronously(0.5)

        await asyncio.create_task(holder(), name="repld-internal")

    pushes = _watchdog_run(internal, threshold=0.1, kill=0.25)
    assert_eq(
        _kinds(pushes), ["loop_blocked", "loop_unblocked"], "repld- holder is spared"
    )
    print("  ✓ loop watchdog names the holder, reports once, kills only the holder")


def _starvation_classification() -> None:
    """A wedge where the loop was never scheduled reads differently from one
    a callback actually held — misdirecting the first to `asyncio.to_thread()`
    sends people to rewrite code that's already async (flagged live by
    claude-code-research-6d against the claude_code_research kernel: a
    single-frame `selectors.*.select` wedge and a `subprocess.py
    _execute_child` wedge inside an already-async `create_subprocess_exec`,
    both under real swap pressure, both got the to_thread advice). A third
    report from the same session: `cdp.py`'s `store_event`, a DuckDB insert
    documented in microseconds, took 7.6s under swap at 27/31 GB and load
    18.9 — a normal-looking frame that no shape-based rule catches, so
    `_watch_block` also weighs `_read_pressure().overloaded` regardless of
    the frame."""
    frame = lambda path, name: traceback.FrameSummary(path, 1, name)

    assert_true(
        kernel._is_starved([frame("/usr/lib/python3.12/selectors.py", "select")]),
        "the whole stack is the loop's own select() — nothing else was running",
    )
    assert_true(
        not kernel._is_starved(
            [
                frame("/gists/x.py", "holder"),
                frame("/usr/lib/python3.12/selectors.py", "select"),
            ]
        ),
        "select() below a real frame is a callback that itself calls select, not idling",
    )
    assert_true(
        kernel._is_starved(
            [
                frame("/gists/android.py", "_check"),
                frame("/usr/lib/python3.12/subprocess.py", "_execute_child"),
            ]
        ),
        "fork/exec itself is on the loop by necessity, even from create_subprocess_exec",
    )
    assert_true(
        not kernel._is_starved(
            [
                frame("/gists/x.py", "holder"),
                frame("/usr/lib/python3.12/subprocess.py", "_wait"),
            ]
        ),
        "waiting on an already-spawned child is a real block, not fork/exec",
    )

    _pressure_defaults = {
        "load1": 1.0,
        "cores": 8,
        "mem_used": 1,
        "mem_total": 10,
        "swap_used": 0,
        "swap_total": 10,
    }
    pressure = lambda **kw: kernel._Pressure(**{**_pressure_defaults, **kw})
    assert_true(not pressure().overloaded, "1.0 load on 8 cores, no swap: fine")
    assert_true(pressure(load1=9.0).overloaded, "load past core count is overloaded")
    assert_true(
        pressure(swap_used=9, swap_total=10).overloaded, "swap past 80% is overloaded"
    )
    assert_true(
        not pressure(load1=7.9, swap_used=7, swap_total=10).overloaded,
        "under both thresholds is not overloaded",
    )

    async def blocked() -> None:
        await asyncio.sleep(0.05)
        _hold_loop_synchronously(0.3)

    real_holder, real_pressure = kernel._loop_holder, kernel._read_pressure
    try:
        kernel._loop_holder = lambda loop, thread_id: kernel._Holder(
            task=None, task_id=None, stack=["s.py:1 in select"], starved=True
        )
        kernel._read_pressure = lambda: None
        pushes = _watchdog_run(blocked, threshold=0.15, kill=None)
    finally:
        kernel._loop_holder, kernel._read_pressure = real_holder, real_pressure
    content = pushes[0]["content"]
    assert_true(
        "descheduled" in content and "to_thread" not in content,
        f"a starved wedge gets the descheduled line, not the to_thread advice (got {content!r})",
    )

    try:
        kernel._loop_holder = lambda loop, thread_id: kernel._Holder(
            task=None,
            task_id=None,
            stack=["cdp.py:979 in _handle_event", "cdp.py:859 in store_event"],
            starved=False,
        )
        kernel._read_pressure = lambda: pressure(
            load1=18.9, swap_used=27, swap_total=31
        )
        pushes = _watchdog_run(blocked, threshold=0.15, kill=None)
    finally:
        kernel._loop_holder, kernel._read_pressure = real_holder, real_pressure
    content = pushes[0]["content"]
    assert_true(
        "descheduled" in content and "to_thread" not in content,
        f"an ordinary-looking frame under severe pressure is still starved, not "
        f"blamed on the callback (got {content!r})",
    )
    print(
        "  ✓ starvation classification: frame shape and system pressure both "
        "override the to_thread advice"
    )


def _crash_reason() -> None:
    from repld import bridge

    oom = (
        "Main process exited, code=killed, status=9/KILL\n"
        "repld-x.service: Failed with result 'oom-kill'.\n"
    )
    assert_eq(
        bridge._reason_from_journal(oom),
        "systemd result oom-kill, main process KILL",
        "an OOM-killed kernel names both the result and the signal",
    )
    assert_eq(
        bridge._reason_from_journal("Main process exited, code=killed, status=9/KILL"),
        "main process killed by KILL",
        "a bare SIGKILL is still reported",
    )
    assert_eq(
        bridge._reason_from_journal("Main process exited, code=exited, status=15/TERM"),
        None,
        "a SIGTERM is a deliberate stop, not a cause",
    )
    # The child-only OOM line fires while the kernel survives, so it is no cause.
    assert_eq(
        bridge._reason_from_journal(
            "The kernel OOM killer killed some processes in this unit."
        ),
        None,
        "a killed child alone names no cause",
    )
    print(
        "  ✓ bridge._reason_from_journal: oom-kill / SIGKILL named, TERM and child-only OOM ignored"
    )


def _tab_owner_routing() -> None:
    from types import SimpleNamespace
    from typing import Any, cast

    NS = cast(Any, SimpleNamespace)

    from repld.browser.cdp import CDPSession
    from repld.browser.session import BrowserSession

    route = cast(Any, CDPSession.route_to).fget
    owner, caller = NS(closed=False), NS(closed=False)
    assert_eq(
        route(NS(owner=owner, last_caller=caller)), owner, "owner beats last_caller"
    )
    owner.closed = True
    assert_eq(
        route(NS(owner=owner, last_caller=caller)),
        caller,
        "a closed owner falls back to last_caller",
    )
    assert_eq(route(NS(owner=None, last_caller=None)), None, "unclaimed tab broadcasts")

    watcher, opener_owner = NS(), NS()
    parent = NS(owner=opener_owner)
    fake = NS(
        _pattern_owners={"*app.test*": watcher},
        find_by_target_id=lambda tid: parent if tid == "P" else None,
    )
    inherit = cast(Any, BrowserSession._inherited_owner)
    assert_eq(
        inherit(fake, {"url": "https://app.test/x", "openerId": "P"}),
        watcher,
        "a pattern match inherits the watcher",
    )
    assert_eq(
        inherit(fake, {"url": "https://other.test/", "openerId": "P"}),
        opener_owner,
        "an opener-matched tab inherits its opener's owner",
    )
    assert_eq(inherit(fake, {"url": "https://other.test/"}), None, "no match, no owner")

    sess = NS()
    seen: list = []

    async def _probe() -> None:
        tasks._tool_caller.set(sess)
        seen.append(tasks.current_origin())

    asyncio.run(_probe())
    assert_true(seen == [sess], "a tool call's caller is visible as current_origin")
    print(
        "  ✓ tab owner: route_to precedence, watch/opener inheritance, tool-call origin"
    )


def _tab_lifecycle_pushes() -> None:
    from types import SimpleNamespace
    from typing import Any, cast

    from repld.browser import cdp as cdp_mod
    from repld.browser import session as session_mod
    from repld.browser.session import BrowserSession

    NS = cast(Any, SimpleNamespace)
    sent: list = []

    def _fake_push(content, meta=None, **kw):
        sent.append(((meta or {})["kind"], kw))

    owner = NS()
    tab = NS(
        port=9222,
        chrome_target_id="ABCDEF123",
        target_info={"url": "https://x.test/"},
        route_to=owner,
        closing=False,
    )
    real_cdp, real_session = cdp_mod.push_channel, session_mod.push_channel
    cdp_mod.push_channel = session_mod.push_channel = _fake_push
    try:
        cdp_mod._push_tab_crashed(tab)
        assert_eq(
            sent[-1],
            ("tab_crashed", {"session": owner, "fallback_broadcast": True}),
            "a crash goes to the tab's route_to, broadcasting on a miss",
        )

        announce = cast(Any, BrowserSession._announce_tab_closed)
        announce(NS(port=9222), tab)
        assert_eq(
            sent[-1],
            ("tab_closed", {"session": owner}),
            "an outside close is told to the owner, never broadcast",
        )

        n = len(sent)
        tab.closing = True
        announce(NS(port=9222), tab)
        tab.closing, tab.route_to = False, None
        announce(NS(port=9222), tab)
        assert_eq(len(sent), n, "repld's own close and an unclaimed tab push nothing")
    finally:
        cdp_mod.push_channel, session_mod.push_channel = real_cdp, real_session
    print("  ✓ tab lifecycle: crash routed, outside close targeted, own close silent")


def _files_probe_timeout() -> None:
    from types import SimpleNamespace
    from typing import Any, cast

    from repld.browser.tab import Tab

    probe = cast(Any, Tab._probe_files_readable)

    def _tab(reply):
        async def _exec(method, params=None, timeout=30):
            if method == "DOM.resolveNode":
                return {"object": {"objectId": "o"}}
            if isinstance(reply, Exception):
                assert timeout < 30, "the probe must bound its own wait"
                raise reply
            return reply

        return SimpleNamespace(_exec=_exec)

    note = asyncio.run(probe(_tab(TimeoutError()), 1))
    assert_true("not verified" in note, "a stalled probe reports instead of raising")
    ok = asyncio.run(probe(_tab({"result": {"value": []}}), 1))
    assert_eq(ok, "", "a readable file adds no note")
    err = ""
    try:
        asyncio.run(probe(_tab({"result": {"value": ["a.txt: NotReadableError"]}}), 1))
    except RuntimeError as exc:
        err = str(exc)
    assert_true("unreadable" in err, "an unreadable file still raises")
    print(
        "  ✓ set_files probe: stalled -> note, readable -> silent, unreadable -> raises"
    )


def phase_2_pure() -> None:
    _gate_coercion()
    _answer_split()
    _preview()
    _render()
    _format_args()
    _envelopes()
    _cli_args()
    _json_types()
    _lint_helpers()
    _sibling_counts_concurrency()
    _loop_watchdog()
    _starvation_classification()
    _crash_reason()
    _tab_owner_routing()
    _tab_lifecycle_pushes()
    _files_probe_timeout()
