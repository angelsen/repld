"""Shared test infrastructure — Bridge, Kernel, assertion helpers.

Used by smoketest.py and all phase modules.
"""

import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from queue import Empty, Queue

REPO = Path(__file__).resolve().parent.parent

# Absolute real-binary paths first, on purpose: a bare name resolved via
# PATH can hit a personal launcher wrapper ahead of the real binary (this
# machine's own `google-chrome-stable` on PATH is one — it drops
# --remote-debugging-port/--user-data-dir and no-ops if something is already
# listening on 9222, so a throwaway spawn through it silently launches
# nothing and this class's caller ends up back on the developer's real
# Chrome). The bare-name fallback stays for a plain PATH-only install.
_CHROME_CANDIDATES = (
    "/usr/bin/google-chrome-stable",
    "/usr/bin/google-chrome",
    "/opt/google/chrome/chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
)


def _find_chrome() -> str | None:
    for name in _CHROME_CANDIDATES:
        if "/" in name:
            if Path(name).exists():
                return name
        else:
            found = shutil.which(name)
            if found:
                return found
    return None


class ThrowawayChrome:
    """Headless Chrome on an ephemeral port and a throwaway profile — isolated
    from whatever the developer's own debug Chrome (and every other repld
    kernel on the machine attached to it) is doing at the same time.

    `--user-data-dir` + `--remote-debugging-port=0` makes Chrome write the
    port it actually bound to `DevToolsActivePort` inside that profile dir —
    the same resolution `browser.pool.BrowserPool.connect(profile=...)` uses
    for a hand-launched Chrome, reused here for a spawned one.
    """

    def __init__(self) -> None:
        self.binary = _find_chrome()
        self.proc: subprocess.Popen | None = None
        self.port: int | None = None
        self.profile_dir: Path | None = None

    def start(self, timeout: float = 15.0) -> bool:
        """Launch Chrome and wait for its port. False if unavailable or it
        never came up — the caller skips phase 6 rather than failing the run,
        same as `_chrome_ready` already does for an unreachable Chrome."""
        if self.binary is None:
            return False
        self.profile_dir = Path(tempfile.mkdtemp(prefix="repld-test-chrome-"))
        self.proc = subprocess.Popen(
            [
                self.binary,
                "--headless=new",
                "--remote-debugging-port=0",
                f"--user-data-dir={self.profile_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-extensions",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        port_file = self.profile_dir / "DevToolsActivePort"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                return False
            try:
                self.port = int(port_file.read_text().splitlines()[0].strip())
                return True
            except (OSError, ValueError, IndexError):
                time.sleep(0.1)
        return False

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.profile_dir is not None:
            shutil.rmtree(self.profile_dir, ignore_errors=True)


def lock_path_for(cwd: Path) -> Path:
    """The kernel lockfile a kernel started in `cwd` will write.

    Runtime state lives under $XDG_RUNTIME_DIR, not the project directory, so
    tests have to resolve it the same way the kernel does.
    """
    from repld import paths

    return paths.lock_path(cwd)


def runtime_dir_for(cwd: Path) -> Path:
    from repld import paths

    return paths.project_dir(cwd)


class Bridge:
    """Subprocess wrapper. One bridge = one MCP session.

    Writes requests to stdin, reads NDJSON messages off stdout into a queue.
    """

    def __init__(
        self,
        cwd: Path,
        *extra_args: str,
        env: dict[str, str | None] | None = None,
        global_args: tuple[str, ...] = (),
    ):
        # A None value deletes the key rather than setting it — the ambient
        # environment this test process itself runs under (e.g. when this
        # very suite runs inside a Claude Code session) can already carry
        # CLAUDE_CODE_SESSION_ID, and a bare os.environ.copy() would leak it
        # into a bridge a test means to spawn with no Claude Code identity.
        proc_env = os.environ.copy()
        for k, v in (env or {}).items():
            if v is None:
                proc_env.pop(k, None)
            else:
                proc_env[k] = v
        self.proc = subprocess.Popen(
            [
                "uv",
                "run",
                "--project",
                str(REPO),
                "repld",
                *global_args,
                "bridge",
                *extra_args,
            ],
            cwd=str(cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=proc_env,
        )
        self.inbox: Queue[dict] = Queue()
        self._notifs: list[dict] = []
        # Undrained, a PIPE'd stderr blocks the bridge once its 64 KiB fills —
        # which surfaces here as a bare `no response` timeout, so keep the tail.
        self.stderr_tail: deque[str] = deque(maxlen=40)
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()
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

    def _drain_stderr(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self.stderr_tail.append(line.rstrip())

    def handshake(self, *, timeout: float = 5.0) -> dict:
        """`initialize` + `notifications/initialized`; returns the initialize response."""
        resp = self.call(
            "initialize", {"protocolVersion": "2024-11-05"}, timeout=timeout
        )
        self.send("notifications/initialized", {}, notif=True)
        return resp

    def exec(
        self, code: str, *, timeout: float | None = None, call_timeout: float = 5.0
    ) -> dict:
        """`tools/call exec` — the raw response, since callers read text, `_meta`
        or `isError` from it. `timeout` is the cell's inline budget."""
        args: dict = {"code": code}
        if timeout is not None:
            args["timeout"] = timeout
        return self.call(
            "tools/call", {"name": "exec", "arguments": args}, timeout=call_timeout
        )

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
            # Unsolicited notification: stash it so wait_notification finds it.
            self._notifs.append(msg)
        tail = "\n".join(self.stderr_tail)
        raise TimeoutError(
            f"no response to {method} within {timeout}s; bridge stderr tail:\n{tail}"
        )

    def wait_notification(
        self,
        method: str,
        *,
        kind: str | None = None,
        where: Callable[[dict], bool] | None = None,
        timeout: float = 5.0,
    ) -> dict:
        """First matching push, stash first — so `kind=` alone returns the OLDEST
        push of that kind, which may be an earlier scenario's; narrow with `where=`."""

        def _matches(m: dict) -> bool:
            if m.get("method") != method:
                return False
            meta = m.get("params", {}).get("meta", {})
            if kind is not None and meta.get("kind") != kind:
                return False
            return where is None or where(m)

        for m in self._notifs:
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
            self._notifs.append(msg)
        raise TimeoutError(f"no {method} notification (kind={kind}) within {timeout}s")

    def close(self, timeout: float = 3) -> None:
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()


class Kernel:
    def __init__(self, cwd: Path):
        self.cwd = cwd
        self.stderr_log = cwd / "kernel.stderr"
        env = os.environ.copy()
        # The child gets its own dup of the fd; closing the parent's copy here
        # doesn't cut off the kernel's stderr.
        with open(self.stderr_log, "w") as log:
            self.proc = subprocess.Popen(
                ["uv", "run", "--project", str(REPO), "repld", "--no-display"],
                cwd=str(cwd),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=log,
                env=env,
            )
        self._wait_lockfile()

    @property
    def lock_path(self) -> Path:
        return lock_path_for(self.cwd)

    def _wait_lockfile(self, timeout: float = 10.0) -> None:
        lock = self.lock_path
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


def content_text(resp: dict) -> str:
    """The first text block of a tools/call result."""
    return resp["result"]["content"][0]["text"]


def assert_eq(got, expected, label: str) -> None:
    if got != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {got!r}")


def assert_true(cond, label: str) -> None:
    if not cond:
        raise AssertionError(f"{label}: condition false")
