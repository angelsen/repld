"""Phase 4: Channel notifications — task_done push, notify() from user code,
pre-gate queuing, notify(session=)/claude_sessions() identity targeting."""

import ast
import json
import re
import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path
from queue import Empty

from harness import REPO, Bridge, Kernel, assert_eq, assert_true, content_text


def phase_4_push_kind_args(_kernel: Kernel) -> None:
    """Every `push_kind(content, kind)` call site passes them in that order.

    Both parameters are `str`, so swapping them type-checks, lints, and runs —
    it just sends the agent a push whose content is the word "venv" and whose
    `kind` is a sentence with a filesystem path in it. That shipped in
    `gists._recover_missing_import` and nothing caught it, because the only
    path that reaches it needs a venv appearing mid-session.

    An AST sweep costs nothing and covers every call site, including ones
    written later, which a runtime test of one code path cannot.

    Per-file alias resolution, because matching the bare name `push_kind`
    covered one call site out of eight: `kernel.py` imports it as
    `from .channel import push_kind as _push` and holds seven of them,
    including the loop watchdog's, and every one was invisible to the sweep
    that exists to check them. The `checked` count is asserted against a floor
    for the same reason — a sweep that silently matches nothing passes.
    """
    bad: list[str] = []
    checked = 0
    for f in sorted((REPO / "src" / "repld").rglob("*.py")):
        tree = ast.parse(f.read_text())
        # Local names bound to channel.push_kind in this file.
        names = {"push_kind"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for a in node.names:
                    if a.name == "push_kind" and a.asname:
                        names.add(a.asname)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name not in names or len(node.args) < 2:
                continue
            checked += 1
            kind = node.args[1]
            rel = f.relative_to(REPO)
            # A kind must be a plain snake_case literal. Requiring a literal is
            # the half that matters: the way this goes wrong is the *content*
            # landing in the kind slot, and content is an f-string — so a check
            # that skipped non-constants would skip exactly the bug it exists
            # for. Every kind in the codebase is a literal, so nothing legitimate
            # is caught by insisting on one.
            if not isinstance(kind, ast.Constant) or not isinstance(kind.value, str):
                bad.append(f"{rel}:{node.lineno} kind is not a literal")
            elif not re.fullmatch(r"[a-z][a-z0-9_]*", kind.value):
                bad.append(f"{rel}:{node.lineno} kind={kind.value!r}")
    assert_eq(bad, [], "push_kind(content, kind) — second arg must be a kind")
    assert_true(checked >= 10, f"the sweep found the call sites (got {checked})")
    print(f"  ✓ push_kind arg order: {checked} call site(s) pass a real kind")


def phase_4(kernel: Kernel) -> None:
    """Nudged exec → channel notification arrives with kind=task_done.
    notify() from user code → channel notification with custom meta."""
    b = Bridge(kernel.cwd)
    try:
        b.handshake()
        print("  ✓ initialize + notifications/initialized")

        # Nudge-and-wait-for-channel
        resp = b.exec(
            "import time; time.sleep(0.5); print('slow done')",
            timeout=0.1,
            call_timeout=3.0,
        )
        meta = resp["result"]["_meta"]
        assert_eq(meta["done"], False, "nudge meta.done")
        task_id = meta["task_id"]
        print(f"  ✓ nudged: task_id={task_id}")

        notif = b.wait_notification(
            "notifications/claude/channel", kind="task_done", timeout=5.0
        )
        params = notif["params"]
        nmeta = params["meta"]
        assert_eq(nmeta["kind"], "task_done", "channel meta.kind")
        assert_eq(nmeta["task_id"], task_id, "channel meta.task_id matches")
        assert_eq(nmeta["error"], "0", "channel meta.error=0 for success")
        assert_true(
            "slow done" in params["content"],
            f"channel content contains delta (got {params['content']!r})",
        )
        print(f"  ✓ channel task_done: {params['content'][:60]!r}...")

        # notify() from user code
        resp = b.exec("notify('ping', kind='user', color='blue')", call_timeout=3.0)
        assert_eq(resp["result"]["_meta"]["done"], True, "notify exec done sync")

        notif = b.wait_notification(
            "notifications/claude/channel", kind="user", timeout=3.0
        )
        params = notif["params"]
        assert_eq(params["content"], "ping", "notify content")
        assert_eq(params["meta"]["kind"], "user", "notify meta.kind")
        assert_eq(params["meta"]["color"], "blue", "notify meta.color")
        print(f"  ✓ notify(): content={params['content']!r} meta={params['meta']}")

        # Error in nudged exec → error="1" + traceback content
        resp = b.exec(
            "import time\ntime.sleep(0.3)\nraise RuntimeError('boom')",
            timeout=0.1,
            call_timeout=3.0,
        )
        err_task_id = resp["result"]["_meta"]["task_id"]
        notif = b.wait_notification(
            "notifications/claude/channel", kind="task_done", timeout=5.0
        )
        nmeta = notif["params"]["meta"]
        assert_eq(nmeta["task_id"], err_task_id, "error channel task_id")
        assert_eq(nmeta["error"], "1", "channel error=1 on exception")
        print(
            f"  ✓ error case: error=1, content mentions RuntimeError={('RuntimeError' in notif['params']['content'])}"
        )
    finally:
        b.close()


def phase_4b_pregate(kernel: Kernel) -> None:
    """A channel push produced between initialize and notifications/initialized
    should be queued and arrive once the client completes the handshake."""
    b = Bridge(kernel.cwd)
    try:
        b.call("initialize", {"protocolVersion": "2024-11-05"})
        # Do NOT send notifications/initialized yet.
        # Trigger a channel push while the session is pre-init.
        # Use a nudged exec so the push is guaranteed.
        resp = b.exec(
            "import time; time.sleep(0.3); notify('queued push')",
            timeout=0.1,
            call_timeout=3.0,
        )
        task_id = resp["result"]["_meta"]["task_id"]
        # Wait for the task to finish. Push should now be queued, not delivered.
        time.sleep(0.6)
        # Confirm nothing arrived yet.
        try:
            msg = b.inbox.get(timeout=0.3)
            raise AssertionError(f"channel push arrived before init: {msg}")
        except Empty:
            pass
        # Now send initialized. Queued pushes should flush.
        b.send("notifications/initialized", {}, notif=True)
        # Expect two pushes: one from notify(), one from task_done.
        seen_contents = set()
        for _ in range(2):
            notif = b.wait_notification("notifications/claude/channel", timeout=3.0)
            seen_contents.add(notif["params"]["content"][:40])
        assert_true(
            any("queued push" in c for c in seen_contents),
            f"notify('queued push') delivered after init (got {seen_contents})",
        )
        assert_true(
            any(task_id in c for c in seen_contents),
            f"task_done for {task_id} delivered after init (got {seen_contents})",
        )
        print("  ✓ pre-init channel push queued & flushed on initialized")
    finally:
        b.close()


def phase_4c_claude_sessions(kernel: Kernel) -> None:
    """notify(session=) targets one Claude Code session by id, no broadcast
    fallback on a miss; claude_sessions() lists connected (id, project_dir,
    kind) tuples, with (None, None, None) for a session with no Claude Code
    identity, and kind='bg' when the bridge's own CLAUDE_JOB_DIR env var is
    set (a `claude --bg` worker)."""
    a = Bridge(
        kernel.cwd,
        env={"CLAUDE_CODE_SESSION_ID": "sess-A", "CLAUDE_PROJECT_DIR": "/proj/a"},
    )
    b = Bridge(
        kernel.cwd,
        env={"CLAUDE_CODE_SESSION_ID": "sess-B", "CLAUDE_PROJECT_DIR": "/proj/b"},
    )
    try:
        a.handshake()
        b.handshake()
        print("  ✓ two bridges, distinct CLAUDE_CODE_SESSION_ID")

        # Targeted notify reaches only the named session, and reports delivery.
        resp = b.exec("print(notify('for-A', session='sess-A'))", call_timeout=3.0)
        assert_true(
            "True" in content_text(resp), "notify(session=) returns True on hit"
        )

        notif = a.wait_notification("notifications/claude/channel", timeout=3.0)
        assert_true(
            "for-A" in notif["params"]["content"], "targeted session received it"
        )
        try:
            b.wait_notification("notifications/claude/channel", timeout=1.0)
            raise AssertionError("targeted push leaked to the sender")
        except TimeoutError:
            pass
        print("  ✓ notify(session=) reaches only the named session")

        # A miss returns False — no fallback to broadcast.
        resp = a.exec("print(notify('nowhere', session='sess-NOPE'))", call_timeout=3.0)
        assert_true("False" in content_text(resp), "unknown session id returns False")
        try:
            b.wait_notification("notifications/claude/channel", timeout=1.0)
            raise AssertionError("a miss fell back to broadcast")
        except TimeoutError:
            pass
        print("  ✓ notify(session=<unknown>) returns False, no broadcast fallback")

        # claude_sessions() lists both, with their project dirs.
        resp = a.exec("print(sorted(claude_sessions()))", call_timeout=3.0)
        out = content_text(resp)
        assert_true(
            "sess-A" in out and "/proj/a" in out, f"sess-A listed (got {out!r})"
        )
        assert_true(
            "sess-B" in out and "/proj/b" in out, f"sess-B listed (got {out!r})"
        )
        print(f"  ✓ claude_sessions(): {out.strip()}")
    finally:
        a.close()
        b.close()

    # A bridge with no Claude Code env still connects and shows (None, None) —
    # explicit deletion, since the test process's own env may already carry
    # CLAUDE_CODE_SESSION_ID (e.g. this suite running inside a Claude Code
    # session).
    c = Bridge(
        kernel.cwd,
        env={"CLAUDE_CODE_SESSION_ID": None, "CLAUDE_PROJECT_DIR": None},
    )
    try:
        c.handshake()
        resp = c.exec("print(claude_sessions())", call_timeout=3.0)
        out = content_text(resp)
        assert_true(
            "(None, None, None)" in out,
            f"env-less bridge reports (None, None, None) (got {out!r})",
        )
        print(
            "  ✓ a bridge with no CLAUDE_CODE_SESSION_ID env reports (None, None, None)"
        )
    finally:
        c.close()

    # CLAUDE_JOB_DIR set (a `claude --bg` worker) reports kind='bg'.
    d = Bridge(
        kernel.cwd,
        env={
            "CLAUDE_CODE_SESSION_ID": "sess-D",
            "CLAUDE_PROJECT_DIR": "/proj/d",
            "CLAUDE_JOB_DIR": "/home/x/.claude/jobs/abc123",
        },
    )
    try:
        d.handshake()
        resp = d.exec("print(claude_sessions())", call_timeout=3.0)
        out = content_text(resp)
        assert_true(
            "('sess-D', '/proj/d', 'bg')" in out,
            f"CLAUDE_JOB_DIR reported as kind='bg' (got {out!r})",
        )
        print("  ✓ CLAUDE_JOB_DIR env reports kind='bg'")
    finally:
        d.close()


def phase_4e_exclude_and_current_session_id(kernel: Kernel) -> None:
    """notify(exclude=) skips one session from an otherwise-broadcast push;
    ignored when session= is also given (targeting and excluding the same
    session still delivers). current_session_id() reports the calling
    cell's own id, matching what claude_sessions() lists it as."""
    a = Bridge(
        kernel.cwd,
        env={"CLAUDE_CODE_SESSION_ID": "sess-A", "CLAUDE_PROJECT_DIR": "/proj/a"},
    )
    b = Bridge(
        kernel.cwd,
        env={"CLAUDE_CODE_SESSION_ID": "sess-B", "CLAUDE_PROJECT_DIR": "/proj/b"},
    )
    try:
        a.handshake()
        b.handshake()

        # current_session_id() reports the calling session's own id.
        resp = a.exec("print(current_session_id())", call_timeout=3.0)
        assert_true(
            "sess-A" in content_text(resp), "current_session_id() reports sess-A"
        )
        print("  ✓ current_session_id() reports the calling session")

        # exclude= skips the named session from the broadcast; the other
        # connected session still gets it.
        a.exec("notify('minus-A', exclude='sess-A')", call_timeout=3.0)
        notif = b.wait_notification(
            "notifications/claude/channel",
            timeout=3.0,
            where=lambda n: "minus-A" in n["params"]["content"],
        )
        assert_true(
            "minus-A" in notif["params"]["content"],
            "excluded broadcast still reaches sess-B",
        )
        try:
            a.wait_notification(
                "notifications/claude/channel",
                timeout=1.0,
                where=lambda n: "minus-A" in n["params"]["content"],
            )
            raise AssertionError("excluded session received its own broadcast")
        except TimeoutError:
            pass
        print("  ✓ notify(exclude=) skips the named session, reaches everyone else")

        # exclude= is a no-op when session= is also given -- targeting and
        # excluding the same session still delivers to it.
        resp = a.exec(
            "print(notify('for-B', session='sess-B', exclude='sess-B'))",
            call_timeout=3.0,
        )
        assert_true(
            "True" in content_text(resp),
            "exclude= ignored alongside session=, delivery unaffected",
        )
        notif = b.wait_notification(
            "notifications/claude/channel",
            timeout=3.0,
            where=lambda n: "for-B" in n["params"]["content"],
        )
        assert_true("for-B" in notif["params"]["content"], "targeted push still lands")
        print("  ✓ exclude= ignored when session= is also given")

        # An exclude= id that isn't connected is a no-op -- broadcast unaffected.
        a.exec("notify('plain', exclude='sess-NOPE')", call_timeout=3.0)
        notif = b.wait_notification(
            "notifications/claude/channel",
            timeout=3.0,
            where=lambda n: "plain" in n["params"]["content"],
        )
        assert_true(
            "plain" in notif["params"]["content"], "unknown exclude= is a no-op"
        )
        print("  ✓ exclude=<unknown> is a no-op, broadcast unaffected")
    finally:
        a.close()
        b.close()


def phase_4f_meta_augmenter(kernel: Kernel) -> None:
    """channel.set_meta_augmenter(fn) merges fn()'s dict into every push's
    meta; explicit per-call meta wins on a key collision; a raising augmenter
    drops its own contribution rather than taking the push down; clearing it
    (None) stops the merge."""
    b = Bridge(kernel.cwd)
    try:
        b.handshake()

        b.exec(
            "from repld import channel\n"
            "channel.set_meta_augmenter(lambda: {'roster': 'sess-A:busy'})",
            call_timeout=3.0,
        )
        b.exec("notify('ping', kind='user')", call_timeout=3.0)
        notif = b.wait_notification(
            "notifications/claude/channel", kind="user", timeout=3.0
        )
        assert_eq(
            notif["params"]["meta"]["roster"], "sess-A:busy", "augmenter's key merged"
        )
        print("  ✓ set_meta_augmenter: augmented key present on an unrelated push")

        # Explicit per-call meta wins over the augmenter on a key collision.
        b.exec(
            "from repld import channel\n"
            "channel.set_meta_augmenter(lambda: {'kind': 'from-augmenter'})",
            call_timeout=3.0,
        )
        b.exec("notify('pong', kind='real')", call_timeout=3.0)
        notif = b.wait_notification(
            "notifications/claude/channel",
            timeout=3.0,
            where=lambda n: n["params"]["content"] == "pong",
        )
        assert_eq(
            notif["params"]["meta"]["kind"], "real", "explicit meta wins collision"
        )
        print("  ✓ explicit per-call meta overrides the augmenter on collision")

        # A raising augmenter drops its own contribution, not the push.
        b.exec(
            "from repld import channel\n"
            "def _boom(): raise RuntimeError('nope')\n"
            "channel.set_meta_augmenter(_boom)",
            call_timeout=3.0,
        )
        b.exec("notify('still-here', kind='survives')", call_timeout=3.0)
        notif = b.wait_notification(
            "notifications/claude/channel", kind="survives", timeout=3.0
        )
        assert_eq(notif["params"]["content"], "still-here", "push survives a raise")
        print("  ✓ a raising augmenter drops its contribution, push still lands")

        # Clearing it (None) stops the merge.
        b.exec(
            "from repld import channel\nchannel.set_meta_augmenter(None)",
            call_timeout=3.0,
        )
        b.exec("notify('clean', kind='after-clear')", call_timeout=3.0)
        notif = b.wait_notification(
            "notifications/claude/channel", kind="after-clear", timeout=3.0
        )
        assert_true(
            "roster" not in notif["params"]["meta"],
            f"cleared augmenter stops merging (got {notif['params']['meta']!r})",
        )
        print("  ✓ set_meta_augmenter(None) clears it")
    finally:
        b.close()


def phase_4d_channel_spill(kernel: Kernel) -> None:
    """push_channel spills oversized content to disk (full text recoverable at
    its `[full output: ...]` path) rather than losing it to a flat clip; a
    huge `meta` value still gets the flat clip since it's attribute-shaped."""
    b = Bridge(kernel.cwd)
    try:
        b.handshake()

        b.exec("notify('x' * 10000, kind='big')", call_timeout=3.0)
        notif = b.wait_notification(
            "notifications/claude/channel", kind="big", timeout=3.0
        )
        content = notif["params"]["content"]
        m = re.search(r"\[full output: (.+)\]", content)
        assert_true(
            m is not None, f"spilled content carries a marker (got {content!r})"
        )
        assert m is not None
        spill_path = m.group(1)
        assert_true(Path(spill_path).exists(), f"spill file exists: {spill_path}")
        with open(spill_path) as f:
            full = f.read()
        assert_eq(len(full), 10000, "spill file holds the untruncated content")
        assert_true(full == "x" * 10000, "spill file content matches exactly")
        print(f"  ✓ oversized notify() content spilled to {spill_path}")

        b.exec("notify('short', kind='big', blob='y' * 5000)", call_timeout=3.0)
        notif = b.wait_notification(
            "notifications/claude/channel", kind="big", timeout=3.0
        )
        blob = notif["params"]["meta"]["blob"]
        assert_true(
            len(blob) < 5000 and "chars total" in blob,
            f"oversized meta value is flat-clipped, not spilled (got {len(blob)} chars)",
        )
        print(f"  ✓ oversized meta value clipped in place ({len(blob)} chars)")
    finally:
        b.close()


def phase_4g_owed_push(kernel: Kernel) -> None:
    """A task's completion push is owed to its session id, not its connection:
    queued while no connection carries the id, delivered exactly once to the
    next one that registers it, and straight to a successor already connected
    when the task finishes, including a `bg` session re-hosted with a new inbox
    socket. A connection with no id, or a push past its deadline, reads `failed`."""

    def get_task(b: Bridge, task_id: str) -> dict:
        resp = b.call(
            "tools/call", {"name": "get_task", "arguments": {"task_id": task_id}}
        )
        return resp["result"]["structuredContent"]

    def start(b: Bridge, seconds: float) -> str:
        out = content_text(
            b.exec(
                "import asyncio\n"
                "async def _owed():\n"
                f"    await asyncio.sleep({seconds})\n"
                "    print('owed done')\n"
                "print('tid=' + defer(_owed(), 'owed'))",
                call_timeout=5.0,
            )
        )
        return out.split("tid=", 1)[1].split()[0]

    def wait_done(b: Bridge, task_id: str) -> dict:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            snap = get_task(b, task_id)
            if snap["done"] and snap["push_state"] != "none":
                return snap
            time.sleep(0.2)
        raise AssertionError(f"task {task_id} never finished")

    def is_task(task_id: str):
        return lambda n: n["params"]["meta"].get("task_id") == task_id

    def bridge(session_id: str | None, inbox: Path | None = None) -> Bridge:
        env: dict[str, str | None] = {
            "CLAUDE_CODE_SESSION_ID": session_id,
            "CLAUDE_PROJECT_DIR": None,
            "CLAUDE_JOB_DIR": None,
            "CLAUDE_CODE_MESSAGING_SOCKET": None,
        }
        if inbox is not None:
            env["CLAUDE_JOB_DIR"] = str(root)
            env["CLAUDE_CODE_MESSAGING_SOCKET"] = str(inbox)
        b = Bridge(kernel.cwd, env=env)
        opened.append(b)
        b.handshake()
        return b

    def listen(path: Path) -> list[str]:
        """A stand-in `claude --bg` inbox: collects each line's text."""
        got: list[str] = []
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(path))
        srv.listen(4)
        servers.append(srv)

        def serve() -> None:
            while True:
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return
                with conn:
                    data = b""
                    while chunk := conn.recv(4096):
                        data += chunk
                got.extend(
                    json.loads(ln)["message"]["content"]
                    for ln in data.decode().splitlines()
                )

        threading.Thread(target=serve, daemon=True).start()
        return got

    root = Path(tempfile.mkdtemp(prefix="repld-owed-"))
    (root / "cc-socks").mkdir()
    servers: list[socket.socket] = []
    opened: list[Bridge] = []
    try:
        obs = bridge("owed-obs")
        a = bridge("owed-A")
        tid = start(a, 1.0)
        a.close()
        snap = wait_done(obs, tid)
        assert_eq(snap["push_state"], "queued", "no connection carries the id")
        assert_eq(snap["push_delivered"], None, "push_delivered reads null")
        print("  ✓ a result whose session left reads push_state queued")

        a2 = bridge("owed-A")
        note = a2.wait_notification(
            "notifications/claude/channel", kind="task_done", where=is_task(tid)
        )
        assert_true("owed done" in note["params"]["content"], "the result's output")
        snap = get_task(obs, tid)
        assert_eq(snap["push_state"], "written", "flips to written on delivery")
        assert_eq(snap["push_delivered"], True, "and push_delivered to true")
        print("  ✓ the same id registering from a new connection gets the push")

        a3 = bridge("owed-A")
        for b in (a2, a3, obs):
            try:
                b.wait_notification(
                    "notifications/claude/channel",
                    kind="task_done",
                    timeout=1,
                    where=is_task(tid),
                )
                raise AssertionError("an owed push was delivered twice")
            except TimeoutError:
                pass
        print("  ✓ exactly once: a later registration of the id gets nothing")

        b1 = bridge("owed-B")
        tid = start(b1, 1.5)
        b1.close()
        b2 = bridge("owed-B")
        b2.wait_notification(
            "notifications/claude/channel",
            kind="task_done",
            timeout=8,
            where=is_task(tid),
        )
        assert_eq(wait_done(obs, tid)["push_state"], "written", "written directly")
        print("  ✓ a successor connected before the task finishes gets it directly")

        n = bridge(None)
        tid = start(n, 0.5)
        n.close()
        snap = wait_done(obs, tid)
        assert_eq(snap["push_state"], "failed", "no id, nothing to owe it to")
        assert_eq(snap["push_delivered"], False, "push_delivered false")
        print("  ✓ a connection with no session id still reads failed")

        # The supervise case: a `bg` host exits mid-task and the same id comes
        # back from a new host with its own inbox socket.
        first_inbox, second_inbox = (root / "cc-socks" / f"{n}.sock" for n in (1, 2))
        first = listen(first_inbox)
        second = listen(second_inbox)
        g1 = bridge("owed-bg", first_inbox)
        tid = start(g1, 1.0)
        g1.close()
        assert_eq(wait_done(obs, tid)["push_state"], "queued", "bg result queued")
        bridge("owed-bg", second_inbox)
        deadline = time.monotonic() + 5
        while not second and time.monotonic() < deadline:
            time.sleep(0.1)
        time.sleep(0.5)
        assert_eq(len(second), 1, f"one push to the new host's inbox (got {second})")
        assert_true(
            f'task_id="{tid}"' in second[0] and "owed done" in second[0],
            f"it is that task's result (got {second[0]!r})",
        )
        assert_eq(first, [], "the old host's inbox got nothing")
        assert_eq(get_task(obs, tid)["push_state"], "written", "written via the inbox")
        print("  ✓ a re-hosted bg session gets its result once, at its new inbox")

        # Expiry: the deadline is fixed at push time from tasks._EVICT_AGE.
        obs.exec(
            "from repld import tasks\n_saved_evict = tasks._EVICT_AGE\ntasks._EVICT_AGE = 1.0"
        )
        try:
            e = bridge("owed-E")
            tid = start(e, 0.3)
            e.close()
            assert_eq(wait_done(obs, tid)["push_state"], "queued", "queued first")
        finally:
            obs.exec("from repld import tasks\ntasks._EVICT_AGE = _saved_evict")
        time.sleep(1.2)
        obs.exec("from repld import ipc\nipc.expire_outbox()")
        snap = get_task(obs, tid)
        assert_eq(snap["push_state"], "failed", "expired reads failed")
        assert_eq(snap["push_delivered"], False, "push_delivered false")
        e2 = bridge("owed-E")
        try:
            e2.wait_notification(
                "notifications/claude/channel",
                kind="task_done",
                timeout=1,
                where=is_task(tid),
            )
            raise AssertionError("an expired push was still delivered")
        except TimeoutError:
            pass
        print("  ✓ an owed push past its deadline reads failed and is never delivered")
    finally:
        for b in opened:
            b.close()
        for srv in servers:
            srv.close()
        shutil.rmtree(root, ignore_errors=True)
