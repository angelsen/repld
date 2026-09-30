"""Unix-socket IPC server (kernel side).

NDJSON wire protocol: one JSON-RPC object per line, \\n-terminated. The socket
is the session — there is no session id. What sits on the other end is
`bridge.py`, a stateful proxy rather than a byte-pipe; see its module docstring
for why it can't be one.

Each connection gets a reader thread (parses NDJSON, dispatches via handler)
and on-demand writes (held under a per-session lock). `broadcast_channel()`
delivers server-initiated notifications (channel pushes) to all connected
sessions; `post_to()` targets one.

Scope: this module is the socket layer only. Where state files live is
`paths.py`; how they're written and validated is `state.py`.
"""

import json
import os
import socket
import struct
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from .core_schemas import BRIDGE_GOODBYE_METHOD, BRIDGE_REBIND_METHOD
from .core_schemas import error as _error
from .core_schemas import notification as _notification
from .state import read_lock

Handler = Callable[[dict, "Session"], dict | None]


def connect_to_kernel(lock_path: Path) -> tuple[socket.socket, dict] | str:
    """Read lockfile, validate kernel pid, connect unix socket.

    Returns (sock, lock_info) on success, or an error message string on failure.
    Used by both ``bridge`` and ``exec`` subcommands.
    """
    lock = read_lock(lock_path)
    if isinstance(lock, str):
        return lock
    sock_path = lock.get("socket_path")
    if not sock_path:
        return f"{lock_path.name} missing socket_path"

    sock_resolved = Path(sock_path)
    if not sock_resolved.is_absolute():
        kernel_cwd = lock.get("cwd")
        base = Path(kernel_cwd) if kernel_cwd else lock_path.parent
        sock_resolved = base / sock_resolved

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(str(sock_resolved))
    except OSError as e:
        # Closed rather than dropped: `bridge._reconnect` polls through here up
        # to 50 times per spawn, every one of them failing while the kernel
        # comes up, and returning the message alone left each socket to
        # refcounting with a ResourceWarning apiece. `_connect_excluding`
        # closes on its own reject path, so this was the asymmetry, not the
        # intent.
        sock.close()
        return f"cannot connect to kernel socket {sock_path}: {e}"

    return sock, lock


def _peer_pid(sock: socket.socket) -> int | None:
    """The connecting process's pid via SO_PEERCRED; None where that's unavailable."""
    opt = getattr(socket, "SO_PEERCRED", None)
    if opt is None:
        return None
    try:
        pid, _uid, _gid = struct.unpack(
            "3i", sock.getsockopt(socket.SOL_SOCKET, opt, 12)
        )
    except OSError:
        return None
    return pid or None


def _ancestry(pid: int) -> list[int]:
    """`pid` and its ancestors, nearest first, via /proc; [] if unreadable."""
    chain: list[int] = []
    while pid > 1 and pid not in chain:
        chain.append(pid)
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            break
        # comm (field 2) may itself contain spaces and parens — split after the last ')'.
        pid = int(stat.rsplit(")", 1)[1].split()[1])
    return chain


class Rebind(NamedTuple):
    old_id: str | None
    flushed: int  # parked pushes this rebind delivered to the successor


