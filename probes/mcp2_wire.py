#!/usr/bin/env python3
"""Probe: drive probes/mcp2_bridge.py over stdio against a real repld kernel, no Claude.

    MCP2_KERNEL_SOCKET=/run/user/.../kernel.sock python3 probes/mcp2_wire.py [row ...]

Each row prints what was sent and what came back; nothing is asserted. With no
argument every row but `dead` runs.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

P = "io.modelcontextprotocol/"
TASKS = P + "tasks"
TASK_CAPS = {"extensions": {TASKS: {}}}
BRIDGE = str(Path(__file__).with_name("mcp2_bridge.py"))
KERNEL = os.environ["MCP2_KERNEL_SOCKET"]


class Bridge:
    """One spike bridge process driven over its stdio."""

    def __init__(self, **env: str) -> None:
        """Start the bridge with `env` laid over this process's environment."""
        self.p = subprocess.Popen(
            [sys.executable, BRIDGE],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            env={**os.environ, **env},
        )
        self.n = 0

    def send(
        self,
        method: str,
        params: dict | None = None,
        caps: dict | None = None,
        notification: bool = False,
    ) -> int:
        """Write one message with `caps` as the request's capabilities; return its id."""
        self.n += 1
        meta = {
            P + "protocolVersion": "2026-07-28",
            P + "clientInfo": {"name": "wire", "version": "0"},
            P + "clientCapabilities": TASK_CAPS if caps is None else caps,
        }
        req = {"jsonrpc": "2.0", "method": method}
        if not notification:
            req["id"] = self.n
        req["params"] = {**(params or {}), "_meta": meta}
        self.p.stdin.write(json.dumps(req) + "\n")
        self.p.stdin.flush()
        return self.n

    def call(
        self,
        method: str,
        params: dict | None = None,
        tasks: bool = True,
        show: bool = True,
        caps: dict | None = None,
    ) -> dict:
        """Send one request and return its result, or `{"error": ...}`."""
        if caps is None:
            caps = TASK_CAPS if tasks else {}
        start = time.monotonic()
        rid = self.send(method, params, caps)
        while True:
            msg = json.loads(self.p.stdout.readline())
            if msg.get("id") == rid:
                out = msg.get("result") or {"error": msg["error"]}
                out.pop("_meta", None)
                if show:
                    took = time.monotonic() - start
                    note = (
                        "" if TASKS in caps.get("extensions", {}) else "(no tasks cap) "
                    )
                    print(f"  {method} {note}{took:.2f}s -> {json.dumps(out)[:420]}")
                return out

    def exec(self, code: str, tasks: bool = True, **args: float) -> dict:
        """Call the `exec` tool with `code`."""
        return self.call(
            "tools/call", {"name": "exec", "arguments": {"code": code, **args}}, tasks
        )

    def get(self, task_id: str, show: bool = True) -> dict:
        """Ask `tasks/get` once."""
        return self.call("tasks/get", {"taskId": task_id}, show=show)

    def until(self, task_id: str, *statuses: str, limit: float = 20) -> dict:
        """Poll `tasks/get` until one of `statuses`, an error, or `limit` seconds."""
        end = time.monotonic() + limit
        while time.monotonic() < end:
            out = self.get(task_id, show=False)
            if out.get("status") in statuses or "error" in out:
                break
            time.sleep(0.2)
        print(f"  tasks/get (polled) -> {json.dumps(out)[:420]}")
        return out

    def close(self) -> None:
        """Close stdin and wait for the bridge to exit."""
        self.p.stdin.close()
        self.p.wait(timeout=10)


