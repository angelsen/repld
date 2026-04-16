"""Unix-socket IPC server (kernel side).

NDJSON wire protocol: one JSON-RPC object per line, \\n-terminated. The bridge
(src/repld/bridge.py) is a dumb byte-pipe that forwards verbatim between its
stdio and this socket. The socket is the session — there is no session id.

Each connection gets a reader thread (parses NDJSON, dispatches via handler)
and on-demand writes (held under a per-session lock). `broadcast()` delivers
server-initiated notifications (channel pushes) to all connected sessions.
"""

import json
import os
import socket
import threading
from pathlib import Path
from typing import Callable

Handler = Callable[[dict, "Session"], dict | None]


class Session:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.rfile = sock.makefile("r", encoding="utf-8")
        self.wfile = sock.makefile("w", encoding="utf-8")
        self.write_lock = threading.Lock()
        self.initialized = False
        # Channel notifications received before the client sends
        # notifications/initialized are queued here, then flushed when
        # set_initialized() is called. Replaces the prototype's
        # threading.Timer(1.0) retry hack.
        self.pending: list[dict] = []
        self._closed = False

    def write(self, msg: dict) -> None:
        with self.write_lock:
            if self._closed:
                return
            try:
                self.wfile.write(json.dumps(msg) + "\n")
                self.wfile.flush()
            except (BrokenPipeError, OSError, ValueError):
                self._close_locked()

    def post_channel(self, msg: dict) -> None:
        """Server-initiated notification (channel push).

        Queued until the session is marked initialized. Normal responses
        (to client requests) should use write() directly.
        """
        with self.write_lock:
            if self._closed:
                return
            if not self.initialized:
                self.pending.append(msg)
                return
            try:
                self.wfile.write(json.dumps(msg) + "\n")
                self.wfile.flush()
            except (BrokenPipeError, OSError, ValueError):
                self._close_locked()

    def set_initialized(self) -> None:
        with self.write_lock:
            if self._closed or self.initialized:
                return
            self.initialized = True
            pending, self.pending = self.pending, []
            for msg in pending:
                try:
                    self.wfile.write(json.dumps(msg) + "\n")
                except (BrokenPipeError, OSError, ValueError):
                    self._close_locked()
                    return
            try:
                self.wfile.flush()
            except (BrokenPipeError, OSError, ValueError):
                self._close_locked()

    def _close_locked(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    def close(self) -> None:
        with self.write_lock:
            self._close_locked()


class Server:
    def __init__(self, socket_path: Path, handler: Handler):
        self.socket_path = Path(socket_path)
        self.handler = handler
        self.sock: socket.socket | None = None
        self.accept_thread: threading.Thread | None = None
        self.sessions: set[Session] = set()
        self.sessions_lock = threading.Lock()
        self._stop = False

    def start(self) -> None:
        if self.socket_path.exists():
            self.socket_path.unlink()
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
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
                        resp = {
                            "jsonrpc": "2.0",
                            "id": rid,
                            "error": {"code": -32603, "message": f"internal: {e!r}"},
                        }
                    else:
                        resp = None
                if resp is not None:
                    session.write(resp)
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            with self.sessions_lock:
                self.sessions.discard(session)
            session.close()

    def broadcast_channel(self, msg: dict) -> None:
        """Post a server-initiated notification to every connected session.

        Sessions that haven't sent notifications/initialized yet queue the
        message and flush it when they do.
        """
        with self.sessions_lock:
            targets = list(self.sessions)
        for s in targets:
            s.post_channel(msg)

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
        for s in sessions:
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


def stop_server() -> None:
    if _server is not None:
        _server.stop()


def broadcast_channel(msg: dict) -> None:
    if _server is not None:
        _server.broadcast_channel(msg)
