"""End-to-end smoketest.

Starts a kernel in a tempdir, opens a bridge subprocess, drives MCP JSON-RPC
over its stdio, verifies responses. Grows phase-by-phase alongside the
implementation.

Usage:  uv run python tests/smoketest.py [--phase N]
"""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from queue import Empty, Queue

REPO = Path(__file__).resolve().parent.parent


class Bridge:
    """Subprocess wrapper. One bridge = one MCP session.

    Writes requests to stdin, reads NDJSON messages off stdout into a queue.
    """

    def __init__(self, cwd: Path):
        env = os.environ.copy()
        self.proc = subprocess.Popen(
            ["uv", "run", "--project", str(REPO), "repld", "bridge"],
            cwd=str(cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        self.inbox: Queue[dict] = Queue()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._next_id = 1

    def _read_loop(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            self.inbox.put(msg)

    def send(
        self, method: str, params: dict | None = None, *, notif: bool = False
    ) -> int | None:
        req: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            req["params"] = params
        if not notif:
            req["id"] = self._next_id
            self._next_id += 1
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        return req.get("id")

    def call(
        self, method: str, params: dict | None = None, *, timeout: float = 5.0
    ) -> dict:
        rid = self.send(method, params)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                msg = self.inbox.get(timeout=deadline - time.monotonic())
            except Empty:
                break
            if msg.get("id") == rid:
                return msg
            # Unsolicited notification; re-queue? Simpler: push into a
            # side list so channel-push assertions can still find it.
            self._stash_notification(msg)
        raise TimeoutError(f"no response to {method} within {timeout}s")

    _notifs: list[dict]

    def _stash_notification(self, msg: dict) -> None:
        if not hasattr(self, "_notifs"):
            self._notifs = []
        self._notifs.append(msg)

    def wait_notification(self, method: str, *, timeout: float = 5.0) -> dict:
        # Check stash first.
        for m in getattr(self, "_notifs", []):
            if m.get("method") == method:
                self._notifs.remove(m)
                return m
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                msg = self.inbox.get(timeout=deadline - time.monotonic())
            except Empty:
                break
            if msg.get("method") == method:
                return msg
            if "id" in msg:
                # We weren't expecting a response; stash.
                self._stash_notification(msg)
        raise TimeoutError(f"no {method} notification within {timeout}s")

    def close(self) -> None:
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()


class Kernel:
    def __init__(self, cwd: Path):
        self.cwd = cwd
        self.stderr_log = cwd / "kernel.stderr"
        env = os.environ.copy()
        self.proc = subprocess.Popen(
            ["uv", "run", "--project", str(REPO), "repld", "--no-display"],
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=open(self.stderr_log, "w"),
            env=env,
        )
        self._wait_lockfile()

    def _wait_lockfile(self, timeout: float = 10.0) -> None:
        lock = self.cwd / ".pyrepl.lock"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if lock.exists():
                return
            if self.proc.poll() is not None:
                break
            time.sleep(0.1)
        # Read stderr to help debugging
        try:
            log = self.stderr_log.read_text()
        except Exception:
            log = "<no log>"
        raise RuntimeError(f"kernel never wrote lockfile. stderr:\n{log}")

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def assert_eq(got, expected, label: str) -> None:
    if got != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {got!r}")


def assert_true(cond, label: str) -> None:
    if not cond:
        raise AssertionError(f"{label}: condition false")


def phase_2(kernel: Kernel) -> None:
    # Echo handler was phase 2; phase 3 replaces it. This is retired — we now
    # just verify bridge↔kernel plumbing by doing a real initialize.
    pass


def phase_3(kernel: Kernel) -> None:
    """initialize → tools/list → exec sync → exec nudge → get_task (poll to done)."""
    b = Bridge(kernel.cwd)
    try:
        resp = b.call("initialize", {"protocolVersion": "2024-11-05"})
        result = resp["result"]
        assert_eq(result["serverInfo"]["name"], "repld", "initialize.serverInfo.name")
        assert_true(
            "claude/channel" in result["capabilities"]["experimental"],
            "initialize.capabilities advertises claude/channel",
        )
        print("  ✓ initialize")

        b.send("notifications/initialized", {}, notif=True)
        print("  ✓ notifications/initialized sent")

        resp = b.call("tools/list")
        tool_names = [t["name"] for t in resp["result"]["tools"]]
        assert_eq(sorted(tool_names), ["cancel", "exec", "get_task"], "tools/list")
        print(f"  ✓ tools/list: {tool_names}")

        # Sync exec
        resp = b.call(
            "tools/call",
            {"name": "exec", "arguments": {"code": "print('hi from exec')"}},
        )
        content = resp["result"]["content"][0]["text"]
        meta = resp["result"]["_meta"]
        assert_true("hi from exec" in content, f"sync exec output (got {content!r})")
        assert_eq(meta["done"], True, "sync exec meta.done")
        print(f"  ✓ sync exec: {content.strip()!r}")

        # Nudge exec
        resp = b.call(
            "tools/call",
            {
                "name": "exec",
                "arguments": {
                    "code": "import time\nfor i in range(3):\n    time.sleep(0.3); print(f'step {i}')",
                    "timeout": 0.2,
                },
            },
            timeout=3.0,
        )
        meta = resp["result"]["_meta"]
        assert_eq(meta["done"], False, "nudge exec meta.done")
        task_id = meta["task_id"]
        assert_true(task_id, "nudge has task_id")
        print(f"  ✓ nudged exec: task_id={task_id}")

        # Poll get_task until done
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            resp = b.call(
                "tools/call", {"name": "get_task", "arguments": {"task_id": task_id}}
            )
            snap = resp["result"]["_meta"]
            if snap.get("done"):
                break
            time.sleep(0.2)
        else:
            raise AssertionError("get_task: task never completed within 5s")
        assert_true(
            "step 2" in snap["text"],
            f"get_task captured all output (got {snap['text']!r})",
        )
        print(f"  ✓ get_task: done, output {snap['text'].strip()!r}")

        # Spill test — every cell with output now spills; large output
        # additionally trips the truncated-preview path.
        resp = b.call(
            "tools/call",
            {
                "name": "exec",
                "arguments": {"code": "print('x' * 70000)", "timeout": 5.0},
            },
            timeout=10.0,
        )
        meta = resp["result"]["_meta"]
        assert_eq(meta["done"], True, "spill exec done")
        assert_eq(meta["spilled"], True, "spill exec meta.spilled")
        spill_path = meta["spill_path"]
        assert_true(
            spill_path and Path(spill_path).exists(), f"spill file exists: {spill_path}"
        )
        # Read directly from the file (no MCP read_spill tool — agents use Read).
        with open(spill_path) as f:
            head = f.read(100)
        assert_true(
            head.startswith("x" * 50), f"spill file starts with x's (got {head[:20]!r})"
        )
        print(f"  ✓ spill: {spill_path} ({len(head)} chars head, starts with x's)")
    finally:
        b.close()


def phase_4(kernel: Kernel) -> None:
    """Nudged exec → channel notification arrives with kind=task_done.
    notify() from user code → channel notification with custom meta."""
    b = Bridge(kernel.cwd)
    try:
        b.call("initialize", {"protocolVersion": "2024-11-05"})
        b.send("notifications/initialized", {}, notif=True)
        print("  ✓ initialize + notifications/initialized")

        # Nudge-and-wait-for-channel
        resp = b.call(
            "tools/call",
            {
                "name": "exec",
                "arguments": {
                    "code": "import time; time.sleep(0.5); print('slow done')",
                    "timeout": 0.1,
                },
            },
            timeout=3.0,
        )
        meta = resp["result"]["_meta"]
        assert_eq(meta["done"], False, "nudge meta.done")
        task_id = meta["task_id"]
        print(f"  ✓ nudged: task_id={task_id}")

        notif = b.wait_notification("notifications/claude/channel", timeout=5.0)
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
        resp = b.call(
            "tools/call",
            {
                "name": "exec",
                "arguments": {"code": "notify('ping', kind='user', color='blue')"},
            },
            timeout=3.0,
        )
        assert_eq(resp["result"]["_meta"]["done"], True, "notify exec done sync")

        notif = b.wait_notification("notifications/claude/channel", timeout=3.0)
        params = notif["params"]
        assert_eq(params["content"], "ping", "notify content")
        assert_eq(params["meta"]["kind"], "user", "notify meta.kind")
        assert_eq(params["meta"]["color"], "blue", "notify meta.color")
        print(f"  ✓ notify(): content={params['content']!r} meta={params['meta']}")

        # Error in nudged exec → error="1" + traceback content
        resp = b.call(
            "tools/call",
            {
                "name": "exec",
                "arguments": {
                    "code": "import time\ntime.sleep(0.3)\nraise RuntimeError('boom')",
                    "timeout": 0.1,
                },
            },
            timeout=3.0,
        )
        err_task_id = resp["result"]["_meta"]["task_id"]
        notif = b.wait_notification("notifications/claude/channel", timeout=5.0)
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
        resp = b.call(
            "tools/call",
            {
                "name": "exec",
                "arguments": {
                    "code": "import time; time.sleep(0.3); notify('queued push')",
                    "timeout": 0.1,
                },
            },
            timeout=3.0,
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


def phase_5(kernel: Kernel) -> None:
    """Refuse to start a second kernel in the same cwd (lockfile check)."""
    # Try to start a *second* kernel in the same cwd. Should fail.
    env = os.environ.copy()
    proc = subprocess.run(
        ["uv", "run", "--project", str(REPO), "repld", "--no-display"],
        cwd=str(kernel.cwd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
        env=env,
    )
    assert_true(proc.returncode != 0, "second kernel exits non-zero")
    combined = (proc.stdout or "") + (proc.stderr or "")
    assert_true(
        "another kernel" in combined,
        f"second-kernel error mentions 'another kernel' (got: {combined!r})",
    )
    print("  ✓ stale-lockfile check: second kernel refused to start")


def phase_5_init(_kernel: Kernel) -> None:
    """Spawn a dedicated kernel with --init to verify init-file execution."""
    import tempfile as _tmp

    tmp = Path(_tmp.mkdtemp(prefix="repld-init-"))
    try:
        init_path = tmp / "repl.py"
        init_path.write_text(
            "import asyncio\n"
            "X = 42\n"
            "async def _bg():\n"
            "    await asyncio.sleep(0.05)\n"
            "bg = asyncio.create_task(_bg())\n"
            "print('init loaded, X=', X)\n"
        )
        k = Kernel.__new__(Kernel)
        k.cwd = tmp
        k.stderr_log = tmp / "kernel.stderr"
        env = os.environ.copy()
        k.proc = subprocess.Popen(
            [
                "uv",
                "run",
                "--project",
                str(REPO),
                "repld",
                "--no-display",
                "--init",
                str(init_path),
            ],
            cwd=str(tmp),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=open(k.stderr_log, "w"),
            env=env,
        )
        try:
            k._wait_lockfile()
            b = Bridge(tmp)
            try:
                b.call("initialize", {"protocolVersion": "2024-11-05"})
                b.send("notifications/initialized", {}, notif=True)
                resp = b.call(
                    "tools/call",
                    {"name": "exec", "arguments": {"code": "print(X)"}},
                )
                content = resp["result"]["content"][0]["text"]
                assert_true(
                    "42" in content,
                    f"--init file's X=42 visible in __main__ (got {content!r})",
                )
                print("  ✓ --init file executed and X=42 visible in namespace")
            finally:
                b.close()
        finally:
            k.stop()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


PHASES = {
    3: phase_3,
    4: lambda k: (phase_4(k), phase_4b_pregate(k)),
    5: lambda k: (phase_5(k), phase_5_init(k)),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", type=int, default=3, help="highest phase to run")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="repld-smoketest-"))
    kernel = None
    try:
        print(f"== kernel cwd: {tmp} ==")
        kernel = Kernel(tmp)
        for p in sorted(PHASES):
            if p > args.phase:
                break
            print(f"== phase {p} ==")
            PHASES[p](kernel)
        print("== all phases passed ==")
        return 0
    except Exception as e:
        print(f"FAIL: {e}", file=sys.stderr)
        import traceback as tb

        tb.print_exc()
        if kernel is not None:
            try:
                log = kernel.stderr_log.read_text()
                print(f"--- kernel stderr ---\n{log}", file=sys.stderr)
            except Exception:
                pass
        return 1
    finally:
        if kernel is not None:
            kernel.stop()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
