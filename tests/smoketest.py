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
        core_tools = {"cancel", "exec", "get_task"}
        assert_true(
            core_tools.issubset(set(tool_names)),
            f"tools/list contains core tools (got {tool_names!r})",
        )
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


def phase_6(kernel: Kernel) -> None:
    """Browser integration — requires Chrome with --remote-debugging-port=9222.

    Skips gracefully if Chrome is not reachable.
    """
    import urllib.request as _urlreq

    try:
        with _urlreq.urlopen("http://localhost:9222/json/version", timeout=2) as r:
            r.read()
    except Exception:
        print("  - phase 6: Chrome not available on port 9222, skipping")
        return

    b = Bridge(kernel.cwd)
    try:
        b.call("initialize", {"protocolVersion": "2024-11-05"})
        b.send("notifications/initialized", {}, notif=True)

        # Verify browser tools are in the list
        resp = b.call("tools/list")
        tool_names = [t["name"] for t in resp["result"]["tools"]]
        browser_tools = {
            "browser_attach",
            "browser_detach",
            "browser_tabs",
            "browser_pages",
            "browser_js",
            "browser_network",
            "browser_body",
            "browser_click",
            "browser_type",
            "browser_console",
            "browser_screenshot",
            "browser_cdp",
            "browser_clear",
        }
        assert_true(
            browser_tools.issubset(set(tool_names)),
            f"tools/list contains all 13 browser tools (got {tool_names!r})",
        )
        print("  ✓ all 13 browser tools in tools/list")

        # Attach any open tab
        resp = b.call(
            "tools/call",
            {"name": "browser_attach", "arguments": {"pattern": "*"}},
            timeout=10.0,
        )
        result_text = resp["result"]["content"][0]["text"]
        assert_true(
            "result" in result_text,
            f"browser_attach returned result (got {result_text!r})",
        )
        print(f"  ✓ browser_attach: {result_text[:80]!r}")

        # List attached tabs
        resp = b.call(
            "tools/call", {"name": "browser_tabs", "arguments": {}}, timeout=5.0
        )
        tabs_json = resp["result"]["content"][0]["text"]
        tabs = json.loads(tabs_json)
        if not tabs:
            print(
                "  - browser_tabs: no tabs attached (Chrome may have no open tabs), skipping js/network"
            )
            return
        tab_target = tabs[0]["target"]
        tab_url = tabs[0]["url"]
        print(
            f"  ✓ browser_tabs: {len(tabs)} tab(s), first target={tab_target!r} url={tab_url!r}"
        )

        # browser_js: evaluate 1+1
        resp = b.call(
            "tools/call",
            {
                "name": "browser_js",
                "arguments": {"target": tab_target, "code": "1+1"},
            },
            timeout=10.0,
        )
        js_text = resp["result"]["content"][0]["text"]
        js_result = json.loads(js_text)
        assert_true(
            js_result.get("result") == 2,
            f"browser_js 1+1 == 2 (got {js_result!r})",
        )
        print(f"  ✓ browser_js: 1+1 = {js_result['result']!r}")

        # browser_network: returns a list (may be empty)
        resp = b.call(
            "tools/call",
            {
                "name": "browser_network",
                "arguments": {"target": tab_target},
            },
            timeout=5.0,
        )
        net_text = resp["result"]["content"][0]["text"]
        net_rows = json.loads(net_text)
        assert_true(
            isinstance(net_rows, list),
            f"browser_network returns list (got {net_text[:80]!r})",
        )
        print(f"  ✓ browser_network: {len(net_rows)} row(s)")

        # browser_detach all
        resp = b.call(
            "tools/call",
            {"name": "browser_detach", "arguments": {}},
            timeout=5.0,
        )
        detach_text = resp["result"]["content"][0]["text"]
        print(f"  ✓ browser_detach: {detach_text[:80]!r}")

        # Verify tabs now empty
        resp = b.call(
            "tools/call", {"name": "browser_tabs", "arguments": {}}, timeout=5.0
        )
        tabs_after = json.loads(resp["result"]["content"][0]["text"])
        assert_eq(tabs_after, [], "browser_tabs after detach is empty")
        print("  ✓ browser_tabs empty after detach")
    finally:
        b.close()


def phase_7_defer(kernel: Kernel) -> None:
    """defer() from exec → task_id returned, channel push on completion."""
    b = Bridge(kernel.cwd)
    try:
        b.call("initialize", {"protocolVersion": "2024-11-05"})
        b.send("notifications/initialized", {}, notif=True)

        # defer a coroutine — should return task_id inline, then push channel
        resp = b.call(
            "tools/call",
            {
                "name": "exec",
                "arguments": {
                    "code": (
                        "import asyncio\n"
                        "async def _slow():\n"
                        "    await asyncio.sleep(0.3)\n"
                        "    print('deferred done')\n"
                        "tid = defer(_slow(), label='test-defer')\n"
                        "print(f'task_id={tid}')"
                    ),
                },
            },
            timeout=3.0,
        )
        content = resp["result"]["content"][0]["text"]
        assert_true("task_id=" in content, f"defer returned task_id (got {content!r})")
        print("  ✓ defer: returned task_id inline")

        # Wait for channel notification
        notif = b.wait_notification("notifications/claude/channel", timeout=5.0)
        params = notif["params"]
        assert_eq(params["meta"]["kind"], "task_done", "defer channel kind")
        assert_true(
            "deferred done" in params["content"],
            f"defer output in channel (got {params['content']!r})",
        )
        assert_true(
            "test-defer" in params["content"],
            f"label in channel content (got {params['content']!r})",
        )
        assert_true(
            params["meta"].get("label") == "test-defer",
            f"label in channel meta (got {params['meta']!r})",
        )
        assert_eq(params["meta"]["error"], "0", "defer success error=0")
        print("  ✓ defer: channel push with label + output")

        # Error case
        resp = b.call(
            "tools/call",
            {
                "name": "exec",
                "arguments": {
                    "code": (
                        "async def _boom():\n"
                        "    raise ValueError('kaboom')\n"
                        "defer(_boom(), label='error-test')"
                    ),
                },
            },
            timeout=3.0,
        )
        notif = b.wait_notification("notifications/claude/channel", timeout=5.0)
        assert_eq(notif["params"]["meta"]["error"], "1", "defer error case")
        assert_eq(
            notif["params"]["meta"].get("label"),
            "error-test",
            "error case label in meta",
        )
        print("  ✓ defer error case: error=1, label preserved")

        # TypeError on non-coroutine
        resp = b.call(
            "tools/call",
            {
                "name": "exec",
                "arguments": {
                    "code": "defer(42)",
                },
            },
            timeout=3.0,
        )
        is_error = resp["result"].get("isError", False)
        content = resp["result"]["content"][0]["text"]
        assert_true(is_error, "defer(42) is an error")
        assert_true(
            "TypeError" in content, f"defer(42) raises TypeError (got {content!r})"
        )
        print("  ✓ defer(non-coroutine): TypeError")
    finally:
        b.close()


PHASES = {
    3: phase_3,
    4: lambda k: (phase_4(k), phase_4b_pregate(k)),
    5: lambda k: (phase_5(k), phase_5_init(k)),
    6: phase_6,
    7: phase_7_defer,
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
