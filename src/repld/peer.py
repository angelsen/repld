"""Caller side of kernel-to-kernel calls: the `peers` builtin.

Another project's kernel becomes an awaitable object: its own interpreter, its
own deps, its own warm state, reached over its unix socket instead of installed
into this one. The callee half is `peer_host.py` plus the `peer/*` methods in
`protocol.py`.

One connection per call, no `initialize`. A kernel's reader thread serves one
request at a time per connection, so a shared connection would run a `gather`
one call at a time and queue a cancel behind the call it should stop. A fresh
connection makes concurrency, cancellation (closing the writer) and death (EOF)
fall out of the socket itself.

A call is never retried once its request is written; reconnecting before that
is fine. What came back tells you whether it ran: `PeerRefused` and
`PeerUnavailable` never ran, `PeerCallError` ran and raised, `PeerDisconnected`
is unknown.

Stdlib only; must not import `protocol.py` (the bridge side of this package
cannot afford what it drags in).
"""

from __future__ import annotations

import asyncio
import json
import os
import socket as _socket
import sys
import threading
from pathlib import Path
from typing import Any

from . import ipc, paths, sessions, tasks
from .core_schemas import (
    BRIDGE_GOODBYE_METHOD,
    PEER_CALL_METHOD,
    PEER_ERR_REFUSED,
    PEER_ERR_UNRESOLVED,
    PEER_INSPECT_METHOD,
    PEER_LIST_METHOD,
    PEER_MAX_PAYLOAD_BYTES,
)

__all__ = ["UNSET", "Peer", "PeerError", "peers", "stub_args"]

# One StreamReader line can carry a full 8 MiB value, escaped, plus the error
# envelope around it.
_READ_LIMIT = PEER_MAX_PAYLOAD_BYTES + 256 * 1024
_MAX_IN_FLIGHT = 32
_START_TIMEOUT = 60.0
_REQUEST_ID = 1

# Env a spawned target kernel must not inherit: they would leave it unbound to
# its own project, point it at this kernel's socket or owner, or leak this
# project's `.env` over its own (load_dotenv never overrides a set var).
_SPAWN_ENV_DROP = frozenset(
    {
        "REPLD_BOUND",
        "REPLD_SOCKET",
        "REPLD_PROJECT",
        "REPLD_PROJECT_GIT",
        "REPLD_OWNER_PID",
        "VIRTUAL_ENV",
        "PYTHONPATH",
        "UV_PROJECT_ENVIRONMENT",
    }
)


class PeerError(Exception):
    """Base for everything `peers` raises about another kernel."""


class PeerNotFound(PeerError):
    """A name that matches no live kernel, or more than one."""

    def __init__(self, message: str, candidates: list[str] | None = None):
        super().__init__(message)
        self.candidates = candidates or []


class PeerUnavailable(PeerError):
    """The kernel is not reachable (or predates peers). The call never ran."""


class PeerRefused(PeerError):
    """The callee declined before running anything: an unresolved name, bad
    arguments, a call cycle, a too-deep chain, a call to self."""

    def __init__(
        self, message: str, *, reason: str, code: int | None = None, data: Any = None
    ):
        super().__init__(message)
        self.reason = reason
        self.code = code
        self.data = data


class PeerCallError(PeerError):
    """The target ran and raised (or returned something that cannot travel as
    JSON). `remote_*` carry what the callee saw."""

    def __init__(
        self,
        message: str,
        *,
        peer: str,
        target: str,
        task_id: str | None,
        remote_type: str,
        remote_message: str,
        remote_traceback: str,
        output: str,
        spill_path: str | None,
    ):
        super().__init__(message)
        self.peer = peer
        self.target = target
        self.task_id = task_id
        self.remote_type = remote_type
        self.remote_message = remote_message
        self.remote_traceback = remote_traceback
        self.output = output
        self.spill_path = spill_path


