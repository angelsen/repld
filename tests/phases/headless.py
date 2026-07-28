"""Phase 15: headless kernel — slim loader, self-healing, targeted push, event log.

Everything here runs in its own tempdir with no kernel started up front: the
point is that the bridge produces one.
"""

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from harness import REPO, Bridge, Kernel, assert_eq, assert_true, lock_path_for


def _handshake(b: Bridge) -> dict:
    resp = b.call("initialize", {"protocolVersion": "2024-11-05"}, timeout=30)
    b.send("notifications/initialized", {}, notif=True)
    return resp


def _exec(
    b: Bridge, code: str, timeout: float = 5.0, exec_timeout: float | None = None
) -> dict:
    args: dict = {"code": code}
    if exec_timeout is not None:
        args["timeout"] = exec_timeout
    return b.call("tools/call", {"name": "exec", "arguments": args}, timeout=timeout)


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
    """Bridge with no kernel spawns one; killing it heals on the next call."""
    b = Bridge(tmp)
    try:
        _handshake(b)
        lock = _wait_lock(tmp)
        first_pid = lock["pid"]
        print(f"  ✓ bridge auto-spawned a headless kernel (pid {first_pid})")

        _exec(b, "SENTINEL = 1")

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
        resp = _exec(b, "print('healed')", timeout=40)
        text = resp["result"]["content"][0]["text"]
        assert_true("healed" in text, f"exec works after respawn (got {text!r})")
        second_pid = _wait_lock(tmp)["pid"]
        assert_true(second_pid != first_pid, "a new kernel pid took over")
        print(f"  ✓ next request healed onto a fresh kernel (pid {second_pid})")

        # State is genuinely new — proves we're talking to a different process.
        resp = _exec(b, "print('SENTINEL' in dir())")
        assert_true(
            "False" in resp["result"]["content"][0]["text"],
            "respawned kernel has a fresh namespace",
        )

        # Channel push still lands, which only works if the handshake replay
        # marked the new kernel's session initialized.
        _exec(b, "notify('after respawn')")
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

        _exec(b, "SENTINEL = 1")

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

        resp = _exec(b, "print('SENTINEL' in dir())", timeout=40)
        assert_true(
            "False" in resp["result"]["content"][0]["text"],
            "restarted kernel has a fresh namespace",
        )
        _exec(b, "notify('after restart')")
        b.wait_notification("notifications/claude/channel", timeout=10)
        print("  ✓ session survived the restart: handshake replayed, push lands")
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
        raise AssertionError(f"could not connect to the kernel: {result}")
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


def _targeted_push(tmp: Path) -> None:
    """A task's completion notifies the session that started it, and only it."""
    a = Bridge(tmp)
    c = Bridge(tmp)
    try:
        _handshake(a)
        _handshake(c)

        # Deferred exec from A: exceeds its own timeout, completes later.
        resp = _exec(
            a,
            "import asyncio\nawait asyncio.sleep(1.5)\nprint('from A')",
            timeout=10,
            exec_timeout=0.3,
        )
        assert_eq(resp["result"]["_meta"]["done"], False, "exec deferred")

        note = a.wait_notification(
            "notifications/claude/channel", kind="task_done", timeout=15
        )
        assert_true("from A" in note["params"]["content"], "originator got its output")
        print("  ✓ deferred exec notified the session that started it")

        try:
            c.wait_notification(
                "notifications/claude/channel", kind="task_done", timeout=2
            )
            raise AssertionError("task_done leaked to the other session")
        except TimeoutError:
            pass
        print("  ✓ the other session saw nothing (no broadcast fallback)")

        # Ambient notify() is genuinely shared state — it still reaches both.
        _exec(c, "notify('ambient')")
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
        resp = _exec(
            b,
            "import repld.events as _e\n"
            "print(f'{_e._disabled}:{_e._queue is None}:{len(_e._pre_init_buf)}')",
        )
        out = resp["result"]["content"][0]["text"]
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
    time.sleep(6)
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


def phase_15_headless(_kernel: Kernel) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="repld-headless-"))
    try:
        _autospawn_and_heal(tmp)
        _bridge_served_tools(tmp)
        _bridge_tool_bypass_error(tmp)
        _targeted_push(tmp)
        _no_display_skips_queue(tmp)
        _event_log(tmp)
        _log_renderer_covers_every_event()
        _stop_kernel(tmp)
        _concurrent_boots(tmp)
    finally:
        _stop_kernel(tmp)
        shutil.rmtree(tmp, ignore_errors=True)
