"""Phase 17: human gates on a headless kernel — `repld gate` list / answer.

The harness kernel runs `--no-display`, which is precisely the case that used to
have no answering surface at all: no pane reading stdin, no pinned tab. So these
assertions run against the configuration that matters.
"""

import http.client
import json
import subprocess
import time
from urllib.parse import urlsplit

from harness import REPO, Bridge, Kernel, assert_eq, assert_true

from repld import paths


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
    resp = b.exec(code, timeout=1.0, call_timeout=15.0)
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
    except OSError, ValueError:
        return False


def phase_17_gates(kernel: Kernel) -> None:
    b = Bridge(kernel.cwd)
    try:
        b.handshake()

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
        assert_eq(
            listed[0]["schema"]["properties"]["answer"]["type"],
            "boolean",
            "a confirm lists its one-field schema",
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
        b.exec(
            "class _FakePinned:\n"
            "    _pinned = True\n"
            "    async def _show_gate(self, *a): pass\n"
            "defer(ask('token?', tab=_FakePinned()), 'pinned-ask')\n",
            timeout=5.0,
            call_timeout=15.0,
        )
        # Every gate above pushed awaiting_human too, and those are still in
        # the stash — an unfiltered wait returns the choose gate's push, which
        # names the command regardless of what this ask did.
        push = b.wait_notification(
            "notifications/claude/channel",
            kind="awaiting_human",
            where=lambda m: "token?" in m["params"]["content"],
            timeout=10,
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

        _elicitation(kernel)
        _timeout_closes_the_gate(b, kernel)
        _dashboard_answers_a_gate(b, kernel)
        _secret_gate(kernel)
        _pane_reshows_the_question()
        _pane_drops_a_timed_out_gate()
        _tty_prompt_declines_once_display_owns_stdin()
    finally:
        b.close()


def _elicit_request(b: Bridge, prompt: str) -> dict:
    """The `elicitation/create` the client received for the gate asking `prompt`."""
    return b.wait_notification(
        "elicitation/create",
        where=lambda m: m["params"]["message"] == prompt,
        timeout=10.0,
    )


def _elicitation(kernel: Kernel) -> None:
    """A client that declares `elicitation` gets the gate as a request; others do not.

    Accept answers, Decline is "no" (`confirm` False, the rest `GateDeclined`),
    Cancel answers nothing, and a gate another surface answers is withdrawn.
    """
    b = Bridge(kernel.cwd)
    try:
        b.handshake(capabilities={"elicitation": {"form": {}}})

        # -- accept, one per kind ---------------------------------------------
        task = _open_gate(b, 'await confirm("Ship it?")')
        req = _elicit_request(b, "Ship it?")
        schema = req["params"]["requestedSchema"]
        assert_eq(schema["properties"]["answer"]["type"], "boolean", "confirm schema")
        b.respond(req["id"], {"action": "accept", "content": {"answer": False}})
        assert_eq(_await_task(b, task).strip(), "False", "accepted answer is the value")

        task = _open_gate(b, 'await choose("Which?", ["alpha", "beta"])')
        req = _elicit_request(b, "Which?")
        assert_eq(
            req["params"]["requestedSchema"]["properties"]["answer"]["enum"],
            ["alpha", "beta"],
            "choose schema carries the options",
        )
        b.respond(req["id"], {"action": "accept", "content": {"answer": "beta"}})
        assert_eq(_await_task(b, task).strip(), "'beta'", "choose answered")

        task = _open_gate(
            b,
            'await form("Deploy where?", {"env": ["prod", "stage"], "count": int,'
            ' "dry": bool})',
        )
        req = _elicit_request(b, "Deploy where?")
        props = req["params"]["requestedSchema"]["properties"]
        assert_eq(
            [props["env"]["type"], props["count"]["type"], props["dry"]["type"]],
            ["string", "integer", "boolean"],
            "form schema maps field types",
        )
        content = {"env": "prod", "count": 3, "dry": True}
        b.respond(req["id"], {"action": "accept", "content": content})
        assert_eq(
            _await_task(b, task).strip(),
            "{'env': 'prod', 'count': 3, 'dry': True}",
            "form returns the dict",
        )
        print("  ✓ elicitation: accept answers confirm, choose and form")

        # -- decline ----------------------------------------------------------
        task = _open_gate(b, 'await confirm("Really?")')
        req = _elicit_request(b, "Really?")
        b.respond(req["id"], {"action": "decline"})
        assert_eq(_await_task(b, task).strip(), "False", "decline is a no")

        task = _open_gate(b, 'await ask("Token?")')
        req = _elicit_request(b, "Token?")
        b.respond(req["id"], {"action": "decline"})
        assert_true(
            "GateDeclined" in _await_task(b, task),
            "declining an ask raises GateDeclined into the cell",
        )
        assert_eq(_pending(kernel), [], "a declined gate is closed")
        print("  ✓ elicitation: decline is False for confirm, GateDeclined otherwise")

        # -- cancel leaves the gate to the other surfaces -----------------------
        task = _open_gate(b, 'await ask("Name?")')
        req = _elicit_request(b, "Name?")
        b.respond(req["id"], {"action": "cancel"})
        time.sleep(1.0)
        gate_id = _pending(kernel)[0]["gate_id"]
        _gate_cli(kernel, "answer", gate_id, "ada")
        assert_eq(_await_task(b, task).strip(), "'ada'", "CLI still answers it")
        print("  ✓ elicitation: cancel answers nothing, the CLI still can")

        # -- another surface wins: the modal is withdrawn -----------------------
        task = _open_gate(b, 'await confirm("Race?")')
        req = _elicit_request(b, "Race?")
        gate_id = _pending(kernel)[0]["gate_id"]
        _gate_cli(kernel, "answer", gate_id, "y")
        cancelled = b.wait_notification(
            "notifications/cancelled",
            where=lambda m: m["params"]["requestId"] == req["id"],
            timeout=10.0,
        )
        assert_true(cancelled is not None, "client is told to close the modal")
        assert_eq(_await_task(b, task).strip(), "True", "the CLI answer won")
        print("  ✓ elicitation: an answer from another surface cancels the request")

        # -- a form answered as JSON from the CLI -------------------------------
        task = _open_gate(b, 'await form("Who?", {"name": str})')
        gate_id = _pending(kernel)[0]["gate_id"]
        bad = _gate_cli(kernel, "answer", gate_id, '{"nme": "x"}')
        assert_true(bad.returncode != 0, "a field the form lacks is refused")
        good = _gate_cli(kernel, "answer", gate_id, '{"name": "ada"}')
        assert_eq(good.returncode, 0, f"JSON answers a form (stderr: {good.stderr})")
        assert_eq(_await_task(b, task).strip(), "{'name': 'ada'}", "form via CLI")
        print("  ✓ elicitation: a form is answerable as JSON from the CLI")

        # -- the answer must not queue behind a blocked reader ------------------
        rid = b.send(
            "tools/call",
            {
                "name": "exec",
                "arguments": {"code": 'await confirm("Inline?")', "timeout": 15},
            },
        )
        req = _elicit_request(b, "Inline?")
        b.respond(req["id"], {"action": "accept", "content": {"answer": True}})
        deadline = time.monotonic() + 8.0
        done = None
        while time.monotonic() < deadline and done is None:
            msg = b.inbox.get(timeout=8.0)
            if msg.get("id") == rid:
                done = msg
        assert_true(
            done is not None and done["result"]["_meta"]["done"] is True,
            "an exec parked in a gate returns inline once elicited",
        )
        print("  ✓ elicitation: the reply passes an exec holding the reader thread")
    finally:
        b.close()

    # -- clients that cannot be asked ------------------------------------------
    nocap = Bridge(kernel.cwd)
    bg = Bridge(
        kernel.cwd,
        env={
            "CLAUDE_JOB_DIR": "/tmp/repld-test-job",
            "CLAUDE_CODE_SESSION_ID": "bg-elicit",
        },
    )
    try:
        nocap.handshake()
        bg.handshake(capabilities={"elicitation": {"form": {}}})
        for client in (nocap, bg):
            _open_gate(client, 'await confirm("Unaskable?")')
            try:
                client.wait_notification("elicitation/create", timeout=1.5)
            except TimeoutError:
                pass
            else:
                raise AssertionError("an unaskable client received a request")
            gate_id = _pending(kernel)[0]["gate_id"]
            _gate_cli(kernel, "answer", gate_id, "y")
        print("  ✓ elicitation: no request without the capability, none to a bg worker")
    finally:
        nocap.close()
        bg.close()


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


def _dashboard_rpc(kernel: Kernel, method: str, params: dict, token: str | None):
    port = json.loads(kernel.lock_path.read_text())["dashboard_port"]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        conn.request(
            "POST",
            "/api",
            json.dumps({"method": method, "params": params, "id": 1}),
            headers,
        )
        resp = conn.getresponse()
        return resp.status, resp.read().decode()
    finally:
        conn.close()


def _dashboard_answers_a_gate(b: Bridge, kernel: Kernel) -> None:
    """The dashboard is a fourth `resolve_gate` caller: Bearer-only, same coercion."""
    token = json.loads(paths.hint_for(paths.socket_path(kernel.cwd)).read_text())[
        "token"
    ]
    task_id = _open_gate(
        b,
        '_seen = []\nawait confirm("Allow the click?", on_open=_seen.append)',
    )
    gate_id = _pending(kernel)[0]["gate_id"]
    seen = b.exec("print(_seen)", timeout=5.0)["result"]["content"][0]["text"]
    assert_true(
        gate_id in seen, f"on_open handed the caller the pending gate's id ({seen!r})"
    )

    status, _ = _dashboard_rpc(kernel, "gates", {}, None)
    assert_eq(status, 401, "the gates RPC needs the token")
    status, body = _dashboard_rpc(kernel, "gates", {}, token)
    listed = json.loads(body)["result"]
    assert_eq([g["gate_id"] for g in listed], [gate_id], "gates lists the pending one")

    _, body = _dashboard_rpc(
        kernel, "gate_answer", {"gate_id": gate_id, "value": "maybe"}, token
    )
    assert_true("error" in json.loads(body), "a confirm rejects a non-y/n answer")
    assert_eq(len(_pending(kernel)), 1, "and stays open")

    _, body = _dashboard_rpc(
        kernel, "gate_answer", {"gate_id": gate_id, "value": "y"}, token
    )
    assert_eq(json.loads(body)["result"]["value"], True, "y resolves it to True")
    assert_eq(_await_task(b, task_id).strip(), "True", "the awaiting cell got True")

    _, body = _dashboard_rpc(
        kernel, "gate_answer", {"gate_id": gate_id, "value": "y"}, token
    )
    assert_true(
        "error" in json.loads(body), "an answered gate cannot be answered twice"
    )
    print("  ✓ dashboard gates / gate_answer: Bearer-only, same coercion, first wins")


def _secret_page(kernel: Kernel, method: str, url: str, body: str | None = None):
    """One request to a `/secret/...` URL, form-encoded for a POST: `(status, text)`."""
    parts = urlsplit(url)
    conn = http.client.HTTPConnection("127.0.0.1", parts.port, timeout=10)
    try:
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        conn.request(method, parts.path + "?" + parts.query, body, headers)
        resp = conn.getresponse()
        return resp.status, resp.read().decode()
    finally:
        conn.close()


def _secret_gate(kernel: Kernel) -> None:
    """`ask(secret=True)` goes to a URL-capable client as a URL elicitation.

    The value arrives on the dashboard page, gated by the gate's own nonce; it
    never reaches the CLI route, the elicitation reply or the event log.
    """
    b = Bridge(kernel.cwd)
    try:
        b.handshake(capabilities={"elicitation": {"form": {}, "url": {}}})
        task = _open_gate(
            b, '_s = await ask("Vault password?", secret=True)\n_s[::-1] == "2retnuh"'
        )
        req = _elicit_request(b, "Vault password?")
        params = req["params"]
        assert_eq(params["mode"], "url", "a secret gate asks in URL mode")
        assert_true("requestedSchema" not in params, "and carries no form schema")
        assert_eq(params["elicitationId"], req["id"], "elicitationId names the request")
        url = params["url"]
        gate = _pending(kernel)[0]
        assert_eq(gate["secret"], True, "the gate lists as secret")

        refused = _gate_cli(kernel, "answer", gate["gate_id"], "hunter2")
        assert_true(refused.returncode != 0, "the CLI refuses a secret answer")
        assert_true("hunter2" not in refused.stderr + refused.stdout, "and echoes none")

        b.respond(req["id"], {"action": "accept"})
        time.sleep(0.5)
        assert_eq(len(_pending(kernel)), 1, "accept only opens the page")

        status, _ = _secret_page(kernel, "GET", url.split("?")[0] + "?n=wrong")
        assert_eq(status, 404, "a wrong nonce gets no form")
        status, _ = _secret_page(kernel, "GET", url.split("?")[0] + "?n=%C3%A9")
        assert_eq(status, 404, "a non-ASCII nonce is a mismatch, not a 500")
        status, page = _secret_page(kernel, "GET", url)
        assert_true(status == 200 and "Vault password?" in page, "the form renders")
        status, _ = _secret_page(kernel, "POST", url, "n=wrong&value=hunter2")
        assert_eq(status, 404, "a wrong nonce cannot answer")
        assert_eq(len(_pending(kernel)), 1, "and leaves the gate open")

        status, _ = _secret_page(
            kernel, "POST", url, "n=" + url.split("n=")[1] + "&value=hunter2"
        )
        assert_eq(status, 200, "the right nonce answers")
        assert_eq(_await_task(b, task).strip(), "True", "the cell gets the value")
        done = b.wait_notification(
            "notifications/elicitation/complete",
            where=lambda m: m["params"]["elicitationId"] == req["id"],
            timeout=10.0,
        )
        assert_true(done is not None, "the client is told the page was submitted")
        status, _ = _secret_page(
            kernel, "POST", url, "n=" + url.split("n=")[1] + "&value=x"
        )
        assert_eq(status, 404, "an answered gate takes no second answer")
        log = paths.eventlog_for(paths.socket_path(kernel.cwd)).read_text()
        assert_true("hunter2" not in log, "the event log never holds the secret")
        assert_true('"secret": true' in log, "the open event marks the gate secret")

        task = _open_gate(b, 'await ask("Declined?", secret=True)')
        req = _elicit_request(b, "Declined?")
        b.respond(req["id"], {"action": "decline"})
        assert_true(
            "GateDeclined" in _await_task(b, task), "decline raises into the cell"
        )
        print("  ✓ ask(secret=True): URL elicitation, nonce-gated page, redacted log")
    finally:
        b.close()

    # A form-only client is never sent a secret as a form.
    b = Bridge(kernel.cwd)
    try:
        b.handshake(capabilities={"elicitation": {"form": {}}})
        task = _open_gate(b, '_s = await ask("Form only?", secret=True)\n_s == "s3"')
        push = b.wait_notification(
            "notifications/claude/channel",
            kind="awaiting_human",
            where=lambda m: "Form only?" in m["params"]["content"],
            timeout=10,
        )
        text = push["params"]["content"]
        assert_true("/secret/" in text, "the push carries the page URL instead")
        assert_true("repld gate answer" not in text, "and no CLI route")
        url = text.split("open ")[1].split()[0]
        status, _ = _secret_page(
            kernel, "POST", url, "n=" + url.split("n=")[1] + "&value=s3"
        )
        assert_eq(status, 200, "the page answers it")
        assert_eq(_await_task(b, task).strip(), "True", "the cell gets the value")
        print("  ✓ ask(secret=True): a form-only client gets the URL in the push")
    finally:
        b.close()
