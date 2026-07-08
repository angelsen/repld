"""Phase 13: Session registry — register on boot, visible in list, removed on shutdown."""

import json
import os
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path

from harness import Bridge, Kernel, assert_true


def _sessions_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
    return Path(base) / "repld" / "sessions"


def _kernel_pid(bridge: Bridge) -> int:
    resp = bridge.call(
        "tools/call",
        {"name": "exec", "arguments": {"code": "print(__import__('os').getpid())"}},
    )
    content = resp["result"]["content"][0]["text"]
    return int(content.strip())


def phase_13_sessions(kernel: Kernel) -> None:
    """Session file exists while the kernel runs and shows up in list_sessions()."""
    b = Bridge(kernel.cwd)
    try:
        b.call("initialize", {"protocolVersion": "2024-11-05"})
        b.send("notifications/initialized", {}, notif=True)

        pid = _kernel_pid(b)
        session_file = _sessions_dir() / f"{pid}.json"
        assert_true(session_file.exists(), f"session file exists at {session_file}")

        info = json.loads(session_file.read_text())
        assert_true(info["pid"] == pid, "session file pid matches kernel pid")
        assert_true(
            Path(info["cwd"]).resolve() == kernel.cwd.resolve(),
            f"session file cwd matches kernel cwd (got {info['cwd']!r})",
        )
        print(f"  ✓ session file written: {session_file}")

        resp = b.call(
            "tools/call",
            {
                "name": "exec",
                "arguments": {
                    "code": (
                        "from repld import sessions as _s\n"
                        "print([s['pid'] for s in _s.list_sessions()])"
                    )
                },
            },
        )
        content = resp["result"]["content"][0]["text"]
        assert_true(
            str(pid) in content,
            f"list_sessions() includes running kernel pid (got {content!r})",
        )
        print("  ✓ list_sessions() includes this kernel")
    finally:
        b.close()

    _test_unregister_on_shutdown()


def _test_unregister_on_shutdown() -> None:
    """Session file is removed (atexit) once the kernel is SIGTERM'd."""
    tmp = Path(tempfile.mkdtemp(prefix="repld-phase13-"))
    k = Kernel(tmp)
    try:
        b = Bridge(tmp)
        try:
            b.call("initialize", {"protocolVersion": "2024-11-05"})
            b.send("notifications/initialized", {}, notif=True)
            pid = _kernel_pid(b)
        finally:
            b.close()

        session_file = _sessions_dir() / f"{pid}.json"
        assert_true(session_file.exists(), "session file exists before shutdown")

        k.proc.send_signal(signal.SIGTERM)
        try:
            k.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            k.proc.kill()
            raise AssertionError("kernel did not exit within 5s of SIGTERM")

        assert_true(
            not session_file.exists(),
            f"session file removed after shutdown ({session_file})",
        )
        print("  ✓ session file removed on shutdown")
    finally:
        if k.proc.poll() is None:
            k.proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)
