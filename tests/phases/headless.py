"""Phase 15: headless kernel — slim loader, self-healing, targeted push, event log.

Everything here runs in its own tempdir with no kernel started up front: the
point is that the bridge produces one.
"""

import contextlib
import io
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from harness import (
    REPO,
    Bridge,
    Kernel,
    assert_eq,
    assert_true,
    content_text,
    lock_path_for,
)

from repld import core_schemas


def _handshake(b: Bridge) -> dict:
    return b.handshake(timeout=30)


def _lock(cwd: Path) -> dict:
    return json.loads(lock_path_for(cwd).read_text())


def _wait_lock(cwd: Path, timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return _lock(cwd)
        except (OSError, json.JSONDecodeError):
            time.sleep(0.1)
    raise AssertionError(f"no kernel lockfile appeared under {lock_path_for(cwd)}")


def _stop_kernel(cwd: Path) -> None:
    try:
        pid = int(_lock(cwd)["pid"])
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)


def _autospawn_and_heal(tmp: Path) -> None:
    """Bridge with no kernel defers spawn until real use; killing it heals on the next call."""
    b = Bridge(tmp)
    try:
        _handshake(b)
        assert_true(
            not lock_path_for(tmp).exists(),
            "handshake alone did not spawn a kernel (discovery served lazily)",
        )
        print("  ✓ initialize + notifications/initialized spawned no kernel")

        b.exec("SENTINEL = 1")
        lock = _wait_lock(tmp)
        first_pid = lock["pid"]
        print(f"  ✓ first real tool call spawned a headless kernel (pid {first_pid})")

        # Kill it with a request in flight: the bridge owes the client a reply
        # the dead kernel will never send.
        rid = b.send(
            "tools/call",
            {
                "name": "exec",
                "arguments": {
                    "code": "import asyncio\nawait asyncio.sleep(30)",
                    "timeout": 25,
                },
            },
        )
        time.sleep(1.0)
        os.kill(int(first_pid), signal.SIGKILL)

        deadline = time.monotonic() + 15
        orphan: dict = {}
        while time.monotonic() < deadline:
            msg = b.inbox.get(timeout=max(0.1, deadline - time.monotonic()))
            if msg.get("id") == rid:
                orphan = msg
                break
        assert_true(orphan, "in-flight request got a reply after kernel death")
        assert_eq(orphan["error"]["code"], -31001, "orphaned request error code")
        print("  ✓ in-flight request answered with -31001, not left hanging")

        assert_true(b.proc.poll() is None, "bridge survives kernel death")

        # Next request heals: fresh kernel, replayed handshake, working tools.
        resp = b.exec("print('healed')", call_timeout=40)
        text = content_text(resp)
        assert_true("healed" in text, f"exec works after respawn (got {text!r})")
        second_pid = _wait_lock(tmp)["pid"]
        assert_true(second_pid != first_pid, "a new kernel pid took over")
        print(f"  ✓ next request healed onto a fresh kernel (pid {second_pid})")

        # State is genuinely new — proves we're talking to a different process.
        resp = b.exec("print('SENTINEL' in dir())")
        assert_true(
            "False" in content_text(resp),
            "respawned kernel has a fresh namespace",
        )

        # Channel push still lands, which only works if the handshake replay
        # marked the new kernel's session initialized.
        b.exec("notify('after respawn')")
        b.wait_notification("notifications/claude/channel", timeout=10)
        print("  ✓ channel push arrives after respawn (handshake replayed)")
    finally:
        b.close()


def _bridge_served_tools(tmp: Path) -> None:
    """repld_restart is advertised by the kernel but answered by the bridge.

    The split matters: a kernel cannot reply to "restart yourself" — the
    response would still be in flight when the process goes away, so the client
    would get -31001 for something that actually worked.
    """
    b = Bridge(tmp)
    try:
        _handshake(b)
        first_pid = _wait_lock(tmp)["pid"]

        listed = b.call("tools/list", {}, timeout=30)["result"]["tools"]
        names = [t["name"] for t in listed]
        assert_true("repld_restart" in names, f"bridge tool advertised (got {names})")
        assert_true("exec" in names, "kernel tools still listed alongside it")

        b.exec("SENTINEL = 1")

        resp = b.call(
            "tools/call", {"name": "repld_restart", "arguments": {}}, timeout=60
        )
        assert_true("error" not in resp, f"answered in-band, not orphaned: {resp}")
        assert_eq(resp["result"].get("isError"), False, "restart reported success")
        meta = resp["result"].get("_meta", {})
        assert_eq(str(meta.get("old_pid")), str(first_pid), "result names the old pid")
        assert_true(
            meta.get("new_pid") and meta["new_pid"] != meta.get("old_pid"),
            f"a different kernel took over (got {meta})",
        )
        print("  ✓ bridge tool answered in-band (no -31001), kernel replaced")

        resp = b.exec("print('SENTINEL' in dir())", call_timeout=40)
        assert_true(
            "False" in content_text(resp),
            "restarted kernel has a fresh namespace",
        )
        b.exec("notify('after restart')")
        b.wait_notification("notifications/claude/channel", timeout=10)
        print("  ✓ session survived the restart: handshake replayed, push lands")

        # The guard that keeps the above honest when the drain is slow. A
        # SIGTERMed kernel holds its pid and its bound socket for as long as
        # `_shutdown` takes, so `_reconnect` used to hand the dying process
        # back and report `pid N → N`. A live kernel is indistinguishable from
        # a draining one here, which is what makes it testable.
        from repld import bridge as _bridge
        from repld import paths

        live_pid = int(_lock(tmp)["pid"])
        probe = _bridge.Bridge(paths.socket_path(tmp))
        refused = probe._connect_excluding(live_pid)
        assert_true(
            isinstance(refused, str) and str(live_pid) in refused,
            f"the kernel being replaced is refused, not reattached (got {refused!r})",
        )
        accepted = probe._connect_excluding(None)
        assert_true(
            not isinstance(accepted, str), "the same kernel attaches unexcluded"
        )
        if not isinstance(accepted, str):  # narrowing: assert_true isn't a TypeGuard
            accepted[0].close()
        print("  ✓ restart refuses to reattach to the kernel it is replacing")
    finally:
        b.close()


