"""End-to-end smoketest.

Starts a kernel in a tempdir, opens a bridge subprocess, drives MCP JSON-RPC
over its stdio, verifies responses. Grows phase-by-phase alongside the
implementation.

Usage:  uv run tests/smoketest.py [--phase N]
"""

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

# Ensure tests/ is on sys.path so phase modules can `from harness import ...`
sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Kernel

from phases.browser import phase_6, phase_6_tools_and_gists
from phases.channels import phase_4, phase_4b_pregate
from phases.core import phase_3
from phases.defer import phase_7_defer
from phases.gist_tools import phase_9_gist_tools
from phases.lockfile import phase_5, phase_5_init
from phases.resources import phase_8_gist_resources

PHASES = {
    3: phase_3,
    4: lambda k: (phase_4(k), phase_4b_pregate(k)),
    5: lambda k: (phase_5(k), phase_5_init(k)),
    6: lambda k: (phase_6_tools_and_gists(k), phase_6(k)),
    7: phase_7_defer,
    8: phase_8_gist_resources,
    9: phase_9_gist_tools,
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
