#!/usr/bin/env python3
"""Probe: a stdlib MCP 2026-07-28 stdio front for a real repld kernel.

Not the product. It exists to measure what Claude Code does with repld's `exec`
as an MCP task (RUNLOG.md, "Session 1"). No `initialize`: `server/discover`,
`resultType`, and the `io.modelcontextprotocol/tasks` extension (`tasks/get`,
`tasks/update`, `tasks/cancel`). Every kernel request rides a connection of its
own, speaking the kernel's existing JSON-RPC (`tools/call exec`/`get_task`,
`tasks/cancel`, `requests/cancel`, `gates/list`, `gates/resolve`, `initialize`).
The task id handed to the client is the kernel's own, so any bridge process can
answer for it.

Environment:
    MCP2_KERNEL_SOCKET   the kernel's unix socket (required)
    MCP2_LOG             JSONL wire log, both directions plus the start-up env
    MCP2_INLINE_S        seconds `exec` waits before it answers as a task (2.0)
    MCP2_PAD             pad the instructions to this many characters with
                         numbered markers (the 4,096 cut row)
    MCP2_REGISTER        "1": hold one kernel connection registered with this
                         session's inbox socket as kind `bg`, so the kernel's
                         own `_write_inbox` carries `notify()` to it
    MCP2_SHARED          "1": one shared kernel connection for every request,
                         replies matched by id (the row that shows what the
                         kernel does with it)
    MCP2_GONE            "rpc_error": answer `tasks/get` for an unreachable
                         kernel with a JSON-RPC error; otherwise as a `failed`
                         task
"""

from __future__ import annotations

import base64
import json
import os
import socket
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

VERSION = "2026-07-28"
META = "io.modelcontextprotocol/"
TASKS = META + "tasks"
SERVER_INFO = {"name": "repld-mcp2-probe", "version": "0"}
KERNEL = os.environ["MCP2_KERNEL_SOCKET"]
LOG = os.environ.get("MCP2_LOG")
INLINE_S = float(os.environ.get("MCP2_INLINE_S", "2.0"))
POLL_MS = 1000

_out_lock = threading.Lock()
_log_lock = threading.Lock()
_created: dict[str, str] = {}
# Gates a client declined or cancelled. The kernel has no decline over
# `gates/resolve`, so the gate stays open there and is not shown again here.
_declined: set[str] = set()


def now() -> str:
    """The current UTC time as an ISO string, to the millisecond."""
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def log(direction: str, msg: object) -> None:
    """Append one row to the wire log, when `MCP2_LOG` names one."""
    if not LOG:
        return
    with _log_lock, open(LOG, "a") as f:
        row = {"t": now(), "mono": round(time.monotonic(), 3), "pid": os.getpid()}
        f.write(json.dumps({**row, "dir": direction, "msg": msg}) + "\n")


def send(msg: dict) -> None:
    """Write one JSON-RPC message to the client and log it."""
    with _out_lock:
        sys.stdout.write(json.dumps(msg, separators=(",", ":")) + "\n")
        sys.stdout.flush()
    log("out", msg)


class RpcError(Exception):
    """A JSON-RPC error to send back as it is."""

    def __init__(self, code: int, message: str, data: object = None) -> None:
        """Carry the error code, message and optional data."""
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


class KernelGone(Exception):
    """The kernel socket refused or closed mid-request."""


class Call:
    """One client `tools/call` in flight, so `notifications/cancelled` can reach it."""

    def __init__(self) -> None:
        """Start with nothing sent to the kernel yet."""
        self.cancelled = False
        self.kernel_rid: str | None = None
        self.task_id: str | None = None
        self.abort: Callable[[], None] | None = None


_calls: dict[object, Call] = {}


# ---------------------------------------------------------------------------
# Kernel side
# ---------------------------------------------------------------------------


