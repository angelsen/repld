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
from pathlib import Path
from typing import IO, Any, Literal, overload


def pid_alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but isn't ours — still alive.
        return True


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
    hand-read). chmod is applied to the tmp file before the rename, so the
    final file never exists with wrong permissions. The tmp name carries the
    pid so concurrent writers (e.g. two kernels booting) can't clobber each
    other's tmp — last rename wins cleanly.
    """
    text = json.dumps(obj, indent=indent)
    if indent is not None:
        text += "\n"
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, "utf-8")
    if chmod is not None:
        tmp.chmod(chmod)
    os.replace(tmp, path)


@overload
def open_private(path: Path, mode: Literal["w"] = "w") -> io.TextIOWrapper: ...
@overload
def open_private(path: Path, mode: Literal["wb"]) -> io.BufferedWriter: ...
def open_private(path: Path, mode: str = "w") -> IO[Any]:
    """Open a runtime file for writing at 0600, with no window at 0644.

    `open(path, "w")` creates at 0644, so chmod-ing afterwards leaves a race in
    which the file is briefly world-readable. What lands in RUNTIME_DIR is cell
    output (tokens, API responses, query results) and page screenshots, so it
    is created at the right mode instead.

    Text mode is utf-8 rather than the locale default: cell output is arbitrary
    text, and an ASCII locale would raise UnicodeEncodeError on the first
    non-ASCII byte. Pass "wb" for binary.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    encoding = None if "b" in mode else "utf-8"
    return os.fdopen(os.open(path, flags, 0o600), mode, encoding=encoding)


def acquire_lock(flock_path: Path) -> int | None:
    """Take the single-kernel-per-project mutex.

    Returns the held fd (which the caller must keep open for its whole life —
    closing it releases the lock), or None if another kernel owns it.

    O_CREAT *without* O_TRUNC: opening must not clobber anything before we
    know whether we win, and this file is never replaced by an atomic rename,
    so the flock keeps referring to the inode every contender opened.
    """
    flock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(flock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    os.write(fd, f"{os.getpid()}\n".encode())
    return fd
