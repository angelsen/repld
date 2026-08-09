"""Runtime state files — write, read, validate, and lock them.

`paths.py` decides *where* kernel state lives; this decides how it is written
and trusted. Everything here operates on a path the caller already resolved,
so it has no repld imports and can be used from any layer.

Two invariants worth keeping:

  - State files are rewritten atomically (tmp + ``os.replace``), so a reader
    racing a writer sees the old file or the new one, never a torn one.
  - A lockfile is only as good as its pid. `read_lock` validates liveness so
    callers can't act on a kernel that died without cleaning up.
"""

import fcntl
import io
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import IO, Any, Literal, overload

# `{pid}-…` — the naming convention every per-process runtime file follows, and
# the whole basis of sweep_dead_pid_files().
_PID_PREFIX = re.compile(r"^(\d+)-")


def _is_zombie(pid: int) -> bool:
    """True if *pid* has exited but hasn't been reaped by its parent.

    Linux only — everywhere else there is no procfs to ask and this reports
    False, leaving `pid_alive` exactly as accurate as it was before.

    Reading ``/proc/<pid>/stat`` rather than ``status`` because the process
    name sits in parentheses and may itself contain spaces, newlines, or more
    parentheses; splitting after the *last* ``)`` is the only parse that
    survives a process called ``foo) Z (bar``.
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            data = f.read()
    except OSError:
        return False
    rparen = data.rfind(b")")
    if rparen == -1:
        return False
    fields = data[rparen + 1 :].split()
    return bool(fields) and fields[0] == b"Z"


def pid_alive(pid) -> bool:
    """True if *pid* names a process that is actually still running.

    ``os.kill(pid, 0)`` alone is not enough: it succeeds for a **zombie**, a
    process that has already exited and is only waiting to be reaped. repld
    creates them routinely — `spawn.spawn_headless` never waits on the kernel
    it starts, so a kernel that dies before its bridge stays a zombie until the
    bridge exits. Counting one as alive means `repld stop` reporting "did not
    exit within 10s" about a process that did, `read_lock` blessing a lockfile
    whose kernel is gone, and `sweep_dead_pid_files` declining to reclaim its
    spill files.
    """
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but isn't ours. Not a process we could have failed to reap,
        # so the zombie check would only ever be answering about someone
        # else's bookkeeping.
        return True
    return not _is_zombie(pid)


def dashboard_token(hint_path: Path) -> str:
    """The dashboard's API token from a kernel's hint file, "" if unavailable.

    One owner for the credential read behind repld's only authenticated
    surface. It was open-coded at all three call sites (`repld dashboard`,
    `repld status`, and the dashboard's own cross-project sidebar), each with
    a different exception tuple and a different empty-token fallback — which
    is the wrong thing to have three opinions about.

    Absent, unreadable, malformed and token-less all collapse to "": every
    caller's next move is the same, since a dashboard reachable but not
    authenticatable is one you render unlinked rather than one you retry.
    """
    try:
        hint = json.loads(hint_path.read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return ""
    return str(hint.get("token") or "") if isinstance(hint, dict) else ""


def read_lock(lock_path: Path) -> dict | str:
    """Read + validate a kernel lockfile.

    Returns the lock dict if the file parses and its pid is alive, or an
    error message string (missing / unreadable / stale pid) otherwise.
    """
    if not lock_path.exists():
        return f"no kernel running for this project (no {lock_path.name})"
    try:
        lock = json.loads(lock_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return f"cannot read {lock_path.name}: {e}"
    if not pid_alive(lock.get("pid", -1)):
        return f"kernel pid {lock.get('pid')} is not running (stale {lock_path.name})"
    return lock


def atomic_write_json(
    path: Path,
    obj: object,
    *,
    indent: int | None = None,
    chmod: int | None = None,
) -> None:
    """Write JSON via tmp + os.replace so concurrent readers never see a torn file.

    indent=N also appends a trailing newline (pretty files are committed or
    hand-read). The tmp name carries the pid so concurrent writers (e.g. two
    kernels booting) can't clobber each other's tmp — last rename wins cleanly.

    When `chmod` is given the *tmp* is created at that mode, not chmod-ed into
    it afterwards. It holds byte-for-byte what the final file will (a
    dashboard API token, a session's list of project paths), so creating it at
    the umask default and fixing it up one line later left exactly the window
    this function is supposed to be the way to avoid.
    """
    text = json.dumps(obj, indent=indent)
    if indent is not None:
        text += "\n"
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    if chmod is None:
        tmp.write_text(text, "utf-8")
    else:
        with open_private(tmp, perm=chmod) as fp:
            fp.write(text)
    os.replace(tmp, path)


@overload
def open_private(
    path: Path, mode: Literal["w"] = "w", *, perm: int = 0o600
) -> io.TextIOWrapper: ...
@overload
def open_private(
    path: Path, mode: Literal["wb"], *, perm: int = 0o600
) -> io.BufferedWriter: ...
def open_private(path: Path, mode: str = "w", *, perm: int = 0o600) -> IO[Any]:
    """Open a runtime file for writing at 0600, with no window at 0644.

    `open(path, "w")` creates at 0644, so chmod-ing afterwards leaves a race in
    which the file is briefly world-readable. What lands in RUNTIME_DIR is cell
    output (tokens, API responses, query results) and page screenshots, so it
    is created at the right mode instead.

    `perm` exists for `atomic_write_json`, which needs the same guarantee for a
    caller-chosen mode; it is not an invitation to widen one. The mode is still
    subject to umask, which can only ever make it stricter.

    Text mode is utf-8 rather than the locale default: cell output is arbitrary
    text, and an ASCII locale would raise UnicodeEncodeError on the first
    non-ASCII byte. Pass "wb" for binary.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    encoding = None if "b" in mode else "utf-8"
    return os.fdopen(os.open(path, flags, perm), mode, encoding=encoding)


def sweep_dead_pid_files(directory: Path) -> int:
    """Unlink `{pid}-*` files in *directory* whose pid is no longer running.

    Everything a process scribbles into the runtime dir — task spills, resource
    spills, screenshots — is named `{pid}-…` precisely so this one rule can
    reclaim it. Nothing cleans up on the way out: a kernel can be SIGKILLed,
    and files outlive the atexit hooks either way. Under a real
    ``$XDG_RUNTIME_DIR`` the accumulation is tmpfs, i.e. RAM held until logout;
    under the ``/tmp/repld-{uid}`` fallback it is forever.

    Only files are considered, and only ones a *dead* pid owns. A recycled pid
    makes this conservative (a file is kept that could have gone), never
    destructive — a live kernel's own output is untouchable by construction.
    """
    removed = 0
    try:
        entries = list(directory.iterdir())
    except OSError:
        return 0
    for entry in entries:
        m = _PID_PREFIX.match(entry.name)
        if m is None:
            continue
        try:
            if not entry.is_file() or pid_alive(int(m.group(1))):
                continue
            entry.unlink()
        except OSError:
            continue
        removed += 1
    return removed


def sweep_own_stale_files(
    directory: Path, *, max_age: float, keep: "set[str] | frozenset[str]" = frozenset()
) -> int:
    """Unlink *this* process's `{pid}-*` files untouched for `max_age` seconds.

    The companion to `sweep_dead_pid_files`, for the half it cannot reach. That
    one reclaims what a *dead* pid left behind, which is the only rule that can
    apply at boot — but a kernel is meant to run for weeks, and everything it
    writes here is untouchable by it for exactly that long. Task spills are
    reclaimed by their registry entry (`tasks._prune_spill_files`); resource
    spills and browser screenshots have no registry, so without this they
    accumulate for the life of the process. Under a real `$XDG_RUNTIME_DIR`
    that is RAM.

    Age is last-modification, not creation: a file still being appended to
    stays young. `keep` names paths that must survive regardless — the caller's
    live registry entries, which age out on their own schedule.
    """
    now = time.time()
    removed = 0
    prefix = f"{os.getpid()}-"
    try:
        entries = list(directory.iterdir())
    except OSError:
        return 0
    for entry in entries:
        if not entry.name.startswith(prefix) or str(entry) in keep:
            continue
        try:
            if not entry.is_file() or now - entry.stat().st_mtime < max_age:
                continue
            entry.unlink()
        except OSError:
            continue
        removed += 1
    return removed


def sweep_dead_project_dirs(projects_dir: Path) -> int:
    """Reclaim a dead kernel's whole per-project directory under PROJECTS_DIR.

    The third boot-time sweep rule described in paths.py's module docstring —
    applied one level up from `sweep_dead_pid_files`, to the
    ``PROJECTS_DIR/<slug>/`` directory itself rather than files inside it.

    Only reclaims a directory whose ``kernel.lock`` exists *and* names a dead
    pid — the same asymmetry `sweep_dead_pid_files` uses, in the same
    direction. A directory with no lock yet may be another kernel mid-boot
    (flock taken in `_claim_project`, lock not written until
    `_write_lockfile` much later in `_start_services`); reclaiming it would
    race that boot. A directory with no lock *ever* (a client resolved
    `project_dir()` — `repld status`, `repld exec` — but no kernel started
    there) is left alone too: there's nothing here to say it's abandoned
    rather than simply unused, and it costs nothing to leave standing.
    """
    removed = 0
    try:
        entries = list(projects_dir.iterdir())
    except OSError:
        return 0
    for entry in entries:
        if not entry.is_dir():
            continue
        lock_path = entry / "kernel.lock"
        if not lock_path.exists():
            continue
        if isinstance(read_lock(lock_path), dict):
            continue  # pid alive
        try:
            shutil.rmtree(entry)
        except OSError:
            continue
        removed += 1
    return removed


def acquire_lock(flock_path: Path) -> int | None:
    """Take the single-kernel-per-project mutex.

    Returns the held fd (which the caller must keep open for its whole life —
    closing it releases the lock), or None if another kernel owns it.

    O_CREAT *without* O_TRUNC: opening must not clobber anything before we
    know whether we win, and this file is never replaced by an atomic rename,
    so the flock keeps referring to the inode every contender opened.

    The pid we then write is a debugging courtesy — nothing reads it, and the
    authoritative copy is `kernel.lock`. It is truncated first regardless:
    without that, a shorter pid landing on a longer one leaves the tail of
    the old number behind (99 over 123456 reads as `99456`), so the one
    thing the file is good for would be actively misleading. `ftruncate`
    rather than `O_TRUNC` — it empties the file we already hold the lock on
    instead of the one we were about to contend for, and keeps the inode.
    """
    flock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(flock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    return fd