class PeerDisconnected(PeerError):
    """The connection ended before a response. Whether the target ran is
    unknown, so nothing was retried. `clean` means the callee said goodbye
    first (an orderly stop or restart), not a crash."""

    def __init__(self, message: str, *, clean: bool):
        super().__init__(message)
        self.clean = clean


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _real(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


def _is_path_like(target: str) -> bool:
    return os.sep in target or target.startswith((".", "~"))


def resolve_cwd(target: str, live: list[dict]) -> str:
    """The project directory *target* names.

    A path (anything with a separator, or starting `.`/`~`) is taken as given
    and need not be running. A bare name must be the basename of exactly one
    live kernel's cwd.
    """
    if not target:
        raise PeerNotFound("empty peer name")
    if _is_path_like(target):
        return _real(target)
    by_cwd: dict[str, dict] = {}
    for s in live:
        cwd = _real(str(s.get("cwd", "")))
        if os.path.basename(cwd) == target:
            by_cwd.setdefault(cwd, s)
    if not by_cwd:
        known = sorted({os.path.basename(_real(str(s.get("cwd", "")))) for s in live})
        hint = f" Live kernels: {', '.join(known)}." if known else ""
        raise PeerNotFound(f"no live kernel named {target!r}.{hint}", known)
    if len(by_cwd) > 1:
        paths_ = sorted(by_cwd)
        raise PeerNotFound(
            f"{target!r} matches {len(paths_)} live kernels; pass a path: "
            + ", ".join(paths_),
            paths_,
        )
    return next(iter(by_cwd))


def pick_session(cwd: str, live: list[dict]) -> dict | None:
    """The live session for project *cwd*, preferring the project's canonical
    socket when `--socket`/`--ephemeral` kernels share the directory."""
    mine = [s for s in live if _real(str(s.get("cwd", ""))) == cwd]
    if not mine:
        return None
    canonical = str(paths.socket_path(Path(cwd)))
    for s in mine:
        if str(s.get("socket_path")) == canonical:
            return s
    return mine[0]


def build_caller() -> dict | None:
    """Who is asking, for `_meta.caller`; None outside a kernel.

    The Claude fields describe the session that started the whole chain, so a
    call made from inside a peer call inherits them from the task's own caller
    rather than reading a None origin. `chain` is every kernel on the path
    including this one, which the callee uses to refuse cycles.
    """
    try:
        sock = str(ipc.socket_path())
    except RuntimeError:
        return None
    task_id = tasks.current_task_id()
    parent = tasks.get(task_id) if task_id else None
    inherited = (parent or {}).get("caller") or {}
    origin = tasks.current_origin()
    session_id = getattr(origin, "claude_session_id", None) or inherited.get(
        "claude_session_id"
    )
    project_dir = getattr(origin, "claude_project_dir", None) or inherited.get(
        "project_dir"
    )
    kind = getattr(origin, "claude_session_kind", None) or inherited.get("session_kind")
    kernel = {"cwd": os.getcwd(), "pid": os.getpid(), "socket": sock}
    hop = {**kernel, "claude_session_id": session_id, "task_id": task_id}
    return {
        "kernel": kernel,
        "claude_session_id": session_id,
        "project_dir": project_dir,
        "session_kind": kind,
        "task_id": task_id,
        "chain": [*(inherited.get("chain") or []), hop],
    }


# Default of a stub parameter whose peer-side default is not a literal: "leave
# this argument out and let the peer apply its own".
UNSET: Any = object()

_SKIPPED = (
    "a parameter with a non-literal default was left out before a later positional "
    "one; pass it explicitly"
)


def stub_args(
    posonly: list,
    named: dict,
    kwonly: dict,
    varargs: tuple = (),
    varkw: dict | None = None,
) -> tuple[list, dict]:
    """Turn a generated stub's parameters into the ``(args, kwargs)`` to forward.

    `UNSET` values are dropped. Positional-only ones can only be dropped from the
    end, and with `*args` present every positional must be sent positionally,
    because `*args` lands after them; a gap in either case cannot be expressed
    and raises TypeError rather than shifting an argument into the wrong slot.
    """
    if varargs:
        positional = [*posonly, *named.values()]
        if any(v is UNSET for v in positional):
            raise TypeError(_SKIPPED)
        args = [*positional, *varargs]
        kwargs: dict = {}
    else:
        args = list(posonly)
        while args and args[-1] is UNSET:
            args.pop()
        if any(v is UNSET for v in args):
            raise TypeError(_SKIPPED)
        kwargs = {k: v for k, v in named.items() if v is not UNSET}
    kwargs.update({k: v for k, v in kwonly.items() if v is not UNSET})
    kwargs.update(varkw or {})
    return args, kwargs


def raise_for_error(msg: dict, name: str) -> None:
    """Raise the typed exception for a JSON-RPC `error` reply. The target did
    not run in any of these."""
    err = msg.get("error")
    if err is None:
        return
    code = err.get("code")
    text = str(err.get("message", ""))
    data = err.get("data")
    if code == -32601:
        raise PeerUnavailable(
            f"{name} predates peers (method not found); restart it with "
            "`repld restart` so it runs this version"
        )
    if code == PEER_ERR_UNRESOLVED:
        raise PeerRefused(text, reason="unresolved", code=code, data=data)
    if code == PEER_ERR_REFUSED:
        reason = (
            (data or {}).get("reason", "refused")
            if isinstance(data, dict)
            else "refused"
        )
        raise PeerRefused(text, reason=reason, code=code, data=data)
    reason = "bad_args" if code == -32602 else "error"
    raise PeerRefused(text, reason=reason, code=code, data=data)


def map_call_result(msg: dict, name: str, target: str) -> Any:
    """A `peer/call` reply as its value, or the typed exception it stands for."""
    raise_for_error(msg, name)
    result = msg.get("result") or {}
    err = result.get("error")
    if err:
        tb = str(err.get("traceback") or "")
        tail = tb.strip().splitlines()[-1] if tb.strip() else ""
        remote = f"{err.get('type')}: {err.get('message')}"
        raise PeerCallError(
            f"{target} on {name} raised {remote}"
            + (f"\n{tb.rstrip()}" if tail else ""),
            peer=name,
            target=target,
            task_id=result.get("task_id"),
            remote_type=str(err.get("type")),
            remote_message=str(err.get("message")),
            remote_traceback=tb,
            output=str(err.get("output") or ""),
            spill_path=result.get("spill_path"),
        )
    return result.get("value")


def clean_env(environ: dict[str, str], dotenv_keys: set[str]) -> dict[str, str]:
    """`environ` minus what must not reach a kernel spawned for another project."""
    return {
        k: v
        for k, v in environ.items()
        if k not in _SPAWN_ENV_DROP and k not in dotenv_keys
    }


def request_sync(
    socket_path: str, method: str, params: dict, timeout: float = 10.0
) -> dict:
    """One blocking round trip, for the CLI and tests. Returns the raw reply
    message; raises `PeerUnavailable` / `PeerDisconnected` on transport failure."""
    sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(socket_path)
    except OSError as exc:
        sock.close()
        raise PeerUnavailable(f"cannot connect to {socket_path}: {exc}") from exc
    try:
        req = {"jsonrpc": "2.0", "id": _REQUEST_ID, "method": method, "params": params}
        sock.sendall((json.dumps(req) + "\n").encode("utf-8"))
        with sock.makefile("r", encoding="utf-8") as rfile:
            for line in rfile:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("id") == _REQUEST_ID:
                    return msg
    except OSError as exc:
        raise PeerDisconnected(f"{socket_path}: {exc}", clean=False) from exc
    finally:
        sock.close()
    raise PeerDisconnected(f"{socket_path} closed without replying", clean=False)


# ---------------------------------------------------------------------------
# The handle
# ---------------------------------------------------------------------------


class Peer:
    """Another project's kernel. Get one with `peers.get(...)`.

    Every method is awaitable; nothing here pretends a remote call is a local
    attribute. A timeout is the caller's own `asyncio.timeout()`, and cancelling
    the awaiting task cancels the call on the other side.
    """

    cwd: str
    name: str
    start: bool

    def __init__(self, cwd: str, *, start: bool = False):
        self.cwd = cwd
        self.name = os.path.basename(cwd)
        self.start = start
        self._semaphore: asyncio.Semaphore | None = None
        self._start_lock: asyncio.Lock | None = None

    def __repr__(self) -> str:
        return f"<Peer {self.name} {self.cwd}>"

    def _socket_path(self) -> str | None:
        """Where this project's kernel listens now, or None. Re-read on every
        connect: a restarted kernel may have come back somewhere else."""
        row = pick_session(self.cwd, sessions.list_sessions())
        if row is not None:
            return str(row["socket_path"])
        canonical = paths.socket_path(Path(self.cwd))
        return str(canonical) if canonical.exists() else None

    async def _ensure_started(self) -> None:
        if self._start_lock is None:
            self._start_lock = asyncio.Lock()
        async with self._start_lock:
            if await asyncio.to_thread(self._socket_path) is not None:
                return
            from .kernel import DOTENV_KEYS

            env = clean_env(dict(os.environ), DOTENV_KEYS)
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "repld",
                "--project",
                self.cwd,
                "start",
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), _START_TIMEOUT)
            except TimeoutError:
                proc.kill()
                raise PeerUnavailable(
                    f"timed out starting a kernel for {self.cwd}"
                ) from None
            if proc.returncode != 0:
                text = out.decode("utf-8", "replace").strip()[-1000:]
                raise PeerUnavailable(
                    f"`repld --project {self.cwd} start` failed: {text}"
                )

    async def _connect(
        self,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, str]:
        for attempt in range(2):
            path = await asyncio.to_thread(self._socket_path)
            if path is not None:
                try:
                    reader, writer = await asyncio.open_unix_connection(
                        path, limit=_READ_LIMIT
                    )
                    return reader, writer, path
                except (FileNotFoundError, ConnectionRefusedError):
                    pass
            if attempt == 0 and self.start:
                await self._ensure_started()
        raise PeerUnavailable(
            f"no kernel is running for {self.cwd}: run "
            f"`repld --project {self.cwd} start`, or peers.get(..., start=True)"
        )

    async def _request(self, method: str, params: dict) -> dict:
        """One round trip on a connection of its own; returns the raw reply."""
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(_MAX_IN_FLIGHT)
        try:
            line = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": _REQUEST_ID,
                    "method": method,
                    "params": params,
                }
            )
        except (TypeError, ValueError) as exc:
            raise TypeError(
                f"peers: arguments are not JSON-serializable: {exc}. Pass plain "
                "values; send bulk data by file path"
            ) from exc
        if len(line) > PEER_MAX_PAYLOAD_BYTES:
            raise ValueError(
                f"peers: request is {len(line)} bytes of JSON, over the "
                f"{PEER_MAX_PAYLOAD_BYTES} limit; write it to a file and pass the path"
            )
        async with self._semaphore:
            reader, writer, path = await self._connect()
            if path == str(_own_socket()):
                writer.close()
                raise PeerRefused(
                    "a kernel cannot call itself; use the function directly",
                    reason="self",
                )
            goodbye = False
            try:
                try:
                    writer.write((line + "\n").encode("utf-8"))
                    await writer.drain()
                    while True:
                        raw = await reader.readline()
                        if not raw:
                            raise PeerDisconnected(
                                f"{self.name} closed the connection before replying; "
                                "whether the call ran is unknown, so it was not retried",
                                clean=goodbye,
                            )
                        try:
                            msg = json.loads(raw)
                        except ValueError:
                            continue
                        if msg.get("id") == _REQUEST_ID:
                            return msg
                        if msg.get("method") == BRIDGE_GOODBYE_METHOD:
                            goodbye = True
                except (ConnectionError, BrokenPipeError) as exc:
                    raise PeerDisconnected(
                        f"{self.name}: {exc}; whether the call ran is unknown",
                        clean=False,
                    ) from exc
            finally:
                # On cancellation this close is the hangup the callee's reader
                # thread polls for to cancel the running target.
                writer.close()

    async def call(self, target: str, /, *args: Any, **kwargs: Any) -> Any:
        """Run the dotted `target` (e.g. "voice.transcribe", "os.getcwd") in the
        peer's kernel and return its JSON value.

        Positional-only on `target` so a remote parameter may itself be called
        `target`. Raises `PeerRefused` (never ran), `PeerCallError` (ran and
        raised), `PeerDisconnected` (unknown) or `PeerUnavailable`.
        """
        params = {
            "target": target,
            "args": list(args),
            "kwargs": kwargs,
            "_meta": {"caller": build_caller()},
        }
        msg = await self._request(PEER_CALL_METHOD, params)
        return map_call_result(msg, self.name, target)

    async def info(self) -> dict:
        """The peer's identity and callable surface: kernel (cwd, pid, python,
        venv), its public gists with function signatures, and `__main__` callables."""
        msg = await self._request(PEER_LIST_METHOD, {})
        raise_for_error(msg, self.name)
        return msg["result"]

    async def inspect(self, target: str) -> dict:
        """What the dotted `target` resolves to in the peer: kind, signature,
        whether it is async, its doc and file. Runs nothing."""
        msg = await self._request(PEER_INSPECT_METHOD, {"target": target})
        raise_for_error(msg, self.name)
        return msg["result"]