def raw(sock: socket.socket, rid: str, method: str, params: dict) -> None:
    """Write one bare JSON-RPC request to a kernel socket."""
    sock.sendall(
        (
            json.dumps(
                {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
            )
            + "\n"
        ).encode()
    )


def kernel_gates() -> list[dict]:
    """The kernel's own list of pending gates, asked on a socket of this script's."""
    with socket.socket(socket.AF_UNIX) as s:
        s.connect(KERNEL)
        raw(s, "g", "gates/list", {})
        return json.loads(s.makefile().readline())["result"]["gates"]


def row_discover() -> None:
    """`server/discover` and `tools/list` as the bridge answers them."""
    b = Bridge()
    out = b.call("server/discover", show=False)
    text = out.pop("instructions")
    print(
        f"  server/discover -> {json.dumps(out)}; instructions {len(text)} chars, head {text[:60]!r}"
    )
    b.call("tools/list")
    b.close()


def row_inline() -> None:
    """Rows 6 and 7: inline when the cell ends inside the wait, with and without the capability."""
    b = Bridge()
    b.exec("1+1")
    b.exec("import asyncio; await asyncio.sleep(1); 'slept 1'")
    b.exec("import asyncio; await asyncio.sleep(4); 'slept 4'", tasks=False)
    b.exec("raise ValueError('boom')")
    b.close()


def row_task() -> None:
    """A cell past the wait: task, poll, result; a late raise; an unknown id."""
    b = Bridge()
    t = b.exec("import asyncio; await asyncio.sleep(3); print('out'); 'slept 3'")
    b.get(t["taskId"])
    b.until(t["taskId"], "completed")
    t = b.exec(
        "import asyncio; await asyncio.sleep(2.5); raise ValueError('late boom')"
    )
    b.until(t["taskId"], "completed", "failed")
    b.get("no-such-task")
    b.close()


def row_cancel() -> None:
    """Row 8: cancel twice, cancel after the task finished, cancel of an unknown id."""
    b = Bridge()
    code = "import asyncio\ntry:\n    await asyncio.sleep(60)\nfinally:\n    CANCEL_SEEN = True"
    t = b.exec(code)
    b.call("tasks/cancel", {"taskId": t["taskId"]})
    b.call("tasks/cancel", {"taskId": t["taskId"]})
    b.until(t["taskId"], "cancelled", "completed", "failed")
    b.exec("CANCEL_SEEN")
    t = b.exec("import asyncio; await asyncio.sleep(2.3); 'finished'")
    b.until(t["taskId"], "completed")
    b.call("tasks/cancel", {"taskId": t["taskId"]})
    b.get(t["taskId"])
    b.call("tasks/cancel", {"taskId": "no-such-task"})
    b.close()


def row_gate() -> None:
    """Row 9: confirm, choose, ask inside a task; two gates in one cell; a gate from defer()."""
    b = Bridge()

    def answer(task_id: str, content: dict | None, action: str = "accept") -> None:
        """Wait for a gate on the task and answer it."""
        out = b.until(task_id, "input_required", "completed", "cancelled")
        for gate_id in out.get("inputRequests", {}):
            response = {
                "action": action,
                **({"content": content} if content is not None else {}),
            }
            b.call(
                "tasks/update",
                {"taskId": task_id, "inputResponses": {gate_id: response}},
            )

    t = b.exec("r = await confirm('wire: go?'); f'confirm={r}'")
    answer(t["taskId"], {"confirmed": True})
    b.until(t["taskId"], "completed")
    t = b.exec("r = await choose('wire: pick', ['red', 'blue']); f'choose={r}'")
    out = b.until(t["taskId"], "input_required")
    schema = next(iter(out["inputRequests"].values()))["params"]["requestedSchema"]
    field = next(iter(schema["properties"]))
    answer(t["taskId"], {field: "blue"})
    b.until(t["taskId"], "completed")
    t = b.exec(
        "a = await ask('wire: name?'); c = await confirm(f'wire: {a} ok?'); f'ask={a} confirm={c}'"
    )
    answer(t["taskId"], {"x": "Ada"})
    time.sleep(0.5)
    answer(t["taskId"], {"x": False})
    b.until(t["taskId"], "completed")
    print("  declined ask:")
    t = b.exec("a = await ask('wire: declined?'); f'ask={a}'")
    answer(t["taskId"], None, action="decline")
    b.until(t["taskId"], "completed", "cancelled", "failed", limit=2)
    print(f"  kernel gates/list -> {json.dumps(kernel_gates())[:200]}")
    b.call("tasks/cancel", {"taskId": t["taskId"]})
    print("  gate raised by defer(), cell already returned:")
    b.exec(
        "async def _later():\n    return await confirm('wire: deferred gate?')\nDT = defer(_later(), 'gatey'); 'cell returned'"
    )
    time.sleep(0.5)
    print(f"  kernel gates/list -> {json.dumps(kernel_gates())[:300]}")
    t = b.exec("import asyncio; await asyncio.sleep(4); 'unrelated task'")
    out = b.get(t["taskId"])
    for gate_id in out.get("inputRequests", {}):
        b.call(
            "tasks/update",
            {
                "taskId": t["taskId"],
                "inputResponses": {
                    gate_id: {"action": "accept", "content": {"x": True}}
                },
            },
        )
    b.until(t["taskId"], "completed")
    b.close()


def row_notask_gate() -> None:
    """Row 9e: a gate in a cell whose client declares elicitation and no tasks, as `claude -p` does."""
    b = Bridge()
    caps = {"elicitation": {"form": {}, "url": {}}}
    for action in ("cancel", "accept"):
        args = {"code": f"r = await confirm('wire: {action}?'); f'confirm={{r}}'"}
        params = {"name": "exec", "arguments": args}
        out = b.call("tools/call", params, caps=caps)
        gate_id = next(iter(out["inputRequests"]))
        response = {"action": action}
        if action == "accept":
            response["content"] = {"answer": True}
        retry = {**params, "inputResponses": {gate_id: response}}
        b.call("tools/call", {**retry, "requestState": out["requestState"]}, caps=caps)
        print(f"  kernel gates/list -> {json.dumps(kernel_gates())[:200]}")
    for gate in kernel_gates():
        with socket.socket(socket.AF_UNIX) as s:
            s.connect(KERNEL)
            raw(s, "r", "gates/resolve", {"gate_id": gate["gate_id"], "value": "n"})
            print(f"  gates/resolve by hand -> {s.makefile().readline().strip()[:160]}")
    b.close()


def row_client_cancel() -> None:
    """Rows 7 and 9e: `notifications/cancelled` for a `tools/call` held open, in both phases."""
    b = Bridge()
    for label, timeout in (("inside the kernel's own wait", 30), ("past it", 1)):
        code = f"import asyncio\ntry:\n    await asyncio.sleep(60)\nfinally:\n    SEEN_{timeout} = True"
        args = {"code": code, "timeout": timeout}
        rid = b.send("tools/call", {"name": "exec", "arguments": args}, caps={})
        time.sleep(2.5)
        b.send("notifications/cancelled", {"requestId": rid}, notification=True)
        time.sleep(1)
        check = b.send(
            "tools/call", {"name": "exec", "arguments": {"code": f"SEEN_{timeout}"}}
        )
        while True:
            msg = json.loads(b.p.stdout.readline())
            if msg.get("id") == rid:
                raise SystemExit(
                    f"  cancelled {label}: request {rid} was still answered: {msg}"
                )
            if msg.get("id") == check:
                text = msg["result"]["content"][0]["text"]
                print(
                    f"  cancelled {label}: no response on id {rid}; cell's finally ran: {text}"
                )
                break
    b.close()


def row_fresh() -> None:
    """Row 2: a second bridge process answers tasks/get for a task the first one started."""
    a = Bridge()
    t = a.exec("import asyncio; await asyncio.sleep(3); 'held by the kernel'")
    a.close()
    print("  first bridge exited; a fresh bridge asks:")
    b = Bridge()
    b.get(t["taskId"])
    b.until(t["taskId"], "completed")
    b.close()


def row_shared() -> None:
    """Row 14: one kernel connection for everything (replies matched by id), then one per request."""
    for shared in ("1", "0"):
        b = Bridge(MCP2_SHARED=shared)
        t = b.exec("import asyncio; await asyncio.sleep(8); 'long'")
        start = time.monotonic()
        # A 3 s inline exec in flight on the same bridge while tasks/get is asked.
        code = "import asyncio; await asyncio.sleep(3); 'inline 3'"
        b.send(
            "tools/call", {"name": "exec", "arguments": {"code": code, "timeout": 5}}
        )
        time.sleep(0.2)
        get = b.send("tasks/get", {"taskId": t["taskId"]})
        for _ in range(2):
            msg = json.loads(b.p.stdout.readline())
            what = "tasks/get" if msg["id"] == get else "inline exec"
            print(
                f"  shared={shared}: {what} answered at {time.monotonic() - start:.2f}s"
            )
        b.call("tasks/cancel", {"taskId": t["taskId"]}, show=False)
        b.close()


def row_wait() -> None:
    """Row 6: what the inline wait costs on the wire for cells of several lengths."""
    b = Bridge()
    for seconds in (0, 0.5, 1.5, 1.9, 2.1):
        start = time.monotonic()
        out = b.exec(f"import asyncio; await asyncio.sleep({seconds}); 'x'")
        kind = out.get("resultType")
        print(
            f"  cell {seconds}s, wait 2.0s -> {kind} after {time.monotonic() - start:.2f}s"
        )
        if kind == "task":
            b.until(out["taskId"], "completed")
    b.close()


def row_secret() -> None:
    """Row 9: a secret gate. `gates/list` carries no page URL and `gates/resolve` refuses it."""
    b = Bridge()
    t = b.exec("s = await ask('wire: token?', secret=True); f'len={len(s)}'")
    out = b.until(t["taskId"], "input_required", "completed", "failed")
    for gate_id in out.get("inputRequests", {}):
        response = {"action": "accept", "content": {"answer": "hunter2"}}
        b.call(
            "tasks/update",
            {"taskId": t["taskId"], "inputResponses": {gate_id: response}},
        )
    b.until(t["taskId"], "completed", "failed", "cancelled", limit=3)
    b.call("tasks/cancel", {"taskId": t["taskId"]})
    b.close()


def row_dead() -> None:
    """Breaks 2 and 3: `tasks/get` for a kernel that died, both answers, then for its successor.

    Kills the kernel `MCP2_KERNEL_SOCKET` names. `MCP2_RESTART_CMD` is a shell
    command that starts a kernel at the same socket again.
    """
    lock = json.loads(Path(KERNEL).with_suffix(".lock").read_text())
    a, b = Bridge(), Bridge(MCP2_GONE="rpc_error")
    t = a.exec("import asyncio; await asyncio.sleep(60); 'never'")
    os.kill(lock["pid"], 9)
    time.sleep(1)
    print(f"  kernel pid {lock['pid']} killed; answered as a task:")
    a.get(t["taskId"])
    print("  the same death with MCP2_GONE=rpc_error:")
    b.get(t["taskId"])
    subprocess.run(os.environ["MCP2_RESTART_CMD"], shell=True, check=True)
    print("  a new kernel at the same socket, the old id:")
    a.get(t["taskId"])
    a.close()
    b.close()


def row_form() -> None:
    """Plan change 7: `form()` inside a task; an answer that fits, then one the kernel refuses."""
    b = Bridge()
    code = (
        "r = await form('wire: deploy?', {'env': ['staging', 'prod'], 'replicas': "
        "{'type': 'integer', 'minimum': 1, 'maximum': 5}, 'note': str, 'dry_run': bool}, "
        "required=['env', 'replicas']); f'form={r}'"
    )
    good = {"env": "prod", "replicas": 3, "dry_run": True}
    over = {**good, "replicas": 9}
    for label, content in (("fits", good), ("replicas over the maximum", over)):
        print(f"  {label}:")
        t = b.exec(code)
        out = b.until(t["taskId"], "input_required")
        gate_id = next(iter(out["inputRequests"]))
        responses = {gate_id: {"action": "accept", "content": content}}
        b.call("tasks/update", {"taskId": t["taskId"], "inputResponses": responses})
        b.until(t["taskId"], "completed", "failed", limit=2)
        b.call("tasks/cancel", {"taskId": t["taskId"]}, show=False)
    b.close()


ROWS = {
    "dead": row_dead,
    "form": row_form,
    "discover": row_discover,
    "inline": row_inline,
    "task": row_task,
    "cancel": row_cancel,
    "gate": row_gate,
    "notask_gate": row_notask_gate,
    "client_cancel": row_client_cancel,
    "fresh": row_fresh,
    "shared": row_shared,
    "wait": row_wait,
    "secret": row_secret,
}

if __name__ == "__main__":
    # `dead` kills the kernel, so it runs only when named.
    for name in sys.argv[1:] or [n for n in ROWS if n != "dead"]:
        print(f"== {name}")
        ROWS[name]()
