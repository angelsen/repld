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
        check=False,
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


def _tty_prompt_declines_once_display_owns_stdin() -> None:
    """`tty_prompt` is boot-only, and refuses rather than fighting the pane.

    Once `display.run_display` starts its reader thread, that thread is parked
    in readline() on sys.__stdin__ and eats every line whether a gate is open
    or not. A second reader doesn't race it for the line so much as block
    behind its buffer lock — and the caller that gets here late is
    `gist_deps.install_deps`, reached from a gist import inside a user cell. On
    an `await` cell that runs on the shared loop, so the kernel wedges with no
    await point for the watchdog's cancel to land on. Declining puts it on the
    same path as a headless kernel, which every caller already handles.
    """
    from repld import gates, gist_deps

    real = gates.stdin_owned()
    try:
        gates.set_stdin_owned(True)
        assert_true(
            gates.tty_prompt("should never be asked: ") is None,
            "tty_prompt declines while the display thread owns stdin",
        )
        assert_true(
            not gist_deps._can_prompt(),
            "install_deps reports instead of falling into the prompt",
        )
        # Not a blanket disable: boot, which is when the two legitimate callers
        # run, is exactly the window before run_display sets the flag.
        gates.set_stdin_owned(False)
        assert_true(
            gist_deps._can_prompt() == bool(_stdin_is_tty()),
            "with stdin free, the decision is back to whether there is a tty",
        )
    finally:
        gates.set_stdin_owned(real)
    print("  ✓ tty_prompt declines once display owns stdin (no second reader)")


def _stdin_is_tty() -> bool:
    import sys

    stdin = sys.__stdin__
    try:
        return stdin is not None and stdin.isatty()
    except (OSError, ValueError):
        return False


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

        # -- ask, an answer that looks like this command's own flags ----------
        # Every flag parser here used to reach past the gate id: `--socket PATH`
        # is consumed from anywhere in argv by `paths.resolve_socket_path`,
        # `--json` was filtered the same way, and `cli_args.wants_help` scans
        # every argument by design. So this answer came back as "deploy now"
        # with the connection silently repointed at a kernel called `now`.
        task_id = _open_gate(b, 'await ask("Release name?")')
        gate = _pending(kernel)[0]
        ok = _gate_cli(
            kernel, "answer", gate["gate_id"], "deploy", "--socket", "now", "--help"
        )
        assert_eq(ok.returncode, 0, f"answering the ask exits 0 (stderr: {ok.stderr})")
        assert_eq(
            _await_task(b, task_id).strip(),
            "'deploy --socket now --help'",
            "flags after the gate id stay part of the answer",
        )
        # The listing form still parses them, and so does `answer` when they
        # come before the verb — the placement rule the usage text now states.
        listed = _gate_cli(kernel, "--json")
        assert_eq(listed.returncode, 0, "listing still accepts --json")
        assert_eq(json.loads(listed.stdout), [], "…and reports no pending gates")
        print("  ✓ an ask answer that looks like a flag is passed through verbatim")

        # -- unknown id --------------------------------------------------------
        missing = _gate_cli(kernel, "answer", "deadbeef", "y")
        assert_true(missing.returncode != 0, "answering an unknown gate id fails")
        assert_true(
            "deadbeef" in (missing.stderr + missing.stdout),
            "the unknown-gate error names the id",
        )
        print("  ✓ unknown gate id reported, not silently swallowed")

        # -- ask() on a pinned tab ---------------------------------------------
        # The pill has buttons and no text input, so it can never answer an
        # ask. Routing to it anyway suppressed the "repld gate answer" hint
        # *and* drew nothing — on this kernel, a gate with no surface at all.
        b.call(
            "tools/call",
            {
                "name": "exec",
                "arguments": {
                    "code": (
                        "class _FakePinned:\n"
                        "    _pinned = True\n"
                        "    async def _show_gate(self, *a): pass\n"
                        "defer(ask('token?', tab=_FakePinned()), 'pinned-ask')\n"
                    ),
                    "timeout": 5.0,
                },
            },
            timeout=15.0,
        )
        push = b.wait_notification(
            "notifications/claude/channel", kind="awaiting_human", timeout=10
        )
        text = push["params"]["content"]
        assert_true(
            "repld gate answer" in text,
            f"a pinned-tab ask still names its answer command (got {text!r})",
        )
        gate_id = _pending(kernel)[0]["gate_id"]
        answered = _gate_cli(kernel, "answer", gate_id, "sk-123")
        assert_eq(answered.returncode, 0, "and the CLI can actually answer it")
        print("  ✓ ask on a pinned tab keeps its headless answer route")

        _timeout_closes_the_gate(b, kernel)
        _pane_reshows_the_question()
        _pane_drops_a_timed_out_gate()
        _tty_prompt_declines_once_display_owns_stdin()
    finally:
        b.close()


