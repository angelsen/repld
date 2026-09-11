"""Phase 4: Channel notifications — task_done push, notify() from user code,
pre-gate queuing, notify(session=)/claude_sessions() identity targeting."""

import ast
import re
import time
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
    fallback on a miss; claude_sessions() lists connected (id, project_dir)
    pairs, with (None, None) for a session with no Claude Code identity."""
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
            "(None, None)" in out,
            f"env-less bridge reports (None, None) (got {out!r})",
        )
        print("  ✓ a bridge with no CLAUDE_CODE_SESSION_ID env reports (None, None)")
    finally:
        c.close()
