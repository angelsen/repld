"""Shared test infrastructure — Bridge, Kernel, assertion helpers.

Used by smoketest.py and all phase modules.
"""

import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
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
        never came up; the caller fails the run, since phase 6 cannot run without it."""
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
            except OSError, ValueError, IndexError:
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


def stop_kernel_at(cwd: Path) -> None:
    """Stop whatever kernel owns `cwd`, one still booting included.

    A held flock with no live pid in `kernel.lock` is a kernel between its
    claim and its lockfile write, so that state is waited out, not read as
    nothing to stop.
    """
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        pid = _live_lock_pid(cwd)
        if pid is not None:
            _terminate(pid)
            return
        if not _is_flock_held(cwd):
            return
        time.sleep(0.1)


def _live_lock_pid(cwd: Path) -> int | None:
    """The pid `kernel.lock` names, if that process is running."""
    try:
        pid = int(json.loads(lock_path_for(cwd).read_text())["pid"])
        os.kill(pid, 0)
    except OSError, KeyError, TypeError, ValueError:
        return None
    return pid


def _terminate(pid: int) -> None:
    """SIGTERM `pid` and wait for it to go, with SIGKILL after 10 s."""
    try:
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            os.kill(pid, 0)
            time.sleep(0.1)
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def _is_flock_held(cwd: Path) -> bool:
    """Whether a kernel holds the project flock for `cwd`.

    Opened without `O_CREAT`, so asking never brings a project dir into being.
    """
    from repld import paths

    try:
        fd = os.open(paths.flock_for(paths.socket_path(cwd)), os.O_RDWR)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        return False
    finally:
        os.close(fd)


def fake_claude(directory: Path) -> Path:
    """A shell named `claude` in `directory`, for a bridge to run under.

    A copy of a system binary is killed on macOS (its signature no longer
    verifies), so there it is a symlink, whose exec'd name is still `claude`.
    """
    fake = directory / "claude"
    sh = shutil.which("sh") or "/bin/sh"
    if sys.platform == "darwin":
        fake.symlink_to(sh)
    else:
        shutil.copy(sh, fake)
    return fake


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
        nested: bool = False,
        under: Path | None = None,
        restart_as: str | None = None,
        seat_pid: bool = False,
        direct: bool = False,
    ):
        # A None value deletes the key rather than setting it — the ambient
        # environment this test process itself runs under (e.g. when this
        # very suite runs inside a Claude Code session) can already carry
        # CLAUDE_CODE_SESSION_ID, and a bare os.environ.copy() would leak it
        # into a bridge a test means to spawn with no Claude Code identity.
        proc_env = os.environ.copy()
        # A `bg` test bridge would stamp the developer session's own inbox and
        # deliver its gate pushes there; a test that wants one passes `env=`.
        proc_env.pop("CLAUDE_CODE_MESSAGING_SOCKET", None)
        proc_env.pop("REPLD_CODE_MODE", None)
        # A suite run from a seat's kernel inherits the seat's own pid claim.
        proc_env.pop("SUPERVISE_SEAT_PID", None)
        proc_env.pop("REPLD_PRE_EXEC_PPID", None)
        for k, v in (env or {}).items():
            if v is None:
                proc_env.pop(k, None)
            else:
                proc_env[k] = v
        # `nested` puts a non-exec'd shell between this process and the bridge,
        # so its process tree is one level deeper than a plain bridge's.
        # `under` is a copy of sh named `claude`, so the bridge has a claude ancestor.
        # `restart_as` (with `under`) starts the bridge a second time under the
        # same claude once the first dies, carrying that session id in its env:
        # Claude Code restarting a stale MCP server on the same pipes.
        shell = str(under) if under is not None else "sh"
        # `seat_pid` exports the launching shell's pid as a supervise host
        # would export its forked `claude`'s (the bridge's parent).
        script = '"$@"; exit $?'
        if seat_pid:
            script = 'SUPERVISE_SEAT_PID=$$ "$@"; exit $?'
        if restart_as is not None:
            assert under is not None, "restart_as needs under"
            proc_env["RESTART_AS"] = restart_as
            script = '"$@"; CLAUDE_CODE_SESSION_ID="$RESTART_AS" "$@"; exit $?'
        launcher = [shell, "-c", script, "sh"] if nested or under else []
        # `uv run` stays resident above the bridge; a claim needs the bridge's
        # parent to be the launching shell, so run the venv's script directly.
        runner = (
            [str(REPO / ".venv" / "bin" / "repld")]
            if seat_pid or direct
            else ["uv", "run", "--project", str(REPO), "repld"]
        )
        self.proc = subprocess.Popen(
            [
                *launcher,
                *runner,
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

    def handshake(
        self, *, timeout: float = 5.0, capabilities: dict | None = None
    ) -> dict:
        """`initialize` + `notifications/initialized`; returns the initialize response.

        `capabilities` is what this client declares, e.g. `{"elicitation": {}}`.
        """
        params: dict = {"protocolVersion": "2024-11-05"}
        if capabilities is not None:
            params["capabilities"] = capabilities
        resp = self.call("initialize", params, timeout=timeout)
        self.send("notifications/initialized", {}, notif=True)
        return resp

    def respond(self, rid: object, result: dict) -> None:
        """Answer a request the server sent us (`elicitation/create`)."""
        assert self.proc.stdin is not None
        line = json.dumps({"jsonrpc": "2.0", "id": rid, "result": result})
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

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

    def kill_bridge(self) -> None:
        """SIGTERM the bridge under this `claude` (its subtree), not the claude.

        SIGKILL leaves the kernel holding the connection for tens of seconds.
        """

        def descendants(pid: int) -> list[int]:
            try:
                kids = Path(f"/proc/{pid}/task/{pid}/children").read_text().split()
            except OSError:
                return []
            return [c for k in kids for c in (int(k), *descendants(int(k)))]

        for pid in descendants(self.proc.pid):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

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
                # Own process group, so `kill()` reaches the kernel behind `uv run`.
                start_new_session=True,
            )
        self._wait_lockfile()

    @property
    def lock_path(self) -> Path:
        return lock_path_for(self.cwd)

    def _wait_lockfile(self, timeout: float = 10.0) -> None:
        """Return once this kernel's own lockfile is written, else kill it and raise."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._is_own_lock():
                return
            if self.proc.poll() is not None:
                break
            time.sleep(0.1)
        # Read stderr to help debugging
        try:
            log = self.stderr_log.read_text()
        except Exception:
            log = "<no log>"
        self.kill()
        raise RuntimeError(f"kernel never wrote its lockfile. stderr:\n{log}")

    def _is_own_lock(self) -> bool:
        """Whether `kernel.lock` names a process in this kernel's process group.

        A SIGKILLed predecessor leaves its lockfile behind in the same cwd, so
        the file existing says nothing; `self.proc.pid` is the group id.
        """
        try:
            pid = int(json.loads(self.lock_path.read_text())["pid"])
            return os.getpgid(pid) == self.proc.pid
        except OSError, KeyError, TypeError, ValueError:
            return False

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.kill()

    def kill(self) -> None:
        """SIGKILL the wrapper and the kernel behind it, whichever is still up.

        `self.proc` is the `uv run` wrapper, and a SIGKILL sent to it alone
        orphans the kernel; the group outlives a reaped leader.
        """
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.proc.wait()


def content_text(resp: dict) -> str:
    """The first text block of a tools/call result."""
    return resp["result"]["content"][0]["text"]


def assert_eq(got, expected, label: str) -> None:
    if got != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {got!r}")


def assert_true(cond, label: str) -> None:
    if not cond:
        raise AssertionError(f"{label}: condition false")
