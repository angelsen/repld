"""Phase 5: single-kernel flock mutex, repld_init.py bootstrap, state file modes."""

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

    # The socket is the one node created by bind() rather than by open_private
    # / atomic_write_json, so it takes a umask to get the same mode with no
    # window — chmod-ing after the bind leaves it connectable in between.
    sock = paths.socket_path(kernel.cwd)
    assert_true(sock.is_socket(), f"kernel socket exists ({sock})")
    assert_eq(_mode(sock), "0o600", "IPC socket is 0600")
    assert_eq(_mode(kernel.lock_path), "0o600", "kernel.lock is 0600")
    assert_eq(_mode(paths.cache_for(sock)), "0o600", "kernel.cache is 0600")
    print("  ✓ socket + lock + cache all 0600, no post-create chmod window")


def _a_dead_pid() -> int:
    """A pid that is definitely not running, for the sweep sentinel."""
    from repld import state

    for candidate in range(4_000_000, 3_900_000, -1):
        if not state.pid_alive(candidate):
            return candidate
    raise RuntimeError("could not find a dead pid to test with")


def phase_5_zombie(_kernel: Kernel) -> None:
    """A zombie is not alive, however cheerfully os.kill(pid, 0) answers.

    repld manufactures these: `spawn.spawn_headless` never waits on the kernel
    it starts, so a kernel that dies before its bridge sits unreaped. Counting
    one as alive made `repld stop` report a false timeout and kept the boot
    sweep off its spill files.
    """
    import time

    from repld import state

    proc = subprocess.Popen(["true"])  # exits immediately; deliberately not reaped
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not state._is_zombie(proc.pid):
            time.sleep(0.02)
        if not state._is_zombie(proc.pid):
            # No procfs (non-Linux), or it got reaped out from under us.
            print("  ~ zombie check skipped (could not produce one to test)")
            return
        assert_true(
            os.kill(proc.pid, 0) is None,
            "os.kill(pid, 0) still succeeds for the zombie (the trap)",
        )
        assert_eq(state.pid_alive(proc.pid), False, "pid_alive() sees through it")
    finally:
        proc.wait()
    assert_eq(state.pid_alive(proc.pid), False, "still dead once reaped")
    print("  ✓ pid_alive: a zombie reads as dead, not alive")


def phase_5_evict(_kernel: Kernel) -> None:
    """A live kernel reclaims its own spills; the boot sweep can't do it for it.

    `sweep_dead_pid_files` only touches files a *dead* pid owns, so a kernel
    meant to run for weeks has to unlink its own — one file per output-producing
    cell, in tmpfs. Evicting the task entry without the unlink would make them
    unreclaimable until the process exits.
    """
    import time

    from repld import tasks

    task_id, task = tasks.new_task()
    tasks._open_spill(task, task_id).write("evict me")
    spill = Path(task["spill_path"])
    assert_true(spill.is_file(), f"spill written ({spill.name})")

    tasks.finalize(task_id)
    tasks._prune_spill_files()
    assert_true(spill.is_file(), "a just-finished task keeps its spill")
    assert_true(tasks.get(task_id) is not None, "and keeps its registry entry")

    # Backdate past the retention window rather than sleeping an hour.
    task["done_at"] = time.monotonic() - tasks._EVICT_AGE - 1
    tasks._prune_spill_files()
    assert_eq(tasks.get(task_id), None, "evicted task entry dropped")
    assert_true(not spill.exists(), "evicted task's spill file unlinked")

    print(f"  ✓ spill eviction: entry + file reclaimed after {tasks._EVICT_AGE:.0f}s")


