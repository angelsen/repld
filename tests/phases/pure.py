"""Phase 2: pure logic, called directly — no kernel, bridge or Chrome."""

import ast
import re

from harness import assert_eq, assert_true

from repld import gate_cmd, gates, gist_api, render, tasks

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


def phase_2_pure() -> None:
    _gate_coercion()
    _answer_split()
    _preview()
    _render()
    _format_args()