def _timeout_closes_the_gate(b: Bridge, kernel: Kernel) -> None:
    """A gate that expires must *announce* that it did, not just disappear.

    `timeout=` is the documented way to keep a gate from parking a cell
    indefinitely, and it resolves nothing — so `HumanPromptResponse` never
    fires. Every surface that mirrors the open-gate set (the pane, a `repld
    log` replay) learns only from an event, and without one the pane kept a
    phantom that swallowed every subsequent line typed at it.
    """
    task_id = _open_gate(b, 'await ask("Never answered?", timeout=1.5, default="dflt")')
    gate_id = _pending(kernel)[0]["gate_id"]

    assert_eq(_await_task(b, task_id).strip(), "'dflt'", "the default was returned")
    assert_eq(_pending(kernel), [], "an expired gate is no longer pending")

    proc = subprocess.run(
        ["uv", "run", "--project", str(REPO), "repld", "log", "-n", "500", "--json"],
        cwd=str(kernel.cwd),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert_eq(proc.returncode, 0, f"repld log --json exits 0 (stderr: {proc.stderr})")
    closed = [
        json.loads(line)
        for line in proc.stdout.splitlines()
        if line.strip() and '"HumanPromptClosed"' in line
    ]
    mine = [r for r in closed if r.get("gate_id") == gate_id]
    assert_eq(len(mine), 1, f"the expired gate logged exactly one close ({closed!r})")
    assert_eq(mine[0]["reason"], "timeout", "and named the reason it ended")
    print("  ✓ an expired gate emits HumanPromptClosed rather than vanishing")


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


def _pane_drops_a_timed_out_gate() -> None:
    """A gate that expired must leave the pane's registry, same as an answered one.

    It never did: `HumanPromptResponse` is the only thing that removed an entry,
    so an expired gate stayed the newest forever — and `_stdin_reader_loop`
    routes to the newest, resolving a gate the kernel already dropped, which is
    a silent no-op. The pane's stdin went dead until some later gate opened.
    """
    from repld import display
    from repld.events import HumanPromptClosed, HumanPromptOpen

    written: list[str] = []
    real_out, display._out = display._out, written.append
    real_gates = dict(display._open_gates)
    try:
        display._open_gates.clear()
        display._render_prompt_open(HumanPromptOpen("aaa", "confirm", "Deploy?", None))
        display._render_prompt_open(
            HumanPromptOpen("bbb", "ask", "Never answered?", None)
        )
        written.clear()
        display._render_prompt_closed(HumanPromptClosed("bbb", "timeout"))
        out = "".join(written)
        assert_eq(list(display._open_gates), ["aaa"], "the expired gate is dropped")
        assert_true("no response (timeout)" in out, f"and says why (got {out!r})")
        # Same re-ask as the answered path: the older gate is still open and its
        # prompt has scrolled above the line just written.
        assert_true("Deploy?" in out, f"the surviving gate is re-asked (got {out!r})")

        # Idempotent: `repld gate` may already have removed it, and a second
        # close must not re-print or disturb what is still on screen.
        written.clear()
        display._render_prompt_closed(HumanPromptClosed("bbb", "timeout"))
        assert_eq("".join(written), "", "closing an unknown gate writes nothing")
        assert_eq(list(display._open_gates), ["aaa"], "and leaves the rest alone")
    finally:
        display._out = real_out
        display._open_gates.clear()
        display._open_gates.update(real_gates)
    print("  ✓ pane drops an expired gate instead of routing stdin at it forever")