def phase_5_orphans(_kernel: Kernel) -> None:
    """The same bound applies to runtime files that never get a task entry.

    `spill_text` (oversized resource reads, big browser tool responses) and
    `Tab.screenshot` both write `{pid}-…` into RUNTIME_DIR without going through
    the task registry, so `_prune_spill_files`'s loop cannot see them and the
    boot sweep only collects *dead* pids. They used to live as long as the
    kernel did.
    """
    import os
    import time

    from repld import paths, tasks

    paths.ensure_runtime_dir()
    mine = os.getpid()
    stale = paths.RUNTIME_DIR / f"{mine}-network-deadbeef.out"
    fresh = paths.RUNTIME_DIR / f"{mine}-screenshot-9222-abc-1700000000.png"
    stale.write_text("an old resource spill")
    fresh.write_bytes(b"a screenshot taken just now")
    old = time.time() - tasks._EVICT_AGE - 60
    os.utime(stale, (old, old))

    # A live task whose spill file is old on disk but whose entry is young: the
    # registry owns its lifetime, and the sweep must not race it.
    task_id, task = tasks.new_task()
    tasks._open_spill(task, task_id).write("still owned")
    owned = Path(task["spill_path"])
    os.utime(owned, (old, old))

    try:
        tasks._prune_spill_files()
        assert_true(not stale.exists(), "orphaned resource spill reclaimed")
        assert_true(fresh.exists(), "a young orphan is left alone")
        assert_true(owned.is_file(), "a live task's spill survives on age alone")
    finally:
        for p in (stale, fresh, owned):
            p.unlink(missing_ok=True)
        tasks._tasks.pop(task_id, None)

    print(f"  ✓ orphan sweep: untracked {mine}-* files reclaimed at _EVICT_AGE")


def phase_5_sweep(kernel: Kernel) -> None:
    """A booting kernel reclaims what dead kernels left in RUNTIME_DIR.

    Nothing runs on the way out of a SIGKILL, so per-process scratch (task
    spills, resource spills, screenshots) can only be collected by whoever
    boots next. It is all named `{pid}-…` so one liveness check covers it.
    """
    import tempfile as _tmp

    from repld import paths

    dead = _a_dead_pid()
    sentinels = [
        paths.RUNTIME_DIR / f"{dead}-sweeptest.out",
        paths.RUNTIME_DIR / f"{dead}-screenshot-9222-abc123-1700000000.png",
    ]
    for s in sentinels:
        s.write_text("stale")

    live_pid = json.loads(kernel.lock_path.read_text())["pid"]
    live_spills = set(paths.RUNTIME_DIR.glob(f"{live_pid}-*.out"))
    assert_true(live_spills, f"live kernel {live_pid} has spills to protect")

    tmp = Path(_tmp.mkdtemp(prefix="repld-sweep-"))
    try:
        fresh = Kernel(tmp)
        try:
            for s in sentinels:
                assert_true(not s.exists(), f"swept dead pid's {s.name}")
            survivors = set(paths.RUNTIME_DIR.glob(f"{live_pid}-*.out"))
            assert_true(
                live_spills <= survivors,
                f"live kernel {live_pid}'s own spills untouched by the sweep",
            )
        finally:
            fresh.stop()
    finally:
        for s in sentinels:
            s.unlink(missing_ok=True)
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"  ✓ boot sweep: dropped dead pid {dead}'s files, kept the live kernel's")


def phase_5_project_sweep(kernel: Kernel) -> None:
    """A booting kernel reclaims a dead kernel's whole PROJECTS_DIR/<slug>/, too.

    `phase_5_sweep` covers the flat `{pid}-*` files in RUNTIME_DIR;
    `sweep_dead_pid_files` never reached the per-project directory sitting
    one level up, so a SIGKILLed kernel's whole socket/lock/flock/dashboard/
    events/cache set piled up under PROJECTS_DIR forever. Same shape as that
    test: a synthetic dead-pid project dir, plus a lock-less one standing in
    for a kernel mid-boot (flock taken, `kernel.lock` not written yet) —
    which must be left alone, not swept as if it were abandoned.
    """
    import tempfile as _tmp

    from repld import paths

    dead = _a_dead_pid()
    dead_dir = paths.PROJECTS_DIR / "sweeptest-deadbeef"
    dead_dir.mkdir(parents=True, exist_ok=True)
    (dead_dir / "kernel.lock").write_text(json.dumps({"pid": dead}))

    no_lock_dir = paths.PROJECTS_DIR / "sweeptest-nolock"
    no_lock_dir.mkdir(parents=True, exist_ok=True)

    live_dir = paths.project_dir(kernel.cwd)
    live_pid = json.loads(kernel.lock_path.read_text())["pid"]

    tmp = Path(_tmp.mkdtemp(prefix="repld-projsweep-"))
    try:
        fresh = Kernel(tmp)
        try:
            assert_true(
                not dead_dir.exists(), "dead kernel's whole project dir reclaimed"
            )
            assert_true(
                no_lock_dir.exists(), "a lock-less dir (mid-boot lookalike) left alone"
            )
            assert_true(
                live_dir.exists(), "the live kernel's own project dir untouched"
            )
            assert_eq(
                json.loads(kernel.lock_path.read_text())["pid"],
                live_pid,
                "live kernel's lockfile intact",
            )
        finally:
            fresh.stop()
    finally:
        shutil.rmtree(dead_dir, ignore_errors=True)
        shutil.rmtree(no_lock_dir, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)

    print(
        "  ✓ boot sweep: dead kernel's whole project dir reclaimed, "
        "lock-less + live dirs kept"
    )