def _lazy_discovery_from_cache(tmp: Path) -> None:
    """No kernel running: discovery is served from `kernel.cache`, not by
    spawning one. A real `tools/call` still spawns and reconciles via
    `notifications/tools/list_changed`."""
    from repld import paths

    _stop_kernel(tmp)
    cache_path = paths.cache_for(paths.socket_path(tmp))
    assert_true(
        cache_path.exists(), f"kernel.cache persisted after shutdown ({cache_path})"
    )
    cached_names = {t["name"] for t in json.loads(cache_path.read_text())["tools"]}
    assert_true(
        {"exec", "get_task", "cancel"} <= cached_names,
        f"cache carries the full tool list (got {sorted(cached_names)})",
    )

    b = Bridge(tmp)
    try:
        init = _handshake(b)
        assert_true(
            not lock_path_for(tmp).exists(), "handshake alone did not spawn a kernel"
        )
        # The other half of the drift guard in phase 3. This `initialize` is
        # answered by the bridge with no kernel in existence, and it is the one
        # the client actually negotiates against for the whole session — a
        # capability the kernel declares but this path forgets would be
        # unusable in every cold-started session.
        assert_eq(
            init["result"]["capabilities"],
            core_schemas.CAPABILITIES,
            "kernel-less initialize negotiates the same capabilities",
        )

        listed = b.call("tools/list", {}, timeout=10)["result"]["tools"]
        assert_eq(
            {t["name"] for t in listed}, cached_names, "tools/list served from cache"
        )
        assert_true(
            not lock_path_for(tmp).exists(), "tools/list did not spawn a kernel"
        )

        resp = b.call("resources/read", {"uri": "repld://docs/guide"}, timeout=10)
        text = resp["result"]["contents"][0]["text"]
        assert_true(len(text) > 1000, f"static doc served in full ({len(text)} bytes)")
        assert_true(
            not lock_path_for(tmp).exists(), "doc resource read did not spawn a kernel"
        )
        print("  ✓ tools/list + resources/read(docs) served from cache, no spawn")

        # Neither a liveness probe nor a genuinely unknown method may cost a
        # kernel. `ping` gets a real `{}` from the bridge itself (the bridge is
        # the thing being pinged); the unknown method keeps the -32601 guard —
        # the old fall-through to _ensure_kernel meant either one silently
        # spawned a kernel and paid the full 5s spawn-and-wait.
        resp = b.call("ping", {}, timeout=10)
        assert_eq(resp["result"], {}, "ping answered {} by the bridge, no kernel")
        assert_true(not lock_path_for(tmp).exists(), "ping did not spawn a kernel")
        resp = b.call("prompts/list", {}, timeout=10)
        assert_eq(
            resp["error"]["code"], -32601, "unknown method answered by the bridge"
        )
        assert_true(
            not lock_path_for(tmp).exists(), "unknown method did not spawn a kernel"
        )
        print("  ✓ ping + unknown method answered without spawning a kernel")

        resp = b.exec("print('post-cache spawn')", call_timeout=40)
        assert_true(
            "post-cache spawn" in content_text(resp),
            "exec worked after the lazy spawn",
        )
        lock = _wait_lock(tmp)
        b.wait_notification("notifications/tools/list_changed", timeout=10)
        print(
            f"  ✓ first real tools/call spawned a kernel (pid {lock['pid']}) "
            "and fired list_changed"
        )
    finally:
        b.close()


