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

from phases.browser import (
    phase_6,
    phase_6_capture_filter,
    phase_6_connect_race,
    phase_6_har_redirects,
    phase_6_input_visibility_guard,
    phase_6_key_native_activation,
    phase_6_label_and_reattach,
    phase_6_like_escaping,
    phase_6_offloop_writes,
    phase_6_png_resize,
    phase_6_ready_classification,
    phase_6_reattach_binding,
    phase_6_shadow_dom_selectors,
    phase_6_since_time_base,
    phase_6_tab_close,
    phase_6_tools_and_gists,
)
from phases.channels import phase_4, phase_4_push_kind_args, phase_4b_pregate
from phases.core import phase_3, phase_3_argv_and_registry, phase_3_patch_targets
from phases.dashboard import phase_14_dashboard
from phases.defer import phase_7_defer
from phases.every import phase_10_every
from phases.gates import phase_17_gates
from phases.gist_tools import phase_9_gist_tools
from phases.headless import phase_15_ephemeral_bridge, phase_15_headless
from phases.links import phase_12_gist_links
from phases.lockfile import (
    phase_5,
    phase_5_init,
    phase_5_boot_failure,
    phase_5_evict,
    phase_5_orphans,
    phase_5_permissions,
    phase_5_project_sweep,
    phase_5_sweep,
    phase_5_zombie,
)
from phases.resources import (
    phase_8_doc_surfaces,
    phase_8_gist_resources,
    phase_8_site_signatures,
)
from phases.sessions import phase_13_sessions
from phases.shutdown import phase_11_shutdown
from phases.venv import phase_16_venv_binding

PHASES = {
    3: lambda k: (
        phase_3(k),
        phase_3_argv_and_registry(k),
        phase_3_patch_targets(k),
    ),
    4: lambda k: (phase_4(k), phase_4b_pregate(k), phase_4_push_kind_args(k)),
    5: lambda k: (
        phase_5(k),
        phase_5_init(k),
        phase_5_boot_failure(k),
        phase_5_permissions(k),
        phase_5_zombie(k),
        phase_5_evict(k),
        phase_5_orphans(k),
        phase_5_sweep(k),
        phase_5_project_sweep(k),
    ),
    6: lambda k: (
        phase_6_png_resize(k),
        phase_6_ready_classification(k),
        phase_6_capture_filter(k),
        phase_6_offloop_writes(k),
        phase_6_connect_race(k),
        phase_6_reattach_binding(k),
        phase_6_input_visibility_guard(k),
        phase_6_key_native_activation(k),
        phase_6_since_time_base(k),
        phase_6_har_redirects(k),
        phase_6_like_escaping(k),
        phase_6_tools_and_gists(k),
        phase_6(k),
        phase_6_label_and_reattach(k),
        phase_6_tab_close(k),
        phase_6_shadow_dom_selectors(k),
    ),
    7: phase_7_defer,
    8: lambda k: (
        phase_8_doc_surfaces(k),
        phase_8_site_signatures(k),
        phase_8_gist_resources(k),
    ),
    9: phase_9_gist_tools,
    10: phase_10_every,
    11: phase_11_shutdown,
    12: phase_12_gist_links,
    13: phase_13_sessions,
    14: phase_14_dashboard,
    15: lambda k: (phase_15_headless(k), phase_15_ephemeral_bridge(k)),
    16: phase_16_venv_binding,
    17: phase_17_gates,
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--phase", type=int, default=3, help="highest phase to run (ceiling: 17)"
    )
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="repld-smoketest-"))
    # Isolate the gist registry so test-kernel imports don't pollute the real
    # ~/.config/repld/gist-registry.json with throwaway tempdir paths.
    import os

    os.environ["XDG_CONFIG_HOME"] = str(tmp / "config")
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
        # Runtime state lives outside the project dir now — clean up both.
        from harness import runtime_dir_for

        shutil.rmtree(runtime_dir_for(tmp), ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