def phase_5(kernel: Kernel) -> None:
    """A second kernel in the same cwd loses the flock and stands down."""
    before = json.loads(kernel.lock_path.read_text())
    env = os.environ.copy()
    proc = subprocess.run(
        ["uv", "run", "--project", str(REPO), "repld", "--no-display"],
        cwd=str(kernel.cwd),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
        check=False,
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


def phase_5_boot_failure(_kernel: Kernel) -> None:
    """A kernel that dies during boot says so on the real stderr.

    `install_tee()` runs partway through boot and `_Tee.write` never touches
    the underlying stream, so everything after it goes to the event log only —
    and there is no display thread yet to render it. A boot failure therefore
    used to be a completely silent exit 1. An over-long `--socket` is the
    cheapest deterministic way to fail past that point: AF_UNIX paths cap at
    ~108 bytes, and the bind happens in `_start_services`, well after the tee.
    """
    import sys as _sys
    import tempfile as _tmp

    tmp = Path(_tmp.mkdtemp(prefix="repld-bootfail-"))
    try:
        too_long = tmp / ("d" * 120) / "kernel.sock"
        too_long.parent.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["REPLD_BOUND"] = "1"  # don't re-exec; this is about the boot path
        r = subprocess.run(
            [_sys.executable, "-m", "repld", "--socket", str(too_long), "--no-display"],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=tmp,
            env=env,
            check=False,
        )
        assert_true(r.returncode != 0, "a failed boot exits non-zero")
        assert_true(
            "kernel failed to start" in r.stderr,
            f"stderr names the failure (got {r.stderr[-400:]!r})",
        )
        assert_true(
            "AF_UNIX path too long" in r.stderr,
            f"...and carries the traceback (got {r.stderr[-400:]!r})",
        )
        print("  ✓ boot failure reaches the terminal instead of exiting silently")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _repld(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["uv", "run", "--project", str(REPO), "repld", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _boot_in(tmp: Path) -> Kernel:
    """A headless kernel in *tmp*, started with no flags at all."""
    k = Kernel.__new__(Kernel)
    k.cwd = tmp
    k.stderr_log = tmp / "kernel.stderr"
    with open(k.stderr_log, "w") as log:
        k.proc = subprocess.Popen(
            ["uv", "run", "--project", str(REPO), "repld", "--no-display"],
            cwd=str(tmp),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=log,
            env=os.environ.copy(),
        )
    k._wait_lockfile()
    return k


def _exec(tmp: Path, code: str) -> str:
    return _repld(tmp, "exec", code).stdout.strip()


def phase_5_init(_kernel: Kernel) -> None:
    """repld_init.py is auto-detected — by *every* kernel for the project.

    The flag it replaced only ever fired for a hand-started kernel that won
    the flock, which since lazy spawn is the rare case: the kernel Claude Code
    talks to is spawned by the bridge, and `repld restart` respawns it. Neither
    could carry an argument, so a project's bootstrap silently vanished.
    """
    import tempfile as _tmp

    tmp = Path(_tmp.mkdtemp(prefix="repld-init-"))
    try:
        (tmp / "repld_init.py").write_text(
            "import asyncio\n"
            "X = 42\n"
            "async def _bg():\n"
            "    await asyncio.sleep(0.05)\n"
            "bg = asyncio.create_task(_bg())\n"
            "print('bootstrap loaded, X=', X)\n"
        )

        # 1. Hand-started kernel, no flags — the file is found by convention.
        k = _boot_in(tmp)
        try:
            b = Bridge(tmp)
            try:
                b.call("initialize", {"protocolVersion": "2024-11-05"})
                b.send("notifications/initialized", {}, notif=True)
                resp = b.call(
                    "tools/call", {"name": "exec", "arguments": {"code": "print(X)"}}
                )
                content = resp["result"]["content"][0]["text"]
                assert_true(
                    "42" in content,
                    f"repld_init.py's X=42 visible in __main__ (got {content!r})",
                )
            finally:
                b.close()
            print("  ✓ repld_init.py auto-detected on a hand-started kernel")

            # 2. The regression guard: `repld restart` goes through
            #    spawn.spawn_headless, which is the path that used to lose the
            #    bootstrap entirely (its argv carries --no-display and nothing
            #    else). Same path the bridge's lazy spawn takes.
            rc = _repld(tmp, "restart")
            assert_eq(rc.returncode, 0, f"repld restart exits 0 (stderr: {rc.stderr})")
            assert_true(
                "42" in _exec(tmp, "print(X)"),
                "bootstrap re-ran on the respawned kernel",
            )
            print("  ✓ repld_init.py re-ran on a kernel spawned by `repld restart`")
        finally:
            _repld(tmp, "stop")
            k.stop()

        # 3. A bootstrap that raises must leave a *live* kernel — that is the
        #    state you fix it from. Killing boot would remove the thing that
        #    can run the fix.
        (tmp / "repld_init.py").write_text(
            "X = 1\nraise RuntimeError('intentional bootstrap boom')\n"
        )
        k = _boot_in(tmp)
        try:
            assert_true(
                "alive" in _exec(tmp, "print('alive')"),
                "kernel still answers after a raising bootstrap",
            )
            log = _repld(tmp, "log", "-n", "200").stdout
            assert_true(
                "intentional bootstrap boom" in log,
                "the bootstrap traceback reached the event log",
            )
            # Bindings made before the raise survive — it's a normal cell.
            assert_true("1" in _exec(tmp, "print(X)"), "pre-raise bindings kept")
            print("  ✓ a raising repld_init.py leaves the kernel up, error on channel")
        finally:
            k.stop()

        # 4. No file at all — nothing to detect, boot unaffected.
        (tmp / "repld_init.py").unlink()
        k = _boot_in(tmp)
        try:
            assert_true("ok" in _exec(tmp, "print('ok')"), "kernel boots with no file")
            print("  ✓ no repld_init.py → clean boot")
        finally:
            k.stop()

        # 5. The flag is gone, not silently ignored.
        rc = _repld(tmp, "--init", "repld_init.py")
        assert_true(rc.returncode != 0, "--init is rejected, not accepted as a no-op")
        assert_true(
            "--init" in rc.stderr,
            f"argparse names the removed flag (got {rc.stderr!r})",
        )
        print("  ✓ --init removed: rejected by argparse rather than ignored")

        # 6. The first exec must not race the bootstrap. The socket binds
        #    before repld_init.py runs, so a caller that connects the instant
        #    the lockfile appears used to reach a bare __main__ and get a
        #    NameError for anything the bootstrap defines.
        (tmp / "repld_init.py").write_text(
            "import time\ntime.sleep(2)\nSLOW = 'bootstrap finished'\n"
        )
        k = _boot_in(tmp)
        try:
            out = _exec(tmp, "print(SLOW)")
            assert_true(
                "bootstrap finished" in out,
                f"first exec waits for a slow bootstrap (got {out!r})",
            )
            print("  ✓ exec waits for the bootstrap instead of racing it")
        finally:
            k.stop()

        # 6b. …and so must a gist tool, which is the *other* thing that lazily
        #     spawns a kernel. It has no deferral to degrade into — it answers
        #     inline or not at all — so the wait can't live where exec's does.
        #     No _boot_in here: the bridge does the spawning, which is the shape
        #     that actually arrives before a bootstrap has run.
        (tmp / "gists").mkdir(exist_ok=True)
        (tmp / "gists" / "bootstate.py").write_text(
            '"""Reads what the project bootstrap put in __main__."""\n'
            "\n"
            "def _tool_boot_state() -> str:\n"
            '    """Report the bootstrap-set value."""\n'
            "    import __main__\n"
            '    return getattr(__main__, "SLOW", "MISSING")\n'
        )
        b = Bridge(tmp)
        try:
            b.call("initialize", {"protocolVersion": "2024-11-05"})
            b.send("notifications/initialized", {}, notif=True)
            # Generous: this call pays for the lazy spawn *and* the 2s
            # bootstrap it now has to wait through.
            resp = b.call(
                "tools/call",
                {"name": "boot_state", "arguments": {}},
                timeout=60.0,
            )
            content = resp["result"]["content"][0]["text"]
            assert_true(
                "bootstrap finished" in content,
                f"gist tool waits for a slow bootstrap (got {content!r})",
            )
            print("  ✓ a gist tool waits for the bootstrap too, not just exec")
        finally:
            b.close()
            _repld(tmp, "stop")
        shutil.rmtree(tmp / "gists", ignore_errors=True)

        # 7. A bootstrap that raises must still release exec — a kernel nobody
        #    can run code in is a kernel nobody can repair.
        (tmp / "repld_init.py").write_text("raise RuntimeError('boom')\n")
        k = _boot_in(tmp)
        try:
            assert_true(
                "alive" in _exec(tmp, "print('alive')"),
                "exec is released even when the bootstrap raised",
            )
            print("  ✓ a failed bootstrap still releases exec")
        finally:
            k.stop()

        # 8. `.env` is read once at boot; load_dotenv() is how a value written
        #    afterwards gets picked up.
        (tmp / "repld_init.py").unlink()
        (tmp / ".env").write_text("PHASE5_LATE=\n")
        k = _boot_in(tmp)
        try:
            assert_eq(
                _exec(tmp, "import os; print(repr(os.environ['PHASE5_LATE']))"),
                "''",
                "empty value captured at boot",
            )
            (tmp / ".env").write_text("PHASE5_LATE=real\n")
            # No-override means the empty capture wins until it is cleared —
            # the trap the docstring calls out.
            out = _exec(
                tmp,
                "from repld import load_dotenv\n"
                "import os\n"
                "load_dotenv()\n"
                "print('still:', repr(os.environ['PHASE5_LATE']))\n"
                "os.environ.pop('PHASE5_LATE', None)\n"
                "load_dotenv()\n"
                "print('now:', repr(os.environ['PHASE5_LATE']))",
            )
            assert_true(
                "still: ''" in out, f"load_dotenv does not override (got {out!r})"
            )
            assert_true("now: 'real'" in out, f"cleared then reloaded (got {out!r})")
            print("  ✓ load_dotenv() re-reads .env; no-override rule holds")
        finally:
            k.stop()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def phase_5_search_dir(_kernel: Kernel) -> None:
    """gists.add_search_dir() — a third gist-search tier wired in from
    repld_init.py, e.g. for a Claude Code plugin's bundled gists.
    """
    import tempfile as _tmp
    import time

    tmp = Path(_tmp.mkdtemp(prefix="repld-searchdir-"))
    extra = Path(_tmp.mkdtemp(prefix="repld-searchdir-extra-"))
    try:
        (extra / "plugin_math.py").write_text("def add(a, b):\n    return a + b\n")
        (extra / "plugin_a.py").write_text(
            "from plugin_b import VALUE\nRESULT = VALUE * 2\n"
        )
        (extra / "plugin_b.py").write_text("VALUE = 21\n")
        (tmp / "repld_init.py").write_text(
            "from pathlib import Path\n"
            "from repld import gists\n"
            f"gists.add_search_dir(Path({str(extra)!r}))\n"
        )

        k = _boot_in(tmp)
        try:
            out = _exec(tmp, "import plugin_math; print(plugin_math.add(2, 3))")
            assert_true(
                "5" in out, f"a gist in the extra dir is importable (got {out!r})"
            )
            print("  ✓ add_search_dir() makes a gist in the extra dir importable")

            out = _exec(tmp, "import plugin_a; print(plugin_a.RESULT)")
            assert_true(
                "42" in out,
                f"cross-import between extra-dir gists resolved (got {out!r})",
            )
            print("  ✓ cross-imports between extra-dir gists resolve")

            out = _exec(
                tmp,
                "from repld import gists\n"
                f"print({str(extra)!r} in [str(d) for d in gists.search_dirs()])",
            )
            assert_true(
                "True" in out, f"search_dirs() lists the extra dir (got {out!r})"
            )
            out = _exec(
                tmp, "from repld import gists\nprint(gists.resolve('plugin_math'))"
            )
            assert_true(
                str(extra / "plugin_math.py") in out,
                f"resolve() names the extra dir's file (got {out!r})",
            )
            print("  ✓ search_dirs()/resolve() expose the extra tier for debugging")

            # mtime-reload: _GistFinder/_check_reload apply to _installed_dirs
            # uniformly, so the extra dir gets the same auto-reload as
            # ./gists — an edit under the running kernel must not go stale.
            time.sleep(0.05)  # ensure the mtime actually advances
            (extra / "plugin_math.py").write_text("def add(a, b):\n    return 999\n")
            out = _exec(tmp, "import plugin_math; print(plugin_math.add(2, 3))")
            assert_true("999" in out, f"edit picked up without restart (got {out!r})")
            print("  ✓ mtime-reload applies to the extra dir, same as local/global")

            # resolve() is a stateless dir walk (no _managed/sys.modules
            # lookup), so it reports a *new* shadow immediately — while the
            # already-imported module keeps serving the old file until a
            # restart. resolve() disagreeing with the cached module's
            # __file__ is the diagnostic for that gap.
            (tmp / "gists").mkdir(exist_ok=True)
            (tmp / "gists" / "plugin_math.py").write_text(
                "def add(a, b):\n    return 'local'\n"
            )
            out = _exec(
                tmp,
                "import plugin_math\n"
                "from repld import gists\n"
                "print('resolve:', gists.resolve('plugin_math'))\n"
                "print('cached:', plugin_math.__file__)\n"
                "print('value:', plugin_math.add(2, 3))",
            )
            assert_true(
                f"resolve: {tmp / 'gists' / 'plugin_math.py'}" in out,
                f"resolve() names the new shadow immediately (got {out!r})",
            )
            assert_true(
                f"cached: {extra / 'plugin_math.py'}" in out,
                f"the already-imported module keeps its old __file__ (got {out!r})",
            )
            assert_true(
                "value: 999" in out,
                f"and keeps serving the pre-shadow value until restart (got {out!r})",
            )
            print(
                "  ✓ resolve() vs. module.__file__ diagnoses a not-yet-restarted shadow"
            )
        finally:
            k.stop()
            (tmp / "gists" / "plugin_math.py").unlink()

        # Shadowing: install() puts global before local; add_search_dir()
        # appends after both, so a project's own gist of the same name wins
        # over one shipped by a plugin. Needs a *fresh* kernel — _check_reload
        # only re-checks a cached module's own recorded path for a newer
        # mtime, it never re-walks _installed_dirs for a higher-precedence
        # file appearing after the name was already imported once.
        (tmp / "gists").mkdir(exist_ok=True)
        (tmp / "gists" / "plugin_math.py").write_text(
            "def add(a, b):\n    return 'local'\n"
        )
        k = _boot_in(tmp)
        try:
            out = _exec(tmp, "import plugin_math; print(plugin_math.add(2, 3))")
            assert_true("local" in out, f"./gists shadows the extra dir (got {out!r})")
            print("  ✓ a local gist of the same name shadows the extra dir")
        finally:
            k.stop()
            shutil.rmtree(tmp / "gists", ignore_errors=True)

        # A missing search dir (plugin uninstalled or moved) warns and boots
        # anyway — a bad path from the project's own bootstrap must not be
        # why the kernel won't come up.
        gone = extra / "does-not-exist"
        (tmp / "repld_init.py").write_text(
            "from pathlib import Path\n"
            "from repld import gists\n"
            f"gists.add_search_dir(Path({str(gone)!r}))\n"
        )
        k = _boot_in(tmp)
        try:
            assert_true(
                "alive" in _exec(tmp, "print('alive')"),
                "kernel still boots when the search dir is missing",
            )
            log = _repld(tmp, "log", "-n", "200").stdout
            assert_true(
                "does not exist" in log and str(gone) in log,
                f"missing search dir warns rather than failing silently (got {log!r})",
            )
            print("  ✓ a missing search dir warns instead of failing boot")
        finally:
            k.stop()

        # Idempotent: repld_init.py runs on every boot, so a repeat call with
        # the same path must not duplicate the sys.path entry or reorder it.
        (tmp / "repld_init.py").write_text(
            "from pathlib import Path\n"
            "from repld import gists\n"
            f"gists.add_search_dir(Path({str(extra)!r}))\n"
            f"gists.add_search_dir(Path({str(extra)!r}))\n"
        )
        k = _boot_in(tmp)
        try:
            out = _exec(
                tmp,
                "from repld import gists\n"
                f"print([str(d) for d in gists.search_dirs()].count({str(extra)!r}))",
            )
            assert_true("1" in out, f"repeat add_search_dir is a no-op (got {out!r})")
            print("  ✓ add_search_dir() is idempotent")
        finally:
            k.stop()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(extra, ignore_errors=True)