def _version_mismatch_cache_discarded(tmp: Path) -> None:
    """A cache written by a different repld version is ignored, not trusted."""
    from repld import paths

    _stop_kernel(tmp)
    cache_path = paths.cache_for(paths.socket_path(tmp))
    cached = json.loads(cache_path.read_text())
    cached["version"] = "0.0.0-does-not-exist"
    cached["tools"] = [
        {
            "name": "bogus_tool_from_old_version",
            "description": "x",
            "inputSchema": {"type": "object", "properties": {}},
        }
    ]
    cache_path.write_text(json.dumps(cached))

    b = Bridge(tmp)
    try:
        _handshake(b)
        listed = b.call("tools/list", {}, timeout=10)["result"]["tools"]
        names = {t["name"] for t in listed}
        assert_true(
            "bogus_tool_from_old_version" not in names,
            f"version-mismatched cache was discarded (got {sorted(names)})",
        )
        assert_true(
            {"exec", "get_task", "cancel"} <= names,
            f"fell back to the static core tool set (got {sorted(names)})",
        )
        print(
            "  ✓ cache from a different repld version discarded, static fallback used"
        )

        # Leave a live, correctly-versioned kernel behind for later helpers.
        b.exec("SENTINEL2 = 1", call_timeout=40)
        _wait_lock(tmp)
    finally:
        b.close()


def _bridge_tool_bypass_error(tmp: Path) -> None:
    """A client on the socket directly gets a named error, not 'unknown tool'.

    The kernel advertises these tools, so falling through to the gist-tool
    lookup would report a name that is right there in tools/list as unknown.
    """
    from repld import ipc

    result = ipc.connect_to_kernel(lock_path_for(tmp))
    if isinstance(result, str):  # str is the failure channel, not a socket
        # Harness precondition, not a caller type contract — AssertionError
        # is what the test runner reports as a failure.
        raise AssertionError(f"could not connect to the kernel: {result}")  # noqa: TRY004
    sock, _ = result
    try:
        req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "repld_restart", "arguments": {}},
        }
        sock.sendall((json.dumps(req) + "\n").encode("utf-8"))
        msg = json.loads(sock.makefile("r", encoding="utf-8").readline())
        assert_true("error" in msg, f"kernel refuses to serve a bridge tool: {msg}")
        assert_true(
            "bridge" in msg["error"]["message"],
            f"error says why (got {msg['error']['message']!r})",
        )
    finally:
        sock.close()
    print("  ✓ bridge tool reaching the kernel directly gets a named error")


def _inflight_never_stranded(tmp: Path) -> None:
    """A kernel dying mid-dispatch answers the request instead of stranding it.

    `_handle_client_line` registers the id *before* writing, so a send that
    fails is answered by `_on_kernel_gone`. But the socket can also go to None
    between the liveness probe and the write — the reader thread hitting EOF
    concurrently — and that path used to `return` with the id already in
    `_inflight`: nothing would ever answer it, so the client hung and
    `_drain_inflight` burned its whole timeout at shutdown.

    Driven in-process; the race isn't reproducible against a live subprocess.
    """
    from repld.bridge import Bridge as _B

    b = _B(tmp / "nonexistent.sock")
    sent: list[dict] = []
    b._to_client = sent.append  # pyright: ignore[reportAttributeAccessIssue]
    b._try_attach_existing = lambda: False
    b._ensure_kernel = lambda: True
    b._sock = None  # ...but it vanished right after the probe

    # A real tools/call, not a discovery method: those are answered from cache
    # without ever reaching _ensure_kernel, which would sidestep the race this
    # test exists to cover.
    b._handle_client_line(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": "exec", "arguments": {"code": "1"}},
            }
        )
        + "\n"
    )
    assert_eq(len(sent), 1, f"the request got an answer (got {sent})")
    assert_eq(sent[0]["id"], 7, "answer carries the client's id")
    assert_eq(sent[0]["error"]["code"], -31001, "answered as a lost request")
    assert_eq(b._inflight, set(), "and the id isn't stranded in _inflight")
    print("  ✓ request registered just as the kernel died is answered, not stranded")


def _targeted_push(tmp: Path) -> None:
    """A task's completion notifies the session that started it, and only it."""
    a = Bridge(tmp)
    c = Bridge(tmp)
    try:
        _handshake(a)
        _handshake(c)

        # Deferred exec from A: exceeds its own timeout, completes later.
        resp = a.exec(
            "import asyncio\nawait asyncio.sleep(1.5)\nprint('from A')",
            timeout=0.3,
            call_timeout=10,
        )
        assert_eq(resp["result"]["_meta"]["done"], False, "exec deferred")

        task_id = resp["result"]["_meta"]["task_id"]

        note = a.wait_notification(
            "notifications/claude/channel", kind="task_done", timeout=15
        )
        assert_true("from A" in note["params"]["content"], "originator got its output")
        print("  ✓ deferred exec notified the session that started it")

        snap = _get_task(a, task_id)
        assert_eq(snap["push_delivered"], True, "delivered push recorded")

        # The originator leaves before its task finishes: the push is dropped,
        # and get_task says so to whoever looks next.
        resp = c.exec(
            "import asyncio\nawait asyncio.sleep(1.5)\nprint('from C')",
            timeout=0.3,
            call_timeout=10,
        )
        orphan_id = resp["result"]["_meta"]["task_id"]
        c.close()
        time.sleep(2.5)
        snap = _get_task(a, orphan_id)
        assert_eq(snap["done"], True, "orphaned task finished")
        assert_eq(snap["push_delivered"], False, "dropped push recorded")
        print("  ✓ get_task's push_delivered: true when seen, false when dropped")

        try:
            c.wait_notification(
                "notifications/claude/channel", kind="task_done", timeout=2
            )
            raise AssertionError("task_done leaked to the other session")
        except TimeoutError:
            pass
        print("  ✓ the other session saw nothing (no broadcast fallback)")

        # Ambient notify() is genuinely shared state — it still reaches both.
        c = Bridge(tmp)
        _handshake(c)
        c.exec("notify('ambient')")
        for name, b in (("A", a), ("C", c)):
            note = b.wait_notification("notifications/claude/channel", timeout=10)
            assert_true(
                "ambient" in note["params"]["content"],
                f"session {name} received the ambient notify()",
            )
        print("  ✓ bare notify() still broadcasts to every session")
    finally:
        a.close()
        c.close()


