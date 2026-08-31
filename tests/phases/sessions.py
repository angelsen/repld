"""Phase 13: Session registry — register on boot, visible in list, removed on shutdown."""

import json
import os
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path

from harness import REPO, Bridge, Kernel, assert_eq, assert_true


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

        _test_unusable_entry_skipped(b, kernel.cwd)
    finally:
        b.close()

    _test_unregister_on_shutdown()


# A session file whose payload survives json.loads but carries no usable pid.
# list_sessions() falls back to the *filename* pid to decide liveness, and used
# to hand the parsed payload back anyway — so every consumer that indexes
# `s["pid"]` (repld status, repld stop --all, the dashboard sidebar) died on it.
_UNUSABLE = [
    ('{"cwd": "/nowhere"}', "no pid key"),
    ('{"pid": null, "cwd": "/nowhere"}', "null pid"),
    ("5", "not an object"),
]


def _test_unusable_entry_skipped(b: Bridge, cwd: Path) -> None:
    """A corrupt session file is skipped, not returned half-parsed.

    Planted under a pid that is alive but is not a kernel (this test runner),
    which is the case that reaches the fallback *and* keeps the file: a dead
    pid's file gets unlinked, so it could never be handed to a caller anyway.
    """
    planted = _sessions_dir() / f"{os.getpid()}.json"
    try:
        for body, label in _UNUSABLE:
            planted.write_text(body)

            resp = b.call(
                "tools/call",
                {
                    "name": "exec",
                    "arguments": {
                        "code": (
                            "from repld import sessions as _s\n"
                            "print([type(x).__name__ + ':' + str(x.get('pid'))\n"
                            "       if isinstance(x, dict) else type(x).__name__\n"
                            "       for x in _s.list_sessions()])"
                        )
                    },
                },
            )
            got = resp["result"]["content"][0]["text"].strip()
            assert_true(
                "None" not in got and "int" not in got,
                f"list_sessions() skipped the {label} entry (got {got!r})",
            )
            assert_true(
                planted.exists(),
                f"{label}: live pid's file kept, not unlinked (only the payload is dropped)",
            )

            # The real crash surface: status indexes s["pid"] over every sibling.
            out = subprocess.run(
                ["uv", "run", "--project", str(REPO), "repld", "status", "--json"],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            assert_eq(
                out.returncode, 0, f"repld status survives a {label} session file"
            )
            json.loads(out.stdout)  # and still emits well-formed JSON
    finally:
        planted.unlink(missing_ok=True)
    print("  ✓ unusable session entries skipped; status/stop don't choke")


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
