"""Shared test infrastructure — Bridge, Kernel, assertion helpers.

Used by smoketest.py and all phase modules.
"""

import json
import os
import signal
import subprocess
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

    def wait_notification(
        self, method: str, *, kind: str | None = None, timeout: float = 5.0
    ) -> dict:
        def _matches(m: dict) -> bool:
            if m.get("method") != method:
                return False
            if kind is not None:
                return m.get("params", {}).get("meta", {}).get("kind") == kind
            return True

        # Check stash first.
        for m in getattr(self, "_notifs", []):
            if _matches(m):
                self._notifs.remove(m)
                return m
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                msg = self.inbox.get(timeout=deadline - time.monotonic())
            except Empty:
                break
            if _matches(msg):
                return msg
            # Stash non-matching messages for later retrieval.
            self._stash_notification(msg)
        raise TimeoutError(f"no {method} notification (kind={kind}) within {timeout}s")

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
