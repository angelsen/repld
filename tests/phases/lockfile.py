"""Phase 5: single-kernel flock mutex, --init file execution, state file modes."""

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

from harness import REPO, Bridge, Kernel, assert_eq, assert_true


def _mode(p: Path) -> str:
    return oct(stat.S_IMODE(p.stat().st_mode))


def phase_5_permissions(kernel: Kernel) -> None:
    """Runtime state is unreadable to other users, on every platform.

    Under $XDG_RUNTIME_DIR this is belt-and-braces (/run/user/N is already
    0700), but the fallback is /tmp/repld-{uid}, where these modes are the only
    thing protecting spill files and the session registry. Enforced at
    RUNTIME_DIR so the assertion holds either way.
    """
    from repld import paths

    pid = json.loads(kernel.lock_path.read_text())["pid"]

    assert_eq(_mode(paths.RUNTIME_DIR), "0o700", f"{paths.RUNTIME_DIR} is 0700")

    session_file = paths.RUNTIME_DIR / "sessions" / f"{pid}.json"
    assert_true(session_file.exists(), f"session file exists ({session_file})")
    assert_eq(_mode(session_file), "0o600", "session file is 0600")

    # Earlier phases exec'd cells that printed, and every cell with output
    # spills from byte 1 — so this kernel has spill files by now.
    spills = list(paths.RUNTIME_DIR.glob(f"{pid}-*.out"))
    assert_true(spills, f"kernel {pid} wrote at least one spill file")
    for s in spills:
        assert_eq(_mode(s), "0o600", f"spill file {s.name} is 0600")

    print(f"  ✓ runtime state private: dir 0700, session + {len(spills)} spill(s) 0600")


def phase_5(kernel: Kernel) -> None:
    """A second kernel in the same cwd loses the flock and stands down."""
    before = json.loads(kernel.lock_path.read_text())
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
    # Exit 0, not an error: a racing bridge should just use the incumbent.
    assert_eq(proc.returncode, 0, "second kernel exits 0")
    combined = (proc.stdout or "") + (proc.stderr or "")
    assert_true(
        "already running" in combined,
        f"second-kernel notice mentions 'already running' (got: {combined!r})",
    )
    after = json.loads(kernel.lock_path.read_text())
    assert_eq(after["pid"], before["pid"], "winner's lockfile left intact")
    print("  ✓ flock mutex: second kernel stood down, incumbent untouched")


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