class Shared:
    """One kernel connection for every request, replies matched by id."""

    def __init__(self) -> None:
        """Connect and start the one reader."""
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.connect(KERNEL)
        self.write_lock = threading.Lock()
        self.lock = threading.Lock()  # guards `waiters` and `closed`
        self.closed = False
        self.waiters: dict[str, tuple[threading.Event, list[dict]]] = {}
        threading.Thread(target=self._read, daemon=True).start()

    def request(self, req: dict, call: Call | None) -> dict:
        """Write `req` and wait for the reply carrying its id."""
        waiter = (threading.Event(), [])
        with self.lock:
            if self.closed:
                raise KernelGone("kernel closed the shared connection")
            self.waiters[req["id"]] = waiter
        if call is not None:
            call.abort = waiter[0].set
        with self.write_lock:
            self.sock.sendall((json.dumps(req) + "\n").encode())
        waiter[0].wait()
        if not waiter[1]:
            raise KernelGone("kernel closed the connection before answering")
        return waiter[1][0]

    def _read(self) -> None:
        """Hand each reply to its waiter; on EOF wake them all empty-handed."""
        global _shared
        try:
            for line in self.sock.makefile("r", encoding="utf-8"):
                msg = json.loads(line)
                with self.lock:
                    waiter = self.waiters.pop(msg.get("id"), None)
                if waiter:
                    waiter[1].append(msg)
                    waiter[0].set()
        except OSError:
            pass
        with self.lock:
            self.closed = True
            left = list(self.waiters.values())
        _shared = None
        for event, _ in left:
            event.set()


_shared: Shared | None = None
_shared_open = threading.Lock()


def kernel(method: str, params: dict, call: Call | None = None) -> dict:
    """One request to the kernel; its `result`, or RpcError for its `error`."""
    global _shared
    rid = f"p{time.monotonic_ns()}"
    req = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
    if call is not None:
        call.kernel_rid = rid
    log("kernel>", {"method": method, "params": params})
    try:
        if os.environ.get("MCP2_SHARED") == "1":
            with _shared_open:
                if _shared is None or _shared.closed:
                    _shared = Shared()
                shared = _shared
            msg = shared.request(req, call)
        else:
            msg = _roundtrip(req, call)
    except OSError as e:
        log("kernel<", {"gone": repr(e)})
        raise KernelGone(f"repld kernel unreachable: {e}") from None
    log("kernel<", msg)
    if "error" in msg:
        raise RpcError(msg["error"]["code"], msg["error"]["message"])
    return msg["result"]


def _roundtrip(req: dict, call: Call | None = None) -> dict:
    """Send one request on a connection of its own and return its reply."""
    with socket.socket(socket.AF_UNIX) as sock:
        sock.connect(KERNEL)
        if call is not None:
            call.abort = lambda: sock.shutdown(socket.SHUT_RDWR)
        sock.sendall((json.dumps(req) + "\n").encode())
        for line in sock.makefile("r", encoding="utf-8"):
            msg = json.loads(line)
            if msg.get("id") == req["id"]:
                return msg
    raise KernelGone("kernel closed the connection before answering")


def snapshot(task_id: str) -> dict | None:
    """The kernel's `get_task` snapshot, or None for an id it does not hold."""
    result = kernel(
        "tools/call", {"name": "get_task", "arguments": {"task_id": task_id}}
    )
    return result.get("structuredContent")


def open_gates() -> list[dict]:
    """The kernel's pending gates that no client has declined here."""
    gates = kernel("gates/list", {})["gates"]
    return [g for g in gates if g["gate_id"] not in _declined]


def register_inbox() -> None:
    """Hold a kernel connection that names this session's inbox socket."""
    params = {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "mcp2-probe", "version": "0"},
        "_claude_session_id": os.environ.get("CLAUDE_CODE_SESSION_ID"),
        "_claude_session_kind": "bg",
        "_claude_inbox_socket": os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET"),
    }
    sock = socket.socket(socket.AF_UNIX)
    sock.connect(KERNEL)
    req = {"jsonrpc": "2.0", "id": "register", "method": "initialize", "params": params}
    note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    sock.sendall((json.dumps(req) + "\n" + json.dumps(note) + "\n").encode())
    for line in sock.makefile("r", encoding="utf-8"):
        msg = json.loads(line)
        log("register" if msg.get("id") == "register" else "kernel-push", msg)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


def complete(**fields: object) -> dict:
    """A `resultType: complete` result carrying `fields`."""
    return {
        "resultType": "complete",
        "_meta": {META + "serverInfo": SERVER_INFO},
        **fields,
    }


