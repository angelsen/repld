"""Phase 5: Lockfile conflict detection, --init file execution."""

import os
import shutil
import subprocess
from pathlib import Path

from harness import REPO, Bridge, Kernel, assert_true


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
