"""Phase 17: human gates on a headless kernel — `repld gate` list / answer.

The harness kernel runs `--no-display`, which is precisely the case that used to
have no answering surface at all: no pane reading stdin, no pinned tab. So these
assertions run against the configuration that matters.
"""

import json
import subprocess
import time

from harness import REPO, Bridge, Kernel, assert_eq, assert_true


def _gate_cli(kernel: Kernel, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["uv", "run", "--project", str(REPO), "repld", "gate", *args],
        cwd=str(kernel.cwd),
        capture_output=True,
        text=True,
        timeout=60,
    )


def _open_gate(b: Bridge, code: str) -> str:
    """Start a cell that blocks on a gate. Returns its task_id."""
    resp = b.call(
        "tools/call",
        {"name": "exec", "arguments": {"code": code, "timeout": 1.0}},
        timeout=15.0,
    )
    meta = resp["result"]["_meta"]
    assert_eq(meta["done"], False, "gate cell is still running")
    return meta["task_id"]


def _await_task(b: Bridge, task_id: str, timeout: float = 10.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = b.call(
            "tools/call", {"name": "get_task", "arguments": {"task_id": task_id}}
        )
        snap = resp["result"]["_meta"]
        if snap["done"]:
            return snap["text"]
        time.sleep(0.1)
    raise AssertionError(f"task {task_id} never completed")


def _pending(kernel: Kernel) -> list[dict]:
    proc = _gate_cli(kernel, "--json")
    assert_eq(proc.returncode, 0, f"repld gate --json exits 0 (stderr: {proc.stderr})")
    return json.loads(proc.stdout)


def phase_17_gates(kernel: Kernel) -> None:
    b = Bridge(kernel.cwd)
    try:
        b.call("initialize", {"protocolVersion": "2024-11-05"})
        b.send("notifications/initialized", {}, notif=True)

        assert_eq(_pending(kernel), [], "no gates pending before any are opened")

        # -- confirm, answered from the CLI -----------------------------------
        task_id = _open_gate(b, 'await confirm("Deploy the release?")')

        push = b.wait_notification(
            "notifications/claude/channel", kind="awaiting_human", timeout=10.0
        )
        params = push["params"]
        gate_id = params["meta"]["gate_id"]
        assert_eq(params["meta"]["prompt_kind"], "confirm", "push names the gate kind")
        # The headless hint is the whole point: the agent that sees this push is
        # the one that has to tell a human where to answer.
        assert_true(
            "no terminal attached" in params["content"]
            and f"repld gate answer {gate_id}" in params["content"],
            f"push carries the headless answer command (got {params['content']!r})",
        )
        print(f"  ✓ awaiting_human push carries gate {gate_id} + answer command")

        listed = _pending(kernel)
        assert_eq(len(listed), 1, "exactly one gate pending")
        assert_eq(listed[0]["gate_id"], gate_id, "listed gate id matches the push")
        assert_eq(listed[0]["prompt"], "Deploy the release?", "listed prompt")
        assert_true(
            listed[0]["waiting_s"] >= 0, "listed gate reports how long it waited"
        )
        print("  ✓ repld gate --json lists the pending confirm")

        bad = _gate_cli(kernel, "answer", gate_id, "maybe")
        assert_true(bad.returncode != 0, "a confirm rejects a non-y/n answer")
        assert_eq(len(_pending(kernel)), 1, "rejected answer leaves the gate open")
        print("  ✓ invalid answer rejected, gate stays open")

        ok = _gate_cli(kernel, "answer", gate_id, "y")
        assert_eq(
            ok.returncode, 0, f"answering the confirm exits 0 (stderr: {ok.stderr})"
        )
        assert_eq(_await_task(b, task_id).strip(), "True", "confirm() returned True")
        assert_eq(_pending(kernel), [], "answered gate is no longer pending")
        print("  ✓ repld gate answer unblocked the cell")

        # -- choose, answered by 1-based index --------------------------------
        # The prompt advertises `1=alpha, 2=beta`, so an index has to resolve to
        # the option; it used to come back as the literal string "2".
        task_id = _open_gate(b, 'await choose("Pick one", ["alpha", "beta"])')
        gate = _pending(kernel)[0]
        assert_eq(gate["kind"], "choose", "choose gate listed with its kind")
        assert_eq(gate["options"], ["alpha", "beta"], "choose gate lists its options")

        rejected = _gate_cli(kernel, "answer", gate["gate_id"], "3")
        assert_true(rejected.returncode != 0, "out-of-range choose index rejected")

        ok = _gate_cli(kernel, "answer", gate["gate_id"], "2")
        assert_eq(
            ok.returncode, 0, f"answering the choose exits 0 (stderr: {ok.stderr})"
        )
        # The cell auto-prints its value, so a str arrives repr'd.
        assert_eq(
            _await_task(b, task_id).strip(), "'beta'", "index 2 resolved to 'beta'"
        )
        print("  ✓ choose answered by index resolves to the option name")

        # -- ask, multi-word answer -------------------------------------------
        task_id = _open_gate(b, 'await ask("Release name?")')
        gate = _pending(kernel)[0]
        ok = _gate_cli(kernel, "answer", gate["gate_id"], "the", "big", "one")
        assert_eq(ok.returncode, 0, f"answering the ask exits 0 (stderr: {ok.stderr})")
        assert_eq(
            _await_task(b, task_id).strip(),
            "'the big one'",
            "unquoted multi-word ask answer is joined",
        )
        print("  ✓ ask takes an unquoted multi-word answer")

        # -- unknown id --------------------------------------------------------
        missing = _gate_cli(kernel, "answer", "deadbeef", "y")
        assert_true(missing.returncode != 0, "answering an unknown gate id fails")
        assert_true(
            "deadbeef" in (missing.stderr + missing.stdout),
            "the unknown-gate error names the id",
        )
        print("  ✓ unknown gate id reported, not silently swallowed")

        _pane_reshows_the_question()
    finally:
        b.close()


def _pane_reshows_the_question() -> None:
    """With two gates open, answering one must re-ask the *other* by name.

    The pane is headless in this suite, so drive its renderers directly. This
    is the surface `repld gate` made reachable: an out-of-band answer for an
    older gate leaves the newer one on screen, and re-showing it with a
    placeholder would leave the human looking at `? still waiting [y/n]`.
    """
    from repld import display
    from repld.events import HumanPromptOpen, HumanPromptResponse

    written: list[str] = []
    real_out, display._out = display._out, written.append
    real_gates = dict(display._open_gates)
    try:
        display._open_gates.clear()
        display._render_prompt_open(HumanPromptOpen("aaa", "confirm", "Deploy?", None))
        display._render_prompt_open(
            HumanPromptOpen("bbb", "choose", "Which region?", ["eu", "us"])
        )
        written.clear()
        # `repld gate answer aaa y` — the *older* one, answered elsewhere.
        display._render_prompt_response(HumanPromptResponse("aaa", True))
        out = "".join(written)
        assert_true(
            "Which region?" in out,
            f"the surviving gate is re-asked by name (got {out!r})",
        )
        assert_true("still waiting" not in out, f"no placeholder prompt (got {out!r})")
        assert_true("1=eu, 2=us" in out, f"and with its own options (got {out!r})")
        assert_eq(list(display._open_gates), ["bbb"], "only the answered gate dropped")
    finally:
        display._out = real_out
        display._open_gates.clear()
        display._open_gates.update(real_gates)
    print("  ✓ pane re-asks the surviving gate's own question, not a placeholder")