def build_text_result(text: str, is_error: bool = False) -> dict:
    """A complete tool result holding one text block."""
    return complete(content=[{"type": "text", "text": text}], isError=is_error)


def parse_client_caps(params: dict) -> dict:
    """The capabilities this one request declares."""
    return params.get("_meta", {}).get(META + "clientCapabilities") or {}


def has_tasks_cap(params: dict) -> bool:
    """Whether this request declares the tasks extension."""
    return TASKS in (parse_client_caps(params).get("extensions") or {})


def format_snapshot(snap: dict) -> str:
    """A finished task's output as the model reads it, spill pointer included."""
    text = snap["text"].rstrip() or "(no output)"
    if snap.get("spill_path") and snap.get("truncated"):
        text += f"\n[full output: {snap['spill_path']}]"
    return text


def build_task(task_id: str, status: str, message: str = "") -> dict:
    """The Task object for a kernel task id in the given status."""
    created = _created.setdefault(task_id, now())
    fields = {
        "taskId": task_id,
        "status": status,
        "createdAt": created,
        "lastUpdatedAt": now(),
        "ttlMs": 3_600_000,
        "pollIntervalMs": POLL_MS,
    }
    if message:
        fields["statusMessage"] = message
    return fields


def build_gate_request(gate: dict) -> dict:
    """A kernel gate as a form-mode `elicitation/create` input request."""
    return {
        "method": "elicitation/create",
        "params": {
            "mode": "form",
            "message": f"[{gate['kind']}] {gate['prompt']}",
            "requestedSchema": gate["schema"],
        },
    }


def answer_gates(responses: dict) -> list[str]:
    """Resolve each accepted gate in the kernel; return the ids left open.

    Decline and cancel resolve nothing: `gates/resolve` takes a value only, and
    no task is cancelled, since `gates/list` does not say which task asked.
    """
    gates = {g["gate_id"]: g for g in open_gates()}
    left = []
    for gate_id, response in responses.items():
        if gate_id not in gates:
            continue
        if response.get("action") != "accept":
            _declined.add(gate_id)
            left.append(gate_id)
            continue
        content = response.get("content") or {}
        value = next(iter(content.values()), "")
        if gates[gate_id]["kind"] == "form":
            # `gates/resolve` takes a form's answer as one JSON object in a string.
            value = json.dumps(content)
        elif isinstance(value, bool):
            value = "y" if value else "n"
        kernel("gates/resolve", {"gate_id": gate_id, "value": str(value)})
    return left


# ---------------------------------------------------------------------------
# Methods
# ---------------------------------------------------------------------------


def instructions() -> str:
    """The kernel's own instructions, marked at the start and padded when `MCP2_PAD` asks."""
    try:
        text = kernel(
            "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}}
        )["instructions"]
    except KernelGone, RpcError, KeyError:
        text = "repld probe: no kernel answered for instructions."
    text = "MARK-START " + text
    pad = int(os.environ.get("MCP2_PAD", "0"))
    n = 0
    while len(text) < pad:
        n += 1
        text += f" MARK-{n:03d}@{len(text)}"
    return text


def discover(params: dict) -> dict:
    """Answer `server/discover`: one version, tools, the tasks extension, instructions."""
    return complete(
        supportedVersions=[VERSION],
        capabilities={"tools": {"listChanged": True}, "extensions": {TASKS: {}}},
        instructions=instructions(),
    )


EXEC_TOOL = {
    "name": "exec",
    "description": "Run Python in the repld kernel's shared __main__. Returns the output, "
    "or finishes later as a task when the cell outlasts `timeout` seconds.",
    "inputSchema": {
        "type": "object",
        "properties": {"code": {"type": "string"}, "timeout": {"type": "number"}},
        "required": ["code"],
    },
}


def build_tool_names() -> list[str]:
    """The kernel's tool names, sorted, for spotting a change."""
    tools = kernel("tools/list", {})["tools"]
    return sorted(t["name"] for t in tools)


def tools_list(params: dict) -> dict:
    """`exec` plus the kernel's gist tools; `browser_*` and the task tools are left out."""
    extra = []
    for tool in kernel("tools/list", {})["tools"]:
        name = tool["name"]
        if name in ("exec", "get_task", "cancel", "repld_restart"):
            continue
        if name.startswith("browser_"):
            continue
        keep = {k: tool[k] for k in ("name", "description", "inputSchema") if k in tool}
        extra.append(keep)
    return complete(tools=[EXEC_TOOL, *extra])


