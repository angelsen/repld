"""Phase 16: project-venv binding — detection, version-gated adopt, refusal.

The rule under test is asymmetric on purpose. A venv whose Python minor version
matches this interpreter can be spliced onto sys.path at runtime; one that
doesn't must be left strictly alone, because a mismatched venv's compiled
extensions are built for a single version while its pure-Python and abi3
packages import anywhere. Splicing it anyway *half*-works, which is worse to
debug than a clean refusal — so the refusal assertion is the important one here.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from harness import Kernel, assert_eq, assert_true

from repld import bind


def _other_minor() -> str:
    """A Python minor version that is not the one running the tests."""
    return "3.12" if sys.version_info[:2] != (3, 12) else "3.13"


def _make_venv(root: Path, python: str | None = None) -> Path | None:
    """Build a real venv under `root`. None if uv can't provide that Python."""
    cmd = ["uv", "venv", "--quiet"]
    if python:
        cmd += ["--python", python]
    cmd.append(str(root / ".venv"))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (root / ".venv") if r.returncode == 0 else None


def phase_16_venv_binding(_kernel: Kernel) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="repld-venv-"))
    orig_cwd = os.getcwd()
    orig_path = list(sys.path)
    orig_virtual_env = os.environ.get("VIRTUAL_ENV")
    try:
        if shutil.which("uv") is None:
            print("  ⚠ uv not on PATH — skipping phase 16")
            return

        # The smoketest itself runs under `uv run`, so VIRTUAL_ENV is set to
        # *this repo's* venv. Clear it so the fixtures decide what's found.
        os.environ.pop("VIRTUAL_ENV", None)

        # --- detection: a directory is only a venv with a pyvenv.cfg ---
        (tmp / "notavenv" / ".venv").mkdir(parents=True)
        assert_eq(
            bind.project_venv(tmp / "notavenv"), None, "no pyvenv.cfg → not a venv"
        )

        same = _make_venv(tmp)
        assert_true(same is not None, "uv venv built a fixture venv")
        assert same is not None
        assert_eq(bind.project_venv(tmp), same, "./.venv found from cwd")
        assert_eq(
            bind.venv_python_version(same),
            sys.version_info[:2],
            "pyvenv.cfg version parsed",
        )
        assert_true(bind.site_packages(same) is not None, "site-packages located")
        print("  ✓ project_venv / venv_python_version / site_packages")

        # --- ./.venv beats an ambient $VIRTUAL_ENV. Activating one project's
        # venv and then opening a different project must not bind this kernel
        # to the first project's packages — cwd is what identifies a project
        # everywhere else in repld, so it decides here too. ---
        elsewhere = tmp / "elsewhere"
        elsewhere.mkdir()
        other = _make_venv(elsewhere)
        assert other is not None
        os.environ["VIRTUAL_ENV"] = str(other)
        assert_eq(bind.project_venv(tmp), same, "./.venv beats a foreign VIRTUAL_ENV")
        assert_eq(
            bind.project_venv(tmp / "notavenv"),
            other,
            "$VIRTUAL_ENV is still the fallback when there is no ./.venv",
        )
        os.environ.pop("VIRTUAL_ENV", None)
        print("  ✓ ./.venv wins over ambient $VIRTUAL_ENV, which still backstops")

        # --- same version: adopt splices it on and imports resolve ---
        sp = bind.site_packages(same)
        assert sp is not None
        (sp / "repld_venv_probe.py").write_text("VALUE = 41\n")
        assert_true(not bind.is_bound(same), "not bound before adopt")
        added = bind.adopt(same)
        assert_eq(added, sp, "adopt returned the site-packages it added")
        assert_true(bind.is_bound(same), "is_bound true after adopt")
        import repld_venv_probe  # pyright: ignore[reportMissingImports]

        assert_eq(repld_venv_probe.VALUE, 41, "package from the venv imported")
        sys.modules.pop("repld_venv_probe", None)
        sys.path.remove(str(sp))
        print("  ✓ version-matched venv adopted; its packages import")

        # --- different version: sys.path must come back untouched ---
        mixed = tmp / "mixed"
        mixed.mkdir()
        cross = _make_venv(mixed, python=_other_minor())
        if cross is None:
            print(f"  ⚠ Python {_other_minor()} unavailable — skipping refusal check")
        else:
            assert_true(
                not bind.version_matches(cross),
                f"fixture really is a different minor ({_other_minor()})",
            )
            before = list(sys.path)
            assert_eq(bind.adopt(cross), None, "adopt refuses a mismatched venv")
            assert_eq(sys.path, before, "sys.path untouched by the refusal")
            assert_true(not bind.is_bound(cross), "and it is not reported as bound")
            described = bind.describe(cross)
            assert_true("not bound" in described, f"describe() says so: {described!r}")
            print("  ✓ cross-version venv refused, sys.path left alone")

        # --- the sentinel is what stops `uv run … repld` re-entering forever ---
        os.chdir(tmp)
        os.environ[bind.SENTINEL] = "1"
        try:
            bind.rebind_exec(["bridge"])  # returns only because the sentinel is set
            print("  ✓ REPLD_BOUND short-circuits the re-exec (fork-bomb guard)")
        finally:
            os.environ.pop(bind.SENTINEL, None)

        # --- and the command it would have run targets the project ---
        cmd = bind.uv_run_argv(["bridge"])
        assert_true(cmd is not None, "uv_run_argv built a command")
        assert cmd is not None
        assert_eq(cmd[1:3], ["run", "--with-editable"], f"uses uv run (got {cmd})")
        assert_eq(cmd[-2:], ["repld", "bridge"], "re-runs the same subcommand")
        print("  ✓ uv_run_argv targets a local checkout, preserving argv")
    finally:
        os.chdir(orig_cwd)
        if orig_virtual_env is None:
            os.environ.pop("VIRTUAL_ENV", None)
        else:
            os.environ["VIRTUAL_ENV"] = orig_virtual_env
        sys.path[:] = orig_path
        shutil.rmtree(tmp, ignore_errors=True)