class Session:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.peer_pid = _peer_pid(sock)
        self.rfile = sock.makefile("r", encoding="utf-8")
        self.wfile = sock.makefile("w", encoding="utf-8")
        self.write_lock = threading.Lock()
        self.initialized = False
        # Channel notifications received before the client sends
        # notifications/initialized are queued here, then flushed when
        # set_initialized() is called. Replaces the prototype's
        # threading.Timer(1.0) retry hack.
        self.pending: list[dict] = []
        # Channel notifications are held here instead of written while
        # `parked` is set — see `park()`.
        self.parked = False
        self.parked_queue: list[tuple[dict, Callable[[bool], None] | None]] = []
        self.park_deadline: float | None = None
        self._park_timer: threading.Timer | None = None
        self._closed = False
        # Set by Server.register_claude_session once `initialize` carries the
        # bridge-identity fields (core_schemas.BRIDGE_SESSION_ID_KEY etc.).
        # None for a non-Claude-Code client or a bridge run by hand.
        self.claude_session_id: str | None = None
        self.claude_project_dir: str | None = None
        self.claude_session_kind: str | None = None

    @property
    def closed(self) -> bool:
        return self._closed

    def _write_msg(self, msg: dict) -> bool:
        """Write one NDJSON line + flush; close the session on I/O failure.

        Caller must hold write_lock. Returns whether the write succeeded —
        `_unpark_and_flush` needs this to know which queued messages actually
        went out before reporting outcomes through `on_parked_flush`.
        """
        try:
            self.wfile.write(json.dumps(msg) + "\n")
            self.wfile.flush()
            return True
        except (BrokenPipeError, OSError, ValueError):
            self._close_locked()
            return False

    def write(self, msg: dict) -> None:
        with self.write_lock:
            if self._closed:
                return
            self._write_msg(msg)

    def post_channel(
        self, msg: dict, *, on_parked_flush: "Callable[[bool], None] | None" = None
    ) -> bool:
        """Server-initiated notification (channel push).

        Queued until the session is marked initialized, then queued again
        (separately) while parked. Normal responses (to client requests)
        should use write() directly.

        Returns False only when the message was queued because this
        connection is currently parked — the caller's usual "delivered"
        reading (True otherwise: written now, or queued only pending
        notifications/initialized, which is a startup-only gap nothing has
        ever needed to distinguish from delivered) would otherwise say a push
        landed when it is still held. `on_parked_flush`, if given, fires
        with whether the write actually succeeded once the park releases
        (`unpark()` or its own timeout) — not called at all for any other
        path, including a session that's already closed.
        """
        with self.write_lock:
            if self._closed:
                return True
            if not self.initialized:
                self.pending.append(msg)
                return True
            if self.parked:
                self.parked_queue.append((msg, on_parked_flush))
                return False
            self._write_msg(msg)
            return True

    def park(self, timeout_s: float) -> None:
        """Hold channel pushes destined for this connection instead of
        writing them, until `unpark()` or `timeout_s` elapses.

        For the gap between a session's handoff recap and the `/clear` that
        rebinds it (`Server.rebind_claude_session` calls `unpark()`): a push
        landing in that gap would otherwise wake the conversation that has
        already decided to leave, in a context about to be dropped. On
        timeout with no rebind, delivers anyway to whatever conversation the
        connection currently has — a handoff that never gets its clear must
        not lose the push outright. Re-parking an already-parked connection
        is a no-op; it keeps the original deadline.
        """
        with self.write_lock:
            if self._closed or self.parked:
                return
            self.parked = True
            self.park_deadline = time.monotonic() + timeout_s
            timer = threading.Timer(timeout_s, self._unpark_and_flush)
            timer.daemon = True
            self._park_timer = timer
        timer.start()

    def unpark(self) -> int:
        """Release a park early and flush whatever queued while it held, in
        order. Returns how many pushes this call wrote; 0 if nothing is
        parked (including a park the timeout already released)."""
        return self._unpark_and_flush()

    def _unpark_and_flush(self) -> int:
        """Shared body of `unpark()` and the park timeout: whichever runs
        first does the flush, the other's guard makes it a no-op — both can
        race in from different threads (an explicit rebind vs. the Timer).

        Callbacks fire after `write_lock` is released — they run arbitrary
        caller code (`tasks.mark_push_delivered` today), and calling that
        while still holding a lock this module's own `write()`/`post_channel`
        need is an unnecessary way to invite a deadlock later.
        """
        callbacks: list[tuple[Callable[[bool], None], bool]] = []
        flushed = 0
        with self.write_lock:
            if self._closed or not self.parked:
                return 0
            self.parked = False
            self.park_deadline = None
            if self._park_timer is not None:
                self._park_timer.cancel()
                self._park_timer = None
            queued, self.parked_queue = self.parked_queue, []
            for msg, cb in queued:
                ok = (not self._closed) and self._write_msg(msg)
                flushed += ok
                if cb is not None:
                    callbacks.append((cb, ok))
        for cb, ok in callbacks:
            try:
                cb(ok)
            except Exception:
                pass  # a broken callback must not break the flush
        return flushed

    def set_initialized(self) -> None:
        with self.write_lock:
            if self._closed or self.initialized:
                return
            self.initialized = True
            pending, self.pending = self.pending, []
            for msg in pending:
                self._write_msg(msg)
                if self._closed:
                    return

    def _close_locked(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._park_timer is not None:
            self._park_timer.cancel()
            self._park_timer = None
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        # Close the makefile() wrappers explicitly. Left to GC/interpreter-
        # shutdown finalization instead, their buffered close() flushing
        # against the now-dead socket surfaces as an uncatchable "Exception
        # ignored in" BrokenPipeError instead of being swallowed here.
        for f in (self.wfile, self.rfile):
            try:
                f.close()
            except (BrokenPipeError, OSError, ValueError):
                pass

    def close(self) -> None:
        with self.write_lock:
            self._close_locked()

    def say_goodbye(self) -> None:
        """Mark the coming EOF as a clean shutdown. Skips `pending`/park on purpose:
        it is addressed to the bridge, never the client."""
        with self.write_lock:
            if not self._closed:
                self._write_msg(_notification(BRIDGE_GOODBYE_METHOD, {}))


class Server:
    def __init__(self, socket_path: Path, handler: Handler):
        self.socket_path = Path(socket_path)
        self.handler = handler
        self.sock: socket.socket | None = None
        self.accept_thread: threading.Thread | None = None
        self.sessions: set[Session] = set()
        self.sessions_lock = threading.Lock()
        # claude_session_id -> Session, same lock as `sessions` since both
        # mutate together on register/disconnect.
        self._claude_sessions: dict[str, Session] = {}
        self._stop = False

    def start(self) -> None:
        if self.socket_path.exists():
            self.socket_path.unlink()
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        # umask around the bind rather than a chmod after it: `bind()` creates
        # the node with the process umask applied, so chmod-ing afterwards
        # leaves a window in which the socket is connectable by anyone. The
        # 0700 project directory is the real barrier — this is the same
        # defence in depth every other runtime file gets (`state.open_private`,
        # `atomic_write_json(chmod=…)`).
        old_umask = os.umask(0o177)
        try:
            self.sock.bind(str(self.socket_path))
        finally:
            os.umask(old_umask)
        self.sock.listen(8)
        self.accept_thread = threading.Thread(
            target=self._accept_loop, daemon=True, name="repld-ipc-accept"
        )
        self.accept_thread.start()

    def _accept_loop(self) -> None:
        assert self.sock is not None
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            session = Session(conn)
            with self.sessions_lock:
                self.sessions.add(session)
            threading.Thread(
                target=self._read_loop,
                args=(session,),
                daemon=True,
                name="repld-ipc-reader",
            ).start()

    def _read_loop(self, session: Session) -> None:
        try:
            for line in session.rfile:
                line = line.strip()
                if not line:
                    continue
                try:
                    req = json.loads(line)
                except json.JSONDecodeError:
                    continue
                try:
                    resp = self.handler(req, session)
                except Exception as e:
                    rid = req.get("id")
                    if rid is not None:
                        resp = _error(rid, -32603, f"internal: {e!r}")
                    else:
                        resp = None
                if resp is not None:
                    session.write(resp)
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            with self.sessions_lock:
                self.sessions.discard(session)
                # Only if it's still this session — a reconnect can have
                # already replaced the entry via register_claude_session.
                if (
                    session.claude_session_id is not None
                    and self._claude_sessions.get(session.claude_session_id) is session
                ):
                    del self._claude_sessions[session.claude_session_id]
            session.close()

    def broadcast_channel(self, msg: dict, *, exclude: "Session | None" = None) -> None:
        """Post a server-initiated notification to every connected session.

        Sessions that haven't sent notifications/initialized yet queue the
        message and flush it when they do.

        `exclude`: skip this one session — for a self-reported update whose
        caller already has the result synchronously (its own return value)
        and doesn't need the broadcast echoed back to itself, without
        downgrading everyone else's copy to a targeted per-session loop.
        """
        with self.sessions_lock:
            targets = list(self.sessions)
        for s in targets:
            if s is exclude:
                continue
            s.post_channel(msg)

    def post_to(
        self,
        session: Session,
        msg: dict,
        *,
        on_parked_flush: "Callable[[bool], None] | None" = None,
    ) -> bool | None:
        """Post to one session. False if it's gone; None if it's currently
        parked (queued, not yet on the wire — see `Session.park`;
        `on_parked_flush` reports the eventual outcome); True otherwise
        (written now, or queued only pending notifications/initialized).
        """
        with self.sessions_lock:
            live = session in self.sessions
        if not live or session.closed:
            return False
        delivered = session.post_channel(msg, on_parked_flush=on_parked_flush)
        if session.closed:
            return False
        return True if delivered else None

    def register_claude_session(
        self,
        session: Session,
        session_id: str,
        project_dir: str | None,
        session_kind: str | None = None,
    ) -> None:
        """Bind a Claude Code session id to this connection.

        Replaces any existing entry for the same id without unregistering
        it first — a reconnecting bridge (kernel restart) presents the same
        id on a fresh Session, and the old one is already gone or going.
        """
        session.claude_session_id = session_id
        session.claude_project_dir = project_dir
        session.claude_session_kind = session_kind
        with self.sessions_lock:
            self._claude_sessions[session_id] = session

    def rebind_claude_session(
        self, new_id: str, pid: int, old_id: str | None = None
    ) -> Rebind:
        """Re-register the Claude Code session sharing `pid`'s process tree under `new_id`.

        `old_id` names the connection being replaced; it disambiguates a pid
        whose nearest shared ancestor also carries a subagent's own connection.
        It must still share `pid`'s tree, else LookupError.

        Returns the id it replaced and how many parked pushes the rebind
        flushed to the successor (0 when nothing was parked, or when the park
        timeout had already delivered them). The match is the connected session whose
        bridge shares the nearest ancestor with `pid`, the one closest to that
        ancestor if several do; raises LookupError on no match and ValueError
        when two are equally close.
        """
        with self.sessions_lock:
            candidates = [
                (s, _ancestry(s.peer_pid))
                for s in self.sessions
                if s.claude_session_id is not None and s.peer_pid is not None
            ]
        for ancestor in _ancestry(pid):
            hits = [
                (s, chain.index(ancestor))
                for s, chain in candidates
                if ancestor in chain
            ]
            if old_id is not None:
                hits = [(s, d) for s, d in hits if s.claude_session_id == old_id]
            # A pane's bridge hangs directly off its claude; a nested one (a
            # `claude -p` the pane ran) is deeper under the same ancestor.
            if len(hits) > 1:
                nearest = min(d for _, d in hits)
                hits = [(s, d) for s, d in hits if d == nearest]
            if len(hits) > 1:
                ids = ", ".join(sorted(str(s.claude_session_id) for s, _ in hits))
                raise ValueError(
                    f"pid {pid} is ambiguous: ancestor {ancestor} is shared by {ids}"
                )
            if hits:
                break
        else:
            raise LookupError(
                f"no connected Claude Code session shares a process tree with pid {pid}"
            )
        session = hits[0][0]
        with self.sessions_lock:
            old_id = session.claude_session_id
            if old_id is not None and self._claude_sessions.get(old_id) is session:
                del self._claude_sessions[old_id]
            session.claude_session_id = new_id
            self._claude_sessions[new_id] = session
        # Release before posting anything: a park held for the handoff this
        # rebind completes must flush to the successor, and unconditionally
        # calling it costs nothing when nothing was parked (unpark() no-ops).
        flushed = session.unpark()
        # The bridge re-stamps `initialize` from this on every kernel restart,
        # or the next kernel would re-register the stale id.
        session.post_channel(
            _notification(BRIDGE_REBIND_METHOD, {"session_id": new_id})
        )
        return Rebind(old_id, flushed)

    def find_claude_session(self, session_id: str) -> Session | None:
        with self.sessions_lock:
            return self._claude_sessions.get(session_id)

    def list_claude_sessions(self) -> list[tuple[str | None, str | None, str | None]]:
        with self.sessions_lock:
            targets = list(self.sessions)
        return [
            (s.claude_session_id, s.claude_project_dir, s.claude_session_kind)
            for s in targets
        ]

    def stop(self) -> None:
        if self._stop:
            return
        self._stop = True
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        with self.sessions_lock:
            sessions = list(self.sessions)
            self.sessions.clear()
            self._claude_sessions.clear()
        for s in sessions:
            s.say_goodbye()
            s.close()
        try:
            self.socket_path.unlink()
        except OSError:
            pass


_server: Server | None = None


def start_server(socket_path: Path, handler: Handler) -> Server:
    global _server
    _server = Server(socket_path, handler)
    _server.start()
    return _server


def socket_path() -> Path:
    """This kernel's IPC socket — the `socket_path` that `repld status --json` reports.

    Every per-kernel state file is this path with another suffix, so `.parent`
    is where a gist writes files an outside reader finds by that key. It is not
    `paths.project_dir()` on a `--socket` or `--ephemeral` kernel.
    """
    if _server is None:
        raise RuntimeError("repld.socket_path() is only available inside a kernel")
    return _server.socket_path


def stop_server() -> None:
    if _server is not None:
        _server.stop()


def broadcast_channel(msg: dict, *, exclude: "Session | None" = None) -> None:
    if _server is not None:
        _server.broadcast_channel(msg, exclude=exclude)


def post_to(
    session: Session,
    msg: dict,
    *,
    on_parked_flush: "Callable[[bool], None] | None" = None,
) -> bool | None:
    """Deliver a server-initiated notification to one session only.

    False means the session disconnected — callers drop the message rather
    than falling back to a broadcast, which would leak one session's output
    into every other one. None means it's parked instead (see `Server.post_to`)
    — also not a broadcast case, just not yet on the wire.
    """
    if _server is None:
        return False
    return _server.post_to(session, msg, on_parked_flush=on_parked_flush)


def register_claude_session(
    session: Session,
    session_id: str,
    project_dir: str | None,
    session_kind: str | None = None,
) -> None:
    if _server is not None:
        _server.register_claude_session(session, session_id, project_dir, session_kind)


def rebind_claude_session(new_id: str, pid: int, old_id: str | None = None) -> Rebind:
    """See `Server.rebind_claude_session` — for a SessionStart hook after `/clear`,
    which keeps the MCP connection (and the bridge's env-derived id) alive."""
    if _server is None:
        raise LookupError("no IPC server in this process")
    return _server.rebind_claude_session(new_id, pid, old_id)


def park_pushes(session_id: str, timeout_s: float = 300) -> bool:
    """Hold channel pushes bound for the connection registered under
    `session_id` — see `Session.park`. Keyed on the connection rather than
    the id itself, since a push is addressed to the `Session` object;
    `rebind_claude_session` releases it, or call `unpark_pushes` directly if
    the handoff gets called off. False if no connected session currently
    carries that id.

    The 300s default clears the recap-to-`/clear` gaps a supervised clear
    takes (~200-230s); pass a larger `timeout_s` for a slower handoff.
    """
    session = find_claude_session(session_id)
    if session is None:
        return False
    session.park(timeout_s)
    return True


def unpark_pushes(session_id: str) -> bool:
    """Release a park early for the connection registered under
    `session_id`, without a rebind — for a handoff that gets called off: the
    same conversation continues and should get its held pushes at once
    rather than wait out the park's timeout. False if no connected session
    currently carries that id; a no-op (still True) if nothing is parked.
    """
    session = find_claude_session(session_id)
    if session is None:
        return False
    session.unpark()
    return True


def find_claude_session(session_id: str) -> Session | None:
    if _server is None:
        return None
    return _server.find_claude_session(session_id)


def list_claude_sessions() -> list[tuple[str | None, str | None, str | None]]:
    if _server is None:
        return []
    return _server.list_claude_sessions()