def tools_call(params: dict, call: Call) -> dict | None:
    """Run `exec` inline, or as a task past its wait; None once the client cancelled the call."""
    name, args = params.get("name"), params.get("arguments") or {}
    if name != "exec":
        result = kernel("tools/call", {"name": name, "arguments": args})
        return complete(content=result["content"], isError=bool(result.get("isError")))
    started = time.monotonic()
    if "requestState" in params:
        # The retry of a request answered `input_required`: the cell is already running.
        state = json.loads(base64.urlsafe_b64decode(params["requestState"]))
        call.task_id = state["task_id"]
        left = answer_gates(params.get("inputResponses") or {})
        if left:
            return build_text_result(
                f"[task {call.task_id} is still running: it waits on a human at gate "
                f"{', '.join(left)}, and this client asked nobody. Answer it with "
                f"`repld gate answer <gate_id> <value>`]"
            )
        return wait_inline(params, call, started)
    wait = float(args.get("timeout", INLINE_S))
    exec_args = {"code": args["code"], "timeout": wait}
    result = kernel("tools/call", {"name": "exec", "arguments": exec_args}, call)
    meta = result["_meta"]
    if meta["done"]:
        return complete(content=result["content"], isError=bool(result.get("isError")))
    call.task_id = meta["task_id"]
    if has_tasks_cap(params):
        return {
            "resultType": "task",
            **build_task(call.task_id, "working", f"running past {wait:g}s"),
        }
    return wait_inline(params, call, started)


def wait_inline(params: dict, call: Call, started: float) -> dict | None:
    """Hold a request with no tasks capability open until the cell ends or asks a human."""
    while True:
        snap = snapshot(call.task_id)
        if call.cancelled:
            return None  # a cancelled request gets no response, finished or not
        if snap["done"]:
            waited = time.monotonic() - started
            tail = f"[inline after {waited:.1f}s: this client takes no tasks]"
            return build_text_result(
                f"{format_snapshot(snap)}\n{tail}", bool(snap["exception"])
            )
        gates = open_gates() if "elicitation" in parse_client_caps(params) else []
        if gates:
            state = json.dumps({"task_id": call.task_id}).encode()
            return {
                "resultType": "input_required",
                "_meta": {META + "serverInfo": SERVER_INFO},
                "inputRequests": {g["gate_id"]: build_gate_request(g) for g in gates},
                "requestState": base64.urlsafe_b64encode(state).decode(),
            }
        time.sleep(0.3)


def cancel_call(params: dict) -> None:
    """Handle `notifications/cancelled`: stop the kernel cell behind an in-flight `tools/call`."""
    call = _calls.get(params.get("requestId"))
    if call is None:
        return
    call.cancelled = True
    if call.task_id is not None:
        kernel("tasks/cancel", {"task_id": call.task_id})
    elif call.kernel_rid is not None:
        # The kernel answers a cancelled `exec` with nothing, so its reader is released here.
        try:
            kernel("requests/cancel", {"request_id": call.kernel_rid})
        finally:
            try:
                call.abort()
            except OSError, TypeError:
                pass  # the request ended on its own first


def tasks_get(params: dict) -> dict:
    """A kernel task's state as a Task; an open gate makes it `input_required`."""
    task_id = params.get("taskId")
    try:
        snap = snapshot(task_id)
    except KernelGone as e:
        if os.environ.get("MCP2_GONE") == "rpc_error":
            raise RpcError(-32603, str(e)) from None
        fields = build_task(task_id, "failed", "kernel gone")
        return complete(**fields, error={"code": -32603, "message": str(e)})
    if snap is None:
        raise RpcError(-32602, f"Failed to retrieve task: Task not found: {task_id}")
    if not snap["done"]:
        gates = open_gates()
        if gates:
            # The bridge's own choice: `gates/list` does not say which task asked,
            # so every open gate is shown on every running task that is polled.
            requests = {g["gate_id"]: build_gate_request(g) for g in gates}
            fields = build_task(task_id, "input_required", "waiting on a human gate")
            return complete(**fields, inputRequests=requests)
        return complete(**build_task(task_id, "working"))
    exception = snap["exception"]
    if exception and "CancelledError" in exception:
        return complete(**build_task(task_id, "cancelled", "cancelled"))
    result = {
        "content": [{"type": "text", "text": format_snapshot(snap)}],
        "isError": bool(exception),
    }
    return complete(**build_task(task_id, "completed"), result=result)


