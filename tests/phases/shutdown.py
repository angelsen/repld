"""Phase 11: graceful shutdown — _shutdown drains loop tasks before stopping.

Each subtest spawns its own kernel because we send SIGTERM and observe
the result. The shared `kernel` argument from smoketest.py is unused.
"""

import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from harness import Bridge, Kernel, assert_true


def phase_11_shutdown(kernel: Kernel) -> None:
    del kernel  # shared kernel intentionally unused; subtests own their kernels
    _test_clean_drain()
    _test_budget_enforcement()


def _spawn() -> tuple[Path, Kernel, Bridge]:
    tmp = Path(tempfile.mkdtemp(prefix="repld-phase11-"))
    k = Kernel(tmp)
    b = Bridge(tmp)
    b.call("initialize", {"protocolVersion": "2024-11-05"})
    b.send("notifications/initialized", {}, notif=True)
    return tmp, k, b


def _teardown(tmp: Path, kernel: Kernel, bridge: Bridge) -> None:
    bridge.close()
    if kernel.proc.poll() is None:
        kernel.proc.kill()
    shutil.rmtree(tmp, ignore_errors=True)


def _test_clean_drain() -> None:
    """@every and defer try/finally blocks run when the kernel is SIGTERM'd."""
    tmp, kernel, bridge = _spawn()
    every_witness = tmp / "every.witness"
    defer_witness = tmp / "defer.witness"
    try:
        code = (
            "import asyncio\n"
            "@every(60, label='witness_every')\n"
            "def _w():\n"
            "    try: pass\n"
            "    finally:\n"
            f"        open({str(every_witness)!r}, 'a').write('every-cleaned\\n')\n"
            "async def _slow():\n"
            "    try:\n"
            "        await asyncio.sleep(3600)\n"
            "    finally:\n"
            f"        open({str(defer_witness)!r}, 'a').write('defer-cleaned\\n')\n"
            "defer(_slow(), label='witness_defer')\n"
        )
        resp = bridge.call(
            "tools/call",
            {"name": "exec", "arguments": {"code": code}},
            timeout=5.0,
        )
        assert_true(
            not resp["result"].get("isError", False),
            f"register witness tasks: {resp['result']['content'][0]['text']!r}",
        )
        # Let the first @every tick run and defer schedule onto the loop.
        time.sleep(0.5)

        t0 = time.monotonic()
        kernel.proc.send_signal(signal.SIGTERM)
        try:
            kernel.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            kernel.proc.kill()
            raise AssertionError("kernel did not exit within 5s of SIGTERM")
        dt = time.monotonic() - t0

        assert_true(
            every_witness.exists() and "every-cleaned" in every_witness.read_text(),
            f"@every try/finally ran on shutdown (witness={every_witness})",
        )
        assert_true(
            defer_witness.exists() and "defer-cleaned" in defer_witness.read_text(),
            f"defer try/finally ran on shutdown (witness={defer_witness})",
        )
        # Clean drain should be near-instant (well under the 2s budget).
        assert_true(
            dt < 4.0,
            f"clean drain finished promptly (got {dt:.2f}s, budget 2s)",
        )
        print(f"  ✓ shutdown: @every + defer try/finally ran (drain {dt:.2f}s)")
    finally:
        _teardown(tmp, kernel, bridge)


def _test_budget_enforcement() -> None:
    """A blocked finally can't hang shutdown beyond the 2s drain budget."""
    tmp, kernel, bridge = _spawn()
    try:
        # Sync time.sleep in the ticker body blocks the loop; drain coroutine
        # gets queued but can't progress, so the 2s budget should fire.
        code = (
            "import time\n"
            "@every(60, label='stuck')\n"
            "def _stuck():\n"
            "    try: pass\n"
            "    finally:\n"
            "        time.sleep(60)\n"
        )
        resp = bridge.call(
            "tools/call",
            {"name": "exec", "arguments": {"code": code}},
            timeout=5.0,
        )
        assert_true(
            not resp["result"].get("isError", False),
            "register stuck ticker",
        )
        time.sleep(0.5)

        t0 = time.monotonic()
        kernel.proc.send_signal(signal.SIGTERM)
        try:
            kernel.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            kernel.proc.kill()
            raise AssertionError("kernel hung past 10s — budget did not fire")
        dt = time.monotonic() - t0

        # Budget is 2s; allow some slack for signal delivery and final teardown.
        assert_true(
            1.5 < dt < 5.0,
            f"drain budget enforced (got {dt:.2f}s, expected ~2-3s)",
        )
        print(f"  ✓ shutdown: stuck finally bypassed by 2s budget ({dt:.2f}s)")
    finally:
        _teardown(tmp, kernel, bridge)