def _own_socket() -> str:
    try:
        return str(ipc.socket_path())
    except RuntimeError:
        return ""


class _Peers:
    """`peers`: find other project kernels and get handles to call them."""

    PeerError = PeerError
    PeerNotFound = PeerNotFound
    PeerUnavailable = PeerUnavailable
    PeerRefused = PeerRefused
    PeerCallError = PeerCallError
    PeerDisconnected = PeerDisconnected

    def __init__(self) -> None:
        self._handles: dict[str, Peer] = {}
        self._lock = threading.Lock()

    def list(self) -> list[dict]:
        """Live kernels on this machine: name, cwd, pid, socket_path,
        started_at, and whether it is this one."""
        return [
            {
                "name": os.path.basename(_real(str(s["cwd"]))),
                "cwd": s["cwd"],
                "pid": s["pid"],
                "socket_path": s["socket_path"],
                "started_at": s.get("started_at"),
                "self": int(s["pid"]) == os.getpid(),
            }
            for s in sessions.list_sessions()
        ]

    def get(self, target: str, *, start: bool = False) -> Peer:
        """A handle for the project at path `target`, or the live kernel whose
        directory is named `target`. Sync and cheap; nothing connects until the
        first await.

        A path need not be running. `start=True` makes the first call spawn that
        project's kernel (bound to its own venv) if none is up; without it a
        stopped peer is a `PeerUnavailable`.
        """
        cwd = resolve_cwd(target, sessions.list_sessions())
        with self._lock:
            handle = self._handles.get(cwd)
            if handle is None:
                handle = self._handles[cwd] = Peer(cwd, start=start)
            elif start:
                handle.start = True
            return handle


peers = _Peers()