def _get_task(b: Bridge, task_id: str) -> dict:
    resp = b.call("tools/call", {"name": "get_task", "arguments": {"task_id": task_id}})
    return resp["result"]["structuredContent"]


def _session_rebind(tmp: Path) -> None:
    """`ipc.rebind_claude_session` re-keys a live connection after `/clear`,
    matched by process tree, and the bridge keeps the new id across a respawn."""
    a = Bridge(tmp, env={"CLAUDE_CODE_SESSION_ID": "gen-1", "CLAUDE_PROJECT_DIR": None})
    c = Bridge(tmp, env={"CLAUDE_CODE_SESSION_ID": "other", "CLAUDE_PROJECT_DIR": None})
    try:
        _handshake(a)
        _handshake(c)

        def rebind(new_id: str, pid: int) -> str:
            code = (
                "from repld import ipc\n"
                "try:\n"
                f"    print('old=', ipc.rebind_claude_session({new_id!r}, {pid}))\n"
                "except (LookupError, ValueError) as e:\n"
                "    print(type(e).__name__, e)"
            )
            return content_text(a.exec(code, call_timeout=40))

        # This process is an ancestor of both bridges.
        out = rebind("gen-2", os.getpid())
        assert_true("ValueError" in out, f"shared ancestor is ambiguous (got {out!r})")
        out = rebind("gen-2", 1)
        assert_true(
            "LookupError" in out, f"unrelated pid matches nothing (got {out!r})"
        )
        print("  ✓ rebind refuses an ambiguous or unrelated pid")

        # Started under gen-1, finishes under gen-2 — the /clear-then-continue case.
        out = content_text(
            a.exec(
                "import asyncio\n"
                "async def _pre_clear():\n"
                "    await asyncio.sleep(2)\n"
                "    print('pre-clear done')\n"
                "print('tid=' + defer(_pre_clear(), 'pre-clear'))"
            )
        )
        pre_clear_id = out.split("tid=", 1)[1].split()[0]

        out = rebind("gen-2", a.proc.pid)
        assert_true(
            "old= gen-1" in out, f"rebind returns the replaced id (got {out!r})"
        )
        out = content_text(
            a.exec(
                "print(current_session_id(), sorted(s for s, _, _ in claude_sessions()),"
                " notify('x', session='gen-1'))"
            )
        )
        assert_true(
            "gen-2 ['gen-2', 'other'] False" in out,
            f"new id live, old id gone (got {out!r})",
        )
        print("  ✓ rebind re-keys the matching session; the old id no longer targets")

        note = a.wait_notification(
            "notifications/claude/channel",
            kind="task_done",
            timeout=10,
            where=lambda n: n["params"]["meta"].get("task_id") == pre_clear_id,
        )
        assert_true(
            "pre-clear done" in note["params"]["content"],
            "pre-rebind defer() pushed to the same connection",
        )
        assert_eq(
            _get_task(a, pre_clear_id)["push_delivered"],
            True,
            "pre-rebind defer() recorded as delivered",
        )
        try:
            c.wait_notification(
                "notifications/claude/channel",
                kind="task_done",
                timeout=1,
                where=lambda n: n["params"]["meta"].get("task_id") == pre_clear_id,
            )
            raise AssertionError("pre-rebind task_done leaked to another session")
        except TimeoutError:
            pass
        print("  ✓ defer() started before the rebind still pushes to its connection")

        try:
            a.wait_notification(core_schemas.BRIDGE_REBIND_METHOD, timeout=1)
            raise AssertionError("rebind notification leaked to the MCP client")
        except TimeoutError:
            pass

        os.kill(int(_lock(tmp)["pid"]), signal.SIGKILL)
        time.sleep(0.5)
        out = content_text(a.exec("print(current_session_id())", call_timeout=40))
        assert_true("gen-2" in out, f"respawned kernel sees the new id (got {out!r})")
        print("  ✓ bridge replays the rebound id onto a fresh kernel, not its env's")
    finally:
        a.close()
        c.close()


