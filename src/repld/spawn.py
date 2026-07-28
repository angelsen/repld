"""Spawning a headless kernel — the one copy of the incantation.

Two callers need it and must not drift: `bridge.py`, when Claude Code connects
and no kernel is running for the cwd, and `repld restart`. It lives in its own
module rather than inside `lifecycle_cmd` because the bridge is spawned once
per Claude Code session and `cli.py` keeps it to a dict lookup plus a single
import — pulling in `lifecycle_cmd`'s urllib/signal/sessions would tax every
session for code the bridge never runs.

Only the spawn is shared. Each caller keeps its own wait loop, because they
wait on genuinely different things: the bridge polls until the socket is
*connectable* (it needs the connection anyway), `repld restart` polls until the
lockfile appears. Parameterising one loop over both predicates would cost more
than the six lines it saved.

**Where the kernel lands matters as much as that it starts.** `start_new_session`
alone is not independence: `setsid()` detaches from the controlling terminal, so
closing the window won't SIGHUP the kernel, but it neither reparents the process
nor moves it out of the spawner's cgroup. A kernel spawned by a bridge therefore
stayed a child of that bridge and shared the terminal window's cgroup, so
multi-gigabyte state (model weights, a browser) was accounted to the window.

Note what this does *not* buy: kill priority. `oom_score_adj` comes out the same
either way — the user manager's `DefaultOOMScoreAdjust` is 200 on a stock Arch
session, which happens to match what a Claude Code client passes down. Under
global OOM the killer picks by RSS regardless, so a kernel holding several
gigabytes is still first in line. `REPLD_MEMORY_HIGH` and
`REPLD_OOM_SCORE_ADJUST` exist for anyone who wants to change that, and are
opt-in precisely because both override a deliberate policy.

So on systemd we hand the kernel to the user manager as a transient unit
instead. A *service*, not a `--scope`: `--scope` runs the command in the
caller's own context and would keep both the parentage and the inherited
`oom_score_adj`, while a service forks from `systemd --user`, returns
immediately, gets its own cgroup, and sends stdout/stderr to the journal — which
is the only place a bridge-spawned kernel's boot failure can be read, since it
has no terminal.

Everywhere else, and on any systemd failure, the plain `Popen` path still
applies. Spawning must not become less reliable than it was.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import paths

# Names systemd sets *for* a unit, or that describe the caller's own unit.
# Forwarding them would tell the new service it is something it isn't.
_ENV_DENYLIST = frozenset(
    {"INVOCATION_ID", "JOURNAL_STREAM", "NOTIFY_SOCKET", "MANAGERPID"}
)
_ENV_DENY_PREFIXES = ("LISTEN_",)


def _systemd_unit_name(cwd: Path | None = None) -> str:
    """`repld-<slug>.service`, reusing paths.project_slug's readable+hash id.

    One naming scheme for the project, not two: the slug is already what the
    runtime directory is named, so a unit is greppable against its state.
    """
    return f"repld-{paths.project_slug(cwd)}.service"


def _systemd_run_argv(cmd: list[str], cwd: Path, env: dict[str, str]) -> list[str]:
    """Build the `systemd-run` invocation. Pure, so it can be asserted on.

    `--collect` so a kernel that dies badly doesn't leave a `failed` unit
    squatting the name; `--working-directory` because repld is per-cwd and a
    unit would otherwise inherit the manager's.
    """
    argv = [
        "systemd-run",
        "--user",
        "--quiet",
        "--collect",
        f"--unit={_systemd_unit_name(cwd)}",
        f"--working-directory={cwd}",
    ]
    # Both opt-in, and for the same reason: they override deliberate policy.
    # A default memory ceiling would break the legitimate case of loading
    # several gigabytes of model weights into a cell on purpose. And the
    # manager's DefaultOOMScoreAdjust (200 on a stock Arch user session) is a
    # considered statement that user apps are more expendable than system
    # services — repld shouldn't quietly exempt itself from it, even though
    # its kernels are usually the fattest process around and so die first.
    # Floor is the manager's own oom_score_adj: lower needs CAP_SYS_RESOURCE,
    # and systemd clamps rather than failing, so asking for 0 silently gets
    # you 100.
    limit = env.get("REPLD_MEMORY_HIGH")
    if limit:
        argv += ["-p", f"MemoryHigh={limit}"]
    oom_adj = env.get("REPLD_OOM_SCORE_ADJUST")
    if oom_adj:
        argv += ["-p", f"OOMScoreAdjust={oom_adj}"]
    for name, value in sorted(env.items()):
        if name in _ENV_DENYLIST or name.startswith(_ENV_DENY_PREFIXES):
            continue
        argv += [f"--setenv={name}={value}"]
    return argv + cmd


def _kernel_argv(sock_path: Path) -> list[str]:
    """`python -m repld` rather than the `repld` console script: the script may
    not be on PATH in the environment a client hands us, but the interpreter
    running us always has the package importable.
    """
    cmd = [sys.executable, "-m", "repld", "--no-display"]
    # Only forward --socket when it isn't the default this cwd would resolve to
    # anyway; REPLD_SOCKET is inherited through the environment.
    if sock_path != paths.socket_path():
        cmd += ["--socket", str(sock_path)]
    return cmd


def _spawn_via_systemd(cmd: list[str], cwd: Path) -> bool:
    """Hand the kernel to the user manager. False means "didn't start it"."""
    argv = _systemd_run_argv(cmd, cwd, dict(os.environ))
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"repld: systemd-run failed ({e}); spawning directly", file=sys.stderr)
        return False
    if r.returncode == 0:
        return True
    # A taken unit name is a racing boot, which the kernel.flock mutex already
    # arbitrates — the caller polls and adopts the incumbent either way. Say so
    # quietly rather than falling through and starting a second kernel.
    detail = (r.stderr or r.stdout).strip().splitlines()
    if any("already exists" in line for line in detail):
        return False
    print(
        f"repld: systemd-run failed ({detail[-1] if detail else r.returncode}); "
        "spawning directly",
        file=sys.stderr,
    )
    return False


def _spawn_directly(cmd: list[str], cwd: Path) -> bool:
    try:
        subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as e:
        print(f"repld: could not spawn kernel: {e}", file=sys.stderr)
        return False
    return True


def spawn_headless(sock_path: Path) -> bool:
    """Start a headless kernel for the current cwd, outliving its spawner.

    As a transient systemd user service where that's available, so the kernel
    gets its own cgroup and lifetime; otherwise a detached child. If two callers
    race, the kernel's flock mutex settles it and the loser exits 0.

    True means the process started, not that the kernel is up — poll for that.
    """
    cwd = Path(os.getcwd())
    cmd = _kernel_argv(sock_path)
    if shutil.which("systemd-run") and os.environ.get("XDG_RUNTIME_DIR"):
        if _spawn_via_systemd(cmd, cwd):
            return True
        # The unit may exist already (a racing boot). Don't start a second
        # kernel behind systemd's back — the caller's poll adopts the incumbent.
        if _systemd_unit_active(cwd):
            return False
    return _spawn_directly(cmd, cwd)


def _systemd_unit_active(cwd: Path) -> bool:
    """Whether this project's unit is already running, so a failed spawn
    doesn't get retried as a bare process alongside it."""
    try:
        r = subprocess.run(
            ["systemctl", "--user", "is-active", _systemd_unit_name(cwd)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return r.stdout.strip() == "active"
