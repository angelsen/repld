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

import hashlib
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


def _systemd_unit_name(sock_path: Path) -> str:
    """`repld-<slug>-<hash>.service`, keyed off the *socket* path.

    Not cwd: every other piece of per-project state (lock_for, flock_for,
    hint_for, eventlog_for) hangs off the socket path specifically so an
    explicit --socket/REPLD_SOCKET override "moves the whole set coherently"
    (paths.py). Naming the unit from cwd instead would let two kernels that
    share a cwd but were given different --socket overrides collide on one
    unit name — the second spawn would read the first kernel's unit as its
    own incumbent and never actually start.

    The digest is over the resolved socket path itself, so two overrides
    that happen to share a parent directory basename still get distinct
    units; the basename is kept only so the unit stays greppable — for the
    common, un-overridden socket (under paths.project_dir) that basename is
    already `paths.project_slug`.
    """
    resolved = sock_path.resolve()
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:8]
    base = resolved.parent.name or "repld"
    return f"repld-{base}-{digest}.service"


def _systemd_run_argv(
    cmd: list[str], sock_path: Path, cwd: Path, env: dict[str, str]
) -> list[str]:
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
        f"--unit={_systemd_unit_name(sock_path)}",
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


STARTED, INCUMBENT, FAILED = "started", "incumbent", "failed"


def _systemd_available() -> bool:
    """Whether this is a systemd user session at all.

    Two cheap checks instead of a doomed subprocess: the binary, and the
    runtime dir carrying the user bus that `systemd-run --user` needs to reach
    the manager.
    """
    return bool(shutil.which("systemd-run") and os.environ.get("XDG_RUNTIME_DIR"))


def _unit_running(sock_path: Path) -> bool:
    """Whether this socket's unit is already up (or on its way up)."""
    try:
        r = subprocess.run(
            ["systemctl", "--user", "is-active", _systemd_unit_name(sock_path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    # `activating` counts: a racing boot's incumbent may still be starting, and
    # treating that as absent is exactly the double-spawn this check prevents.
    return r.stdout.strip() in ("active", "activating")


def _spawn_via_systemd(cmd: list[str], sock_path: Path, cwd: Path) -> str:
    """Hand the kernel to the user manager.

    STARTED, INCUMBENT (a racing boot got there first) or FAILED — three
    outcomes the caller must tell apart, since only FAILED should fall through
    to a bare process.
    """
    argv = _systemd_run_argv(cmd, sock_path, cwd, dict(os.environ))
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"repld: systemd-run failed ({e})", file=sys.stderr)
        return FAILED
    if r.returncode == 0:
        return STARTED
    # Ask the manager rather than reading the failure text. systemd words a
    # taken unit name as "was already loaded or has a fragment file" — not
    # "already exists" — and the wording is both version-specific and
    # translatable, so matching on it is dead code waiting to happen.
    if _unit_running(sock_path):
        return INCUMBENT
    detail = (r.stderr or r.stdout).strip().splitlines()
    print(
        f"repld: systemd-run failed ({detail[-1] if detail else r.returncode})",
        file=sys.stderr,
    )
    return FAILED


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


def spawn_headless(sock_path: Path) -> str:
    """Start a headless kernel for the current cwd, outliving its spawner.

    As a transient systemd user service where that's available, so the kernel
    gets its own cgroup and lifetime; otherwise a detached child. If two callers
    race, the kernel's flock mutex settles it and the loser exits 0.

    Returns STARTED, INCUMBENT, or FAILED. Three states rather than a bool
    because only FAILED means "no kernel is coming": INCUMBENT is a racing boot
    that already owns the unit, and the caller should poll and adopt it exactly
    as it would its own spawn. Collapsing the two made `repld restart` report
    failure — silently, returning 1 — while a perfectly good kernel came up.

    STARTED means the *process* started, not that the kernel is up; poll either
    way.
    """
    cwd = Path(os.getcwd())
    cmd = _kernel_argv(sock_path)
    if _systemd_available():
        outcome = _spawn_via_systemd(cmd, sock_path, cwd)
        if outcome in (STARTED, INCUMBENT):
            # Starting a second one behind systemd's back would only give the
            # flock mutex something to reject.
            return outcome
        print("repld: spawning a detached child instead", file=sys.stderr)
    return STARTED if _spawn_directly(cmd, cwd) else FAILED
