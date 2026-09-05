"""Phase 16: project-venv binding — detection, version-gated adopt, refusal.

The rule under test is asymmetric on purpose. A venv whose Python minor version
matches this interpreter can be spliced onto sys.path at runtime; one that
doesn't must be left strictly alone, because a mismatched venv's compiled
extensions are built for a single version while its pure-Python and abi3
packages import anywhere. Splicing it anyway *half*-works, which is worse to
debug than a clean refusal — so the refusal assertion is the important one here.
"""

import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
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
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=180, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (root / ".venv") if r.returncode == 0 else None


def _systemd_spawn_argv(tmp: Path) -> None:
    """The systemd-run invocation, asserted without creating any units."""
    from repld import paths, spawn

    a, b = tmp / "alpha", tmp / "beta"
    a_sock, b_sock = paths.socket_path(a), paths.socket_path(b)
    assert_true(
        spawn._systemd_unit_name(a_sock) != spawn._systemd_unit_name(b_sock),
        "different projects get different unit names",
    )
    assert_eq(
        spawn._systemd_unit_name(a_sock),
        spawn._systemd_unit_name(a_sock),
        "and the same socket is stable across calls",
    )
    assert_true(
        paths.project_slug(a) in spawn._systemd_unit_name(a_sock),
        "unit name reuses the project slug for the default per-cwd socket",
    )
    # The bug this keys off the socket rather than cwd to fix: two --socket
    # overrides sharing a cwd must not collapse onto one unit, or the second
    # spawn reads the first kernel's unit as its own incumbent and never
    # actually starts.
    assert_true(
        spawn._systemd_unit_name(a / "one.sock")
        != spawn._systemd_unit_name(a / "two.sock"),
        "same cwd, different --socket overrides still get different units",
    )

    env = {
        "PATH": "/usr/bin",
        "REPLD_SOCKET": "/tmp/x.sock",
        "INVOCATION_ID": "leaked",
        "LISTEN_FDS": "3",
    }
    argv = spawn._systemd_run_argv(["python", "-m", "repld"], a_sock, a, env)
    setenv = [x for x in argv if x.startswith("--setenv=")]
    assert_true(
        f"--unit={spawn._systemd_unit_name(a_sock)}" in argv,
        f"unit comes from the socket, not cwd ({argv})",
    )
    assert_true(f"--working-directory={a}" in argv, f"cwd is the project ({argv})")
    assert_true("--collect" in argv, "failed units don't squat the name")
    assert_true("--setenv=PATH=/usr/bin" in setenv, "ordinary vars pass through")
    assert_true("--setenv=REPLD_SOCKET=/tmp/x.sock" in setenv, "so does REPLD_SOCKET")
    assert_true(
        not any("INVOCATION_ID" in s or "LISTEN_FDS" in s for s in setenv),
        f"systemd's own unit vars aren't forwarded (got {setenv})",
    )
    assert_eq(argv[-3:], ["python", "-m", "repld"], "the command comes last")

    assert_true(
        not any("MemoryHigh" in x for x in argv),
        "no memory ceiling unless asked for",
    )
    assert_true(
        not any("OOMScoreAdjust" in x for x in argv),
        "and the manager's OOM policy is left alone unless asked",
    )
    tuned = spawn._systemd_run_argv(
        ["python"],
        a_sock,
        a,
        {**env, "REPLD_MEMORY_HIGH": "4G", "REPLD_OOM_SCORE_ADJUST": "100"},
    )
    assert_true("MemoryHigh=4G" in tuned, f"REPLD_MEMORY_HIGH applied (got {tuned})")
    assert_true("OOMScoreAdjust=100" in tuned, "REPLD_OOM_SCORE_ADJUST applied")
    print("  ✓ systemd-run argv: per-project unit, cwd, env filtered, opt-in limits")