def tasks_update(params: dict) -> dict:
    """Resolve the gates the responses accept; a declined gate stays open in the kernel."""
    answer_gates(params.get("inputResponses") or {})
    return complete()


def tasks_cancel(params: dict) -> dict:
    """Cancel the kernel task; any id is acknowledged, so a repeat is harmless."""
    kernel("tasks/cancel", {"task_id": params.get("taskId")})
    return complete()


def listen(req_id: object, params: dict) -> None:
    """Acknowledge a subscription and send `tools/list_changed` when the kernel list changes."""
    wanted = params.get("notifications") or {}
    granted = {k: v for k, v in wanted.items() if k == "toolsListChanged" and v}
    meta = {"_meta": {META + "subscriptionId": req_id}}
    send(
        {
            "jsonrpc": "2.0",
            "method": "notifications/subscriptions/acknowledged",
            "params": {**meta, "notifications": granted},
        }
    )
    if not granted:
        return
    seen = build_tool_names()
    while True:
        time.sleep(2)
        try:
            names = build_tool_names()
        except KernelGone, RpcError:
            continue
        if names != seen:
            seen = names
            send(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/tools/list_changed",
                    "params": meta,
                }
            )


METHODS = {
    "server/discover": discover,
    "tools/list": tools_list,
    "tasks/get": tasks_get,
    "tasks/update": tasks_update,
    "tasks/cancel": tasks_cancel,
    "resources/list": lambda p: complete(resources=[]),
    "resources/templates/list": lambda p: complete(resourceTemplates=[]),
    "prompts/list": lambda p: complete(prompts=[]),
}


def answer(method: str | None, rid: object, params: dict) -> dict | None:
    """The result for one request, or None when nothing more is owed on its id."""
    if params.get("_meta", {}).get(META + "protocolVersion") != VERSION:
        data = {"supported": [VERSION]}
        raise RpcError(-32022, "Unsupported protocol version", data)
    if method == "subscriptions/listen":
        listen(rid, params)
        return None
    if method == "tools/call":
        call = _calls[rid] = Call()
        try:
            return tools_call(params, call)
        except KernelGone as e:
            # A cancelled request gets no response.
            return None if call.cancelled else build_text_result(str(e), True)
        finally:
            del _calls[rid]
    if method not in METHODS:
        raise RpcError(-32601, f"Method not found: {method}")
    return METHODS[method](params)


def handle(msg: dict) -> None:
    """Answer one client message on its own thread."""
    method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if rid is None:
        if method == "notifications/cancelled":
            cancel_call(params)
        return
    try:
        result = answer(method, rid, params)
    except RpcError as e:
        error = {"code": e.code, "message": e.message}
        if e.data is not None:
            error["data"] = e.data
        send({"jsonrpc": "2.0", "id": rid, "error": error})
        return
    except KernelGone as e:
        error = {"code": -32603, "message": str(e)}
        send({"jsonrpc": "2.0", "id": rid, "error": error})
        return
    if result is not None:
        send({"jsonrpc": "2.0", "id": rid, "result": result})


def main() -> None:
    """Log the start-up environment, then serve stdin until it closes."""
    names = sorted(k for k in os.environ if k.startswith(("CLAUDE", "MCP2", "REPLD")))
    shown = ("CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_SESSION_ID", "CLAUDE_JOB_DIR")
    shown += ("CLAUDE_PROJECT_DIR", "CLAUDE_CODE_ENTRYPOINT", "CLAUDECODE")
    env = {
        "names": names,
        "ppid": os.getppid(),
        **{k: os.environ.get(k) for k in shown},
    }
    log("start", env)
    if os.environ.get("MCP2_REGISTER") == "1":
        threading.Thread(target=register_inbox, daemon=True).start()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        log("in", msg)
        threading.Thread(target=handle, args=(msg,), daemon=True).start()
    log("eof", {})


if __name__ == "__main__":
    main()