def _project_path(cwd: Path) -> Path:
    from repld import paths

    return paths.project_path(cwd)


def _project_flags(tmp: Path) -> None:
    """`--project-git` puts a worktree session on the main checkout's kernel,
    spawned from the main checkout; `--project DIR` and REPLD_PROJECT_GIT reach
    the same kernel from anywhere, and conflicting or bogus targets refuse."""
    main = (tmp / "proj-main").resolve()
    main.mkdir()
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*git, "init", "-q", str(main)], check=True)
    subprocess.run(
        [*git, "-C", str(main), "commit", "-q", "--allow-empty", "-m", "i"], check=True
    )
    wt = main / ".claude" / "worktrees" / "w1"
    subprocess.run(
        [*git, "-C", str(main), "worktree", "add", "-q", str(wt)], check=True
    )

    def repld(*args: str, cwd: Path, env: dict[str, str] | None = None):
        return subprocess.run(
            ["uv", "run", "--project", str(REPO), "repld", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env={**os.environ, **(env or {})},
        )

    b = Bridge(wt, global_args=("--project-git",))
    try:
        _handshake(b)
        out = content_text(b.exec("import os\nprint(os.getcwd())", call_timeout=40))
        assert_true(
            str(main) in out, f"kernel spawned from the main checkout (got {out!r})"
        )
        assert_true(lock_path_for(main).exists(), "kernel keyed on the main checkout")
        assert_true(not lock_path_for(wt).exists(), "no per-worktree kernel")
        print(
            "  ✓ --project-git: a worktree bridge spawns and joins the main checkout's kernel"
        )

        pid = str(_lock(main)["pid"])
        for label, res in (
            ("--project-git", repld("--project-git", "status", "--json", cwd=wt)),
            (
                "REPLD_PROJECT_GIT=1",
                repld("status", "--json", cwd=wt, env={"REPLD_PROJECT_GIT": "1"}),
            ),
            (
                "--project DIR",
                repld("--project", str(main), "status", "--json", cwd=tmp),
            ),
        ):
            assert_eq(
                res.returncode, 0, f"{label} status exits 0 ({res.stderr.strip()})"
            )
            assert_true(pid in res.stdout, f"{label} status reports pid {pid}")
        print(
            "  ✓ --project DIR, --project-git and REPLD_PROJECT_GIT reach the same kernel"
        )

        res = repld("--project", str(main), "restart", cwd=tmp)
        assert_eq(
            res.returncode, 0, f"--project restart exits 0 ({res.stderr.strip()})"
        )
        assert_true(
            str(_lock(main)["pid"]) != pid, "--project restart replaced the kernel"
        )
        print("  ✓ --project DIR restart works from another cwd")

        empty = tmp / "proj-empty"
        empty.mkdir()
        res = repld("--project", str(empty), "status", "--json", cwd=tmp)
        assert_eq(res.returncode, 0, "status on a kernel-less project exits 0")
        assert_true(
            not _project_path(empty).exists(),
            "status on a kernel-less project leaves no runtime dir behind",
        )
        print("  ✓ a read-only status creates no project runtime dir")

        for label, res, needle in (
            (
                "not a repo",
                repld("--project-git", "status", cwd=Path("/")),
                "not in a git repository",
            ),
            (
                "missing dir",
                repld("--project", str(tmp / "nope"), "status", cwd=tmp),
                "not a directory",
            ),
            (
                "both flags",
                repld("--project", str(main), "--project-git", "status", cwd=wt),
                "mutually exclusive",
            ),
            (
                "with --socket",
                repld("--project-git", "status", "--socket", "/x", cwd=wt),
                "mutually exclusive",
            ),
        ):
            assert_true(res.returncode != 0, f"{label}: refused")
            assert_true(
                needle in res.stderr, f"{label}: says {needle!r} (got {res.stderr!r})"
            )
        print(
            "  ✓ outside a repo, a missing dir, both flags, or with --socket: refused"
        )
    finally:
        b.close()
        _stop_kernel(main)


def _no_display_skips_queue(tmp: Path) -> None:
    """A headless kernel never builds the display queue in the first place.

    Guards the invariant, not the optimisation: with no TUI, `eventlog`'s sink
    is the sole consumer, and an initialized-but-unread queue would pin at
    _MAXSIZE and put every later emit() on the drop-oldest path. Nothing else
    in the suite fails if someone restores an unconditional
    init_event_queue() — the events still reach the log either way.
    """
    b = Bridge(tmp)
    try:
        _handshake(b)
        resp = b.exec(
            "import repld.events as _e\n"
            "print(f'{_e._disabled}:{_e._queue is None}:{len(_e._pre_init_buf)}')",
        )
        out = content_text(resp)
        assert_true(
            "True:True:0" in out,
            f"headless kernel disabled the queue and dropped the pre-init buffer "
            f"(got {out!r})",
        )
        print("  ✓ headless kernel skips the display queue entirely")
    finally:
        b.close()


def _event_log(tmp: Path) -> None:
    """The headless kernel writes an event log and `repld log` reads it back."""
    out = subprocess.run(
        ["uv", "run", "--project", str(REPO), "repld", "log", "-n", "500"],
        cwd=str(tmp),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert_eq(out.returncode, 0, "repld log exits 0")
    assert_true(
        "ambient" in out.stdout, f"repld log shows activity (got {out.stdout[-400:]!r})"
    )
    print("  ✓ repld log replayed the headless kernel's activity")

    out = subprocess.run(
        ["uv", "run", "--project", str(REPO), "repld", "status", "--json"],
        cwd=str(tmp),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert_eq(out.returncode, 0, "repld status exits 0")
    status = json.loads(out.stdout)
    assert_true(status["kernel"] is not None, "repld status found the kernel")
    print("  ✓ repld status reported a kernel this terminal never started")

    # tasks_active comes from the dashboard API, authenticated with the token in
    # the hint file. That file used to be written only by browser code, so on a
    # kernel that never touched the browser these keys were silently absent.
    kernel = status["kernel"]
    assert_true(kernel.get("dashboard_port"), "status carries the dashboard port")
    assert_true(
        kernel.get("tasks_active") is not None,
        f"status queried the dashboard for live counts (got {kernel!r})",
    )
    assert_true("tickers" in kernel, "status carries the ticker count")
    print("  ✓ repld status read live task/ticker counts without the browser")


def _tasks_listing(tmp: Path) -> None:
    """`repld tasks` lists an in-flight defer() and an active @every ticker."""
    b = Bridge(tmp)
    try:
        b.handshake()
        b.exec(
            "import asyncio\n"
            "async def _slow():\n"
            "    await asyncio.sleep(5)\n"
            "tid = defer(_slow(), label='test-tasks')\n"
        )
        b.exec("@every(60)\ndef _test_ticker():\n    return 'tick'\n")

        out = subprocess.run(
            ["uv", "run", "--project", str(REPO), "repld", "tasks", "--json"],
            cwd=str(tmp),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert_eq(out.returncode, 0, "repld tasks exits 0")
        data = json.loads(out.stdout)
        assert_true("tasks" in data, f"response carries tasks (got {data!r})")
        assert_true("tickers" in data, f"response carries tickers (got {data!r})")

        task = next((t for t in data["tasks"] if t.get("label") == "test-tasks"), None)
        assert_true(task is not None, f"deferred task listed (got {data['tasks']!r})")
        if task is not None:  # assert_true isn't a TypeGuard
            assert_true(
                task["started_at"] is not None, "listed task carries started_at"
            )
            assert_true(not task["done"], "listed task is still in flight")
            assert_true(
                task["finished_at"] is None, "in-flight task carries no finished_at yet"
            )

        ticker = next(
            (tk for tk in data["tickers"] if tk.get("label") == "_test_ticker"), None
        )
        assert_true(ticker is not None, f"ticker listed (got {data['tickers']!r})")
        if ticker is not None:
            assert_eq(ticker["seconds"], 60, "listed ticker carries its interval")
        print("  ✓ repld tasks listed the in-flight defer() and the @every ticker")

        b.exec("_test_ticker.cancel()")
        b.exec("tid2 = defer(asyncio.sleep(0), label='test-tasks-done')")

        # asyncio.sleep(0) finishes on the next loop tick, but finalize() runs
        # off that same tick — poll rather than assume it's done by the time
        # the dashboard round trip lands.
        deadline = time.monotonic() + 5
        done_task = None
        while time.monotonic() < deadline:
            out = subprocess.run(
                ["uv", "run", "--project", str(REPO), "repld", "tasks", "--json"],
                cwd=str(tmp),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            data = json.loads(out.stdout)
            done_task = next(
                (t for t in data["tasks"] if t.get("label") == "test-tasks-done"), None
            )
            if done_task is not None and done_task["done"]:
                break
            time.sleep(0.2)
        assert_true(
            done_task is not None and done_task["done"],
            f"quick deferred task finished (got {done_task!r})",
        )
        if done_task is not None:
            assert_true(
                done_task["finished_at"] is not None
                and done_task["finished_at"] >= done_task["started_at"],
                f"finished task carries a wall-clock finished_at (got {done_task!r})",
            )
        print(
            "  ✓ repld tasks carries finished_at (epoch seconds) once a task completes"
        )
    finally:
        b.close()


def _status_counts(tmp: Path) -> None:
    """`repld status --json --counts` fetches tasks_active/tickers for a
    sibling kernel too — absent (not 0) without the flag, since a reader
    needs to tell "idle" from "didn't ask"."""
    sibling = Path(tempfile.mkdtemp(prefix="repld-status-counts-"))
    try:
        b = Bridge(sibling)
        try:
            b.handshake()
            b.exec(
                "import asyncio\n"
                "async def _slow():\n"
                "    await asyncio.sleep(5)\n"
                "defer(_slow(), label='sibling-task')\n"
            )
            b.exec("@every(60)\ndef _sibling_ticker():\n    return 'tick'\n")

            def _status(counts: bool) -> dict:
                argv = [
                    "uv",
                    "run",
                    "--project",
                    str(REPO),
                    "repld",
                    "status",
                    "--json",
                ]
                if counts:
                    argv.append("--counts")
                out = subprocess.run(
                    argv,
                    cwd=str(tmp),
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                assert_eq(
                    out.returncode,
                    0,
                    f"repld status {'--counts ' if counts else ''}exits 0",
                )
                body = json.loads(out.stdout)
                sib = next(
                    (s for s in body["siblings"] if s.get("cwd") == str(sibling)), None
                )
                assert_true(
                    sib is not None, f"sibling kernel listed (got {body['siblings']!r})"
                )
                return sib or {}

            bare = _status(counts=False)
            assert_true(
                "tasks_active" not in bare, "sibling counts absent without --counts"
            )

            counted = _status(counts=True)
            assert_eq(
                counted.get("tasks_active"),
                1,
                f"--counts read the sibling's task count (got {counted!r})",
            )
            assert_eq(
                counted.get("tickers"),
                1,
                f"--counts read the sibling's ticker count (got {counted!r})",
            )
            print(
                "  ✓ repld status --counts fetches a sibling's live task/ticker counts"
            )
        finally:
            b.close()
    finally:
        _stop_kernel(sibling)
        shutil.rmtree(sibling, ignore_errors=True)


def _tasks_version_skew(tmp: Path) -> None:
    """A kernel predating `repld tasks` names itself in the error, not just
    "could not reach the dashboard" — `POST /api` answers an unknown method
    with HTTP 200 + a JSON-RPC error body (`dashboard._handle_api`), which
    `_fetch`'s old bare-`None`-on-anything-but-success return collapsed into
    the same message as a genuinely unreachable dashboard. A real kernel's
    lock/hint files are needed to reach `_fetch` at all; only the RPC result
    is faked — `_fetch` monkeypatched to whatever a 0.6.2 kernel's dashboard
    would answer for an unknown method."""
    from repld import paths, tasks_cmd

    b = Bridge(tmp)
    try:
        b.handshake()
        b.exec("1")  # force the lazy spawn so a lock/hint file exists

        real_fetch = tasks_cmd._fetch
        tasks_cmd._fetch = lambda lock, hint_path: {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32000, "message": "Unknown method: tasks"},
        }
        try:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = tasks_cmd.run_tasks(["--socket", str(paths.socket_path(tmp))])
            assert_eq(rc, 1, "run_tasks reports failure on an old kernel")
            assert_true(
                "predates" in err.getvalue() and "repld restart" in err.getvalue(),
                "names the version skew, not a generic unreachable message "
                f"(got {err.getvalue()!r})",
            )
        finally:
            tasks_cmd._fetch = real_fetch
        print("  ✓ repld tasks names a pre-`tasks` kernel instead of 'could not reach'")
    finally:
        b.close()


def _concurrent_boots(tmp: Path) -> None:
    """Racing kernel boots in one project yield exactly one survivor."""
    procs = [
        subprocess.Popen(
            ["uv", "run", "--project", str(REPO), "repld", "--no-display"],
            cwd=str(tmp),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(3)
    ]
    # Wait for the losers to exit — an unresolved `uv run` on a slow box
    # would otherwise read as a surviving kernel.
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and sum(p.poll() is None for p in procs) > 1:
        time.sleep(0.2)
    alive = [p for p in procs if p.poll() is None]
    assert_eq(len(alive), 1, "exactly one of three racing kernels survived")
    assert_true(
        all(p.returncode == 0 for p in procs if p.poll() is not None),
        "the losers exited 0",
    )
    # The lockfile pid is the kernel itself, not the `uv run` wrapper we hold.
    lock = _wait_lock(tmp)
    os.kill(int(lock["pid"]), 0)  # raises if the lockfile points at a corpse
    print("  ✓ flock: three concurrent boots left exactly one kernel")
    for p in procs:
        if p.poll() is None:
            p.send_signal(signal.SIGTERM)
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()


def _log_renderer_covers_every_event() -> None:
    """`repld log` must render every event type the TUI can.

    log_cmd._render and display._render are deliberately separate — one is
    stateless plain-ANSI over capped records, the other is stateful and rich-
    aware — but that means a new events.Event member renders in the pane and
    silently degrades to a bare type name in `repld log`. This is the seam
    that catches it.
    """
    import dataclasses
    import typing

    from repld import events, log_cmd

    def _sample(annotation) -> object:
        """A value of roughly the right shape — renderers do format it."""
        s = str(annotation)
        if "float" in s:
            return 0.0
        if "int" in s:
            return 0
        if "dict" in s:
            return {}
        if "list" in s:
            return []
        return ""

    members = typing.get_args(events.Event)
    assert_true(len(members) >= 9, f"found {len(members)} Event members to check")
    for cls in members:
        rec = {"type": cls.__name__}
        rec.update({f.name: _sample(f.type) for f in dataclasses.fields(cls)})
        rendered = log_cmd._render(rec)
        assert_true(
            rendered != f"{log_cmd._DIM}{cls.__name__}{log_cmd._RESET}",
            f"log_cmd._render handles {cls.__name__} (fell through to the "
            "unknown-type fallback)",
        )
    print(f"  ✓ repld log renders all {len(members)} event types")


def phase_15_ephemeral_bridge(_kernel: Kernel) -> None:
    """`repld bridge --ephemeral` spawns eagerly and dies with the bridge.

    The default bridge is lazy (spawn on first real tool call) and leaves the
    kernel running for the next session to attach to. `--ephemeral` inverts
    both halves: the kernel is up before the first request — so even
    `initialize` itself answers from a live process, not the cache/static
    fallback `_try_bridge_intercept` would otherwise use — and its whole
    directory is gone the moment stdin closes, rather than sitting around for
    `state.sweep_dead_project_dirs` to find on some future boot.
    """
    from repld import paths, state

    tmp = Path(tempfile.mkdtemp(prefix="repld-ephemeral-"))
    ephemeral_root = paths.RUNTIME_DIR / "ephemeral"
    before = set(ephemeral_root.iterdir()) if ephemeral_root.is_dir() else set()
    # Only the socket/lock/flock/dashboard/events/cache set moves to a private
    # path — spawn.spawn_headless spawns with cwd=os.getcwd() regardless of
    # sock_path, so the kernel's *project* surface (./gists, repld_init.py,
    # .env, .venv binding) must resolve exactly as it does for the persistent
    # kernel. Proved with a real gist rather than just asserted from the spawn
    # code, since "the socket moved" is exactly the kind of change that looks
    # safe and silently isn't.
    (tmp / "gists").mkdir()
    (tmp / "gists" / "ephcheck.py").write_text(
        '"""Ephemeral-bridge cwd/gist regression check."""\n\nVALUE = 42\n'
    )
    try:
        b = Bridge(tmp, "--ephemeral")
        pid = None
        try:
            resp = _handshake(b)
            assert_eq(
                resp["result"]["serverInfo"]["name"],
                "repld",
                "ephemeral bridge answers initialize from an already-live kernel",
            )

            after = set(ephemeral_root.iterdir()) if ephemeral_root.is_dir() else set()
            new_dirs = after - before
            assert_true(
                len(new_dirs) == 1,
                f"exactly one ephemeral dir created (got {new_dirs!r})",
            )
            ephemeral_dir = new_dirs.pop()
            lock_path = ephemeral_dir / "kernel.lock"
            assert_true(lock_path.exists(), "ephemeral kernel wrote its lockfile")
            pid = json.loads(lock_path.read_text())["pid"]
            assert_true(
                state.pid_alive(pid), "ephemeral kernel is a real, running process"
            )

            out = b.exec("print('alive in the one-off kernel')")
            assert_true(
                "alive in the one-off kernel" in out["result"]["content"][0]["text"],
                f"exec runs against the ephemeral kernel (got {out!r})",
            )

            out = b.exec("import repld; print(repld.socket_path())")
            assert_true(
                str(ephemeral_dir / "kernel.sock")
                in out["result"]["content"][0]["text"],
                f"socket_path() names the ephemeral socket, not the project's (got {out!r})",
            )

            out = b.exec("import ephcheck; print(ephcheck.VALUE)")
            assert_true(
                "42" in out["result"]["content"][0]["text"],
                f"./gists resolves for the ephemeral kernel too (got {out!r})",
            )
        finally:
            # Generous timeout: SIGKILLing the bridge mid-teardown would still
            # leave the kernel dying (SIGTERM is sent before the wait loop
            # that this timeout would interrupt), but there's no reason to
            # race it in a test that isn't asserting on that edge.
            b.close(timeout=10)

        assert_true(pid is not None, "reached the point of recording the kernel's pid")
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and state.pid_alive(pid):
            time.sleep(0.1)
        assert_true(not state.pid_alive(pid), "ephemeral kernel died with its bridge")
        assert_true(not ephemeral_dir.exists(), "ephemeral directory reclaimed on exit")
        print(
            "  ✓ repld bridge --ephemeral: spawned eagerly, "
            "killed + reclaimed on stdin close"
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def phase_15_headless(_kernel: Kernel) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="repld-headless-"))
    try:
        _autospawn_and_heal(tmp)
        _bridge_served_tools(tmp)
        _lazy_discovery_from_cache(tmp)
        _version_mismatch_cache_discarded(tmp)
        _bridge_tool_bypass_error(tmp)
        _inflight_never_stranded(tmp)
        _targeted_push(tmp)
        _no_display_skips_queue(tmp)
        _event_log(tmp)
        _tasks_listing(tmp)
        _status_counts(tmp)
        _tasks_version_skew(tmp)
        _project_flags(tmp)
        _session_rebind(tmp)  # SIGKILLs the kernel the cases above read back
        _log_renderer_covers_every_event()
        _stop_kernel(tmp)
        _concurrent_boots(tmp)
    finally:
        _stop_kernel(tmp)
        shutil.rmtree(tmp, ignore_errors=True)