def _systemd_spawn_live(tmp: Path) -> None:
    """A real kernel lands in its own cgroup, not the caller's.

    The whole point of the change: `start_new_session` detaches from the
    terminal but leaves the process in the spawner's cgroup and parented to it.
    """
    from repld import spawn

    if shutil.which("systemd-run") is None or not os.environ.get("XDG_RUNTIME_DIR"):
        print("  ⚠ no systemd-run / XDG_RUNTIME_DIR — skipping live unit check")
        return
    probe = subprocess.run(
        ["systemctl", "--user", "is-system-running"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0 and "running" not in probe.stdout:
        print(f"  ⚠ user manager not usable ({probe.stdout.strip()}) — skipping")
        return

    proj = tmp / "unitproj"
    proj.mkdir()
    sock = proj / "k.sock"
    unit = spawn._systemd_unit_name(sock)
    orig_cwd = os.getcwd()
    try:
        os.chdir(proj)
        assert_eq(
            spawn.spawn_headless(sock),
            spawn.STARTED,
            "kernel spawned via systemd-run",
        )
        pid = 0
        for _ in range(150):
            try:
                pid = int(json.loads((proj / "k.lock").read_text())["pid"])
                break
            except (OSError, ValueError, KeyError):
                time.sleep(0.2)
        assert_true(pid > 0, "kernel wrote its lockfile")

        cgroup = Path(f"/proc/{pid}/cgroup").read_text().strip()
        assert_true(unit in cgroup, f"kernel is in its own unit cgroup (got {cgroup})")
        caller_cg = Path(f"/proc/{os.getpid()}/cgroup").read_text().strip()
        assert_true(cgroup != caller_cg, "...which is not the caller's cgroup")
        ppid = int(Path(f"/proc/{pid}/stat").read_text().split(") ")[1].split()[1])
        assert_true(ppid != os.getpid(), f"reparented away from the spawner ({ppid})")
        print("  ✓ kernel runs in its own systemd unit cgroup, reparented")

        # --- a second spawn is a racing boot, not a failure. This must come
        # from asking the manager: systemd words a taken unit name as "was
        # already loaded or has a fragment file", so the obvious string match
        # on "already exists" never fires and would silently double-spawn. ---
        assert_eq(
            spawn._spawn_via_systemd(spawn._kernel_argv(sock), sock, proj),
            spawn.INCUMBENT,
            "a taken unit name reads as a racing boot",
        )
        # INCUMBENT, not FAILED: no second kernel is started behind systemd's
        # back, but one *is* coming, and the caller must poll rather than treat
        # this as fatal. `repld restart` used to return 1 here, silently.
        assert_eq(
            spawn.spawn_headless(sock),
            spawn.INCUMBENT,
            "a racing boot reads as an incumbent to adopt, not a failure",
        )
        assert_eq(
            int(json.loads((proj / "k.lock").read_text())["pid"]),
            pid,
            "and the incumbent is untouched",
        )
        print("  ✓ racing boot adopts the incumbent instead of double-spawning")
    finally:
        os.chdir(orig_cwd)
        subprocess.run(
            ["systemctl", "--user", "stop", unit],
            capture_output=True,
            timeout=30,
            check=False,
        )
        subprocess.run(
            ["systemctl", "--user", "reset-failed"], capture_output=True, check=False
        )


def _stop_restart_race(tmp: Path) -> None:
    """`_wait_gone` must not declare a stop finished while the unit is still
    tearing down — the race `repld restart` hit as a false "new kernel never
    came up": pid dead and lockfile gone, but systemd's own reap-and-collect
    lagging behind, so a same-named `systemd-run` right after read as an
    INCUMBENT racing boot rather than the kernel just killed.
    """
    from repld import lifecycle_cmd, spawn

    proj = tmp / "raceproj"
    proj.mkdir()
    sock = proj / "k.sock"
    # A spawned-then-reaped pid, not a live one: pid_alive and the missing
    # lockfile are both already satisfied on tick one, so only a fake
    # unit_settled can be gating the loop below.
    proc = subprocess.Popen(["true"])
    proc.wait()

    states = iter(["deactivating", "deactivating", "inactive"])
    calls = []

    def fake_unit_settled(sp: Path) -> bool:
        calls.append(sp)
        return next(states, "inactive") == "inactive"

    orig = spawn.unit_settled
    spawn.unit_settled = fake_unit_settled
    try:
        assert_true(
            lifecycle_cmd._wait_gone(
                proc.pid, proj / "nonexistent.lock", sock, timeout=5.0
            ),
            "_wait_gone waits out a still-deactivating unit before declaring done",
        )
        assert_true(len(calls) >= 3, "unit_settled was actually polled, not skipped")
    finally:
        spawn.unit_settled = orig
    print("  ✓ stop/restart race: _wait_gone holds for a deactivating unit")


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

        # `rebind_exec` documents that failures degrade to an unbound kernel,
        # and it runs before `repld bridge` does anything — so malformed
        # direct_url.json metadata must return None here, not take the MCP
        # server down at startup.
        from repld import relaunch

        class _FakeDist:
            def __init__(self, raw):
                self._raw = raw

            def read_text(self, _name):
                if isinstance(self._raw, Exception):
                    raise self._raw
                return self._raw

        orig_dist = importlib.metadata.distribution
        try:
            for label, raw in [
                ("corrupt json", "{not json"),
                ("not an object", "[1, 2, 3]"),
                ("dir_info missing", '{"url": "file:///x"}'),
                ("dir_info wrong type", '{"dir_info": "yes", "url": "file:///x"}'),
                ("url missing", '{"dir_info": {"editable": true}}'),
                ("url not a string", '{"dir_info": {"editable": true}, "url": 7}'),
                ("unreadable", OSError("boom")),
                ("empty", ""),
            ]:
                importlib.metadata.distribution = lambda _n, r=raw: _FakeDist(r)
                assert_eq(relaunch._editable_path(), None, f"_editable_path: {label}")
            # ...and a valid one still resolves, so this isn't None-always.
            importlib.metadata.distribution = lambda _n: _FakeDist(
                '{"dir_info": {"editable": true}, "url": "file:///home/x/repld"}'
            )
            assert_eq(
                relaunch._editable_path(), "/home/x/repld", "_editable_path: valid"
            )
        finally:
            importlib.metadata.distribution = orig_dist
        print("  ✓ _editable_path degrades on bad metadata instead of raising")

        # --- the rebind must not silently drop the browser extra ---
        # `uv run --with` builds a fresh environment from the spec it is given;
        # the interpreter being left behind contributes nothing to it. So a
        # repld installed *with* the extra used to re-exec into an environment
        # that had none of it — no `browser` builtin, no browser MCP tools, and
        # a `NameError` in a cell as the only symptom. The shape of the bug is
        # the tell and is what this asserts against: it fired only in projects
        # that have a `./.venv`, because only those rebind at all, so browser
        # worked in some projects on a machine and not others for a reason
        # nothing surfaced. Conditional on what we already have, so a repld
        # installed without the extra doesn't start resolving duckdb by itself.
        orig_has_extra = bind.has_browser_extra
        try:
            importlib.metadata.distribution = lambda _n: _FakeDist(
                '{"dir_info": {"editable": true}, "url": "file:///home/x/repld"}'
            )
            for present, expected in [(True, "[browser]"), (False, "")]:
                bind.has_browser_extra = lambda _p=present: _p
                argv = bind.uv_run_argv(["bridge"])
                assert argv is not None
                assert_eq(
                    argv[argv.index("--with-editable") + 1],
                    f"/home/x/repld{expected}",
                    f"editable rebind carries the extra: present={present}",
                )

                # The published-package branch is a separate string and was
                # wrong in the same way.
                def _not_found(_n):
                    raise importlib.metadata.PackageNotFoundError("repld-tool")

                importlib.metadata.distribution = _not_found
                argv = bind.uv_run_argv(["bridge"])
                assert argv is not None
                assert_eq(
                    argv[argv.index("--with") + 1],
                    f"repld-tool{expected}",
                    f"published rebind carries the extra: present={present}",
                )
                importlib.metadata.distribution = lambda _n: _FakeDist(
                    '{"dir_info": {"editable": true}, "url": "file:///home/x/repld"}'
                )
            # And the real predicate answers from *this* interpreter rather
            # than from a constant — the smoketest runs with the extra when
            # Chrome-backed phase 6 does, and without it otherwise, so only
            # its agreement with an actual import is assertable here.
            bind.has_browser_extra = orig_has_extra
            importable = True
            try:
                # I001: align-comments.py column-aligns these noqa comments,
                # which isort reads as unformatted; alignment wins by design.
                import duckdb as _d          # noqa: F401, I001
                import websockets as _w      # noqa: F401
                from PIL import Image as _i  # noqa: F401
            except ImportError:
                importable = False
            assert_eq(
                bind.has_browser_extra(),
                importable,
                "has_browser_extra agrees with a real import of all three",
            )
        finally:
            bind.has_browser_extra = orig_has_extra
            importlib.metadata.distribution = orig_dist
        print("  ✓ rebind carries the browser extra it already has, and only then")

        _systemd_spawn_argv(tmp)
        _systemd_spawn_live(tmp)
        _stop_restart_race(tmp)
    finally:
        os.chdir(orig_cwd)
        if orig_virtual_env is None:
            os.environ.pop("VIRTUAL_ENV", None)
        else:
            os.environ["VIRTUAL_ENV"] = orig_virtual_env
        sys.path[:] = orig_path
        shutil.rmtree(tmp, ignore_errors=True)
