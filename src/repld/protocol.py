"""MCP JSON-RPC tool schemas + dispatch.

Shared by the kernel; the bridge is a dumb byte-pipe and never touches this.
"""

import json
import threading
from typing import Protocol

from .help import INSTRUCTIONS as _INSTRUCTIONS

PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "exec",
        "description": (
            "Execute Python in the running kernel. Returns inline if it "
            "finishes within `timeout` seconds; otherwise returns "
            "{task_id, done:false} and the completion arrives as a channel "
            "notification."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "timeout": {"type": "number", "default": 2.0},
            },
            "required": ["code"],
        },
    },
    {
        "name": "get_task",
        "description": (
            "Fetch current status and a head+tail preview of a task's output. "
            "Use Read on the returned `spill_path` for full content."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "cancel",
        "description": (
            "Attempt to cancel a running task. Returns whether cancellation "
            "was accepted. Cannot preempt tight sync loops (`while True: pass`) "
            "— only await-yielding code is cancellable."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
]


class KernelContext(Protocol):
    def start_task(self, src: str) -> tuple[str, threading.Event]: ...
    def snapshot(self, task_id: str) -> dict: ...
    def mark_nudged(self, task_id: str) -> None: ...
    def cancel_task(self, task_id: str) -> bool: ...


class Dispatcher:
    def __init__(
        self,
        ctx: KernelContext,
        *,
        server_name: str = "repld",
        server_version: str = "0.0.1",
    ):
        self.ctx = ctx
        self.server_name = server_name
        self.server_version = server_version

    def handle(self, req: dict, session) -> dict | None:
        method = req.get("method")
        rid = req.get("id")
        if method == "initialize":
            return self._initialize(rid)
        if method == "notifications/initialized":
            session.set_initialized()
            return None
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
        if method == "tools/call":
            return self._tools_call(rid, req.get("params", {}))
        if rid is None:
            return None
        return _error(rid, -32601, f"method not found: {method}")

    def _initialize(self, rid) -> dict:
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {},
                    "experimental": {
                        "claude/channel": {},
                        "claude/channel/permission": {},
                    },
                },
                "serverInfo": {
                    "name": self.server_name,
                    "version": self.server_version,
                },
                "instructions": _INSTRUCTIONS,
            },
        }

    def _tools_call(self, rid, params: dict) -> dict:
        name = params.get("name")
        args = params.get("arguments", {}) or {}
        if name == "exec":
            return self._exec(rid, args)
        if name == "get_task":
            return self._get_task(rid, args)
        if name == "cancel":
            return self._cancel(rid, args)
        return _error(rid, -32602, f"unknown tool: {name}")

    def _exec(self, rid, args: dict) -> dict:
        src = args.get("code", "")
        timeout = float(args.get("timeout", 2.0))
        task_id, done_event = self.ctx.start_task(src)
        finished = done_event.wait(timeout=timeout)
        snap = self.ctx.snapshot(task_id)
        if finished:
            parts = []
            if snap["text"]:
                parts.append(snap["text"].rstrip())
            if snap["truncated"]:
                parts.append(f"[full output: {snap['spill_path']}]")
            text = "\n".join(parts) or "(no output)"
            return {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {
                    "content": [{"type": "text", "text": text}],
                    "isError": bool(snap["exception"]),
                    "_meta": {
                        "task_id": task_id,
                        "done": True,
                        "spilled": snap["spilled"],
                        "spill_path": snap["spill_path"],
                    },
                },
            }
        self.ctx.mark_nudged(task_id)
        preview = snap["text"].rstrip()
        msg = f"[task {task_id} still running after {timeout}s; completion will arrive via channel]"
        if preview:
            msg += "\n" + preview
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "content": [{"type": "text", "text": msg}],
                "_meta": {"task_id": task_id, "done": False},
            },
        }

    def _get_task(self, rid, args: dict) -> dict:
        snap = self.ctx.snapshot(args["task_id"])
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "content": [{"type": "text", "text": json.dumps(snap, indent=2)}],
                "_meta": snap,
            },
        }

    def _cancel(self, rid, args: dict) -> dict:
        tid = args["task_id"]
        accepted = self.ctx.cancel_task(tid)
        status = "accepted" if accepted else "no-op"
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "content": [{"type": "text", "text": f"cancel task={tid}: {status}"}],
                "_meta": {"task_id": tid, "cancelled": accepted},
            },
        }


def _error(rid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}
