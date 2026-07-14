"""Row dataclass and factory functions for network/console query results."""

import json
from dataclasses import dataclass
from typing import Any

from .cdp import CDPSession

__all__ = ["Row", "Rows"]


def size_str(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes}B"
    return f"{size_bytes / 1024:.1f}KB"


@dataclass
class Row:
    """A row from a HAR or console query."""

    # Discriminator — one of "network", "console", "sse", "lifecycle", set by
    # the corresponding _row_from_* factory.
    kind: str = ""

    # HAR fields (network rows)
    id: int = 0
    request_id: str = ""
    redirect_index: int = 0
    protocol: str = ""
    method: str = ""
    status: int = 0
    url: str = ""
    type: str = ""
    size: int = 0
    time_ms: int | None = None
    state: str = ""
    pause_stage: str | None = None
    paused_id: int | None = None
    frames_sent: int | None = None
    frames_received: int | None = None
    started_datetime: str | None = None
    last_activity: float | None = None
    target: str = ""
    body_status: str | None = None
    mime_family: str = ""
    is_asset: bool = False
    initiator_type: str | None = None
    initiator_url: str | None = None

    # Console fields
    level: str = ""
    source: str = ""
    text: str = ""
    stack_url: str | None = None
    stack_line: str | None = None
    stack_function: str | None = None
    timestamp: str | None = None

    # SSE fields (None = not an SSE row)
    event_name: str | None = None
    event_id: str | None = None
    data: str | None = None

    # Lifecycle fields (None = not a lifecycle row)
    frame_id: str | None = None
    loader_id: str | None = None
    name: str | None = None

    # Back-reference for .body()
    _session: CDPSession | None = None

    def body(self) -> dict:
        """Fetch the response body for this request."""
        if self._session is None:
            return {"error": "no session"}
        return self._session.fetch_body(self.request_id)

    def __repr__(self) -> str:
        if self.kind == "network":
            size_fmt = size_str(self.size) if self.size else "0B"
            time_str = f"{self.time_ms}ms" if self.time_ms is not None else "?"
            rid = f" rid={self.request_id}" if self.request_id else ""
            return f"<Request {self.method} {self.url} -> {self.status} ({time_str}, {size_fmt}){rid}>"
        if self.kind == "console":
            text = self.text if len(self.text) <= 200 else self.text[:200] + "…"
            loc = ""
            if self.stack_url:
                loc = f" @ {self.stack_url}"
                if self.stack_line:
                    loc += f":{self.stack_line}"
            return f"<Console {self.level}: {text}{loc}>"
        if self.kind == "sse":
            data = self.data or ""
            if len(data) > 200:
                data = data[:200] + "…"
            name = f" {self.event_name}" if self.event_name else ""
            return f"<SSE{name}: {data}>"
        if self.kind == "lifecycle":
            return f"<Lifecycle {self.name}>"
        return f"<Row id={self.id}>"


class Rows(list):
    """List subclass with one-entry-per-line repr for grep-friendly spill files."""

    def __repr__(self) -> str:
        if not self:
            return "[]"
        return "\n".join(repr(r) for r in self)


def _row_from_har(c: dict, session: CDPSession) -> Row:
    """Build a Row from a har_summary query result row."""
    return Row(
        kind="network",
        id=c["id"] or 0,
        request_id=c["request_id"] or "",
        redirect_index=c["redirect_index"] or 0,
        protocol=c["protocol"] or "",
        method=c["method"] or "",
        status=c["status"] or 0,
        url=c["url"] or "",
        type=c["type"] or "",
        size=c["size"] or 0,
        time_ms=c["time_ms"],
        state=c["state"] or "",
        pause_stage=c["pause_stage"],
        paused_id=c["paused_id"],
        frames_sent=c["frames_sent"],
        frames_received=c["frames_received"],
        started_datetime=c["started_datetime"],
        last_activity=c["last_activity"],
        target=c["target"] or "",
        body_status=c["body_status"],
        mime_family=c["mime_family"] or "",
        is_asset=bool(c["is_asset"]),
        initiator_type=c["initiator_type"],
        initiator_url=c["initiator_url"],
        _session=session,
    )


def _row_from_console(c: dict, session: CDPSession) -> Row:
    """Build a Row from a console_entries query result row."""
    return Row(
        kind="console",
        id=c["id"] or 0,
        level=c["level"] or "",
        source=c["source"] or "",
        text=c["text"] or "",
        stack_url=c["stack_url"],
        stack_line=c["stack_line"],
        stack_function=c["stack_function"],
        timestamp=c["timestamp"],
        target=c["target"] or "",
        _session=session,
    )


def _row_from_sse(c: dict, session: CDPSession) -> Row:
    """Build a Row from an sse_entries query result row."""
    return Row(
        kind="sse",
        id=c["id"] or 0,
        request_id=c["request_id"] or "",
        event_name=c["event_name"] or "",
        event_id=c["event_id"] or "",
        data=c["data"] if c["data"] is not None else "",
        timestamp=c["timestamp"],
        target=c["target"] or "",
        _session=session,
    )


def _row_from_lifecycle(c: dict, session: CDPSession) -> Row:
    """Build a Row from a lifecycle_entries query result row."""
    return Row(
        kind="lifecycle",
        id=c["id"] or 0,
        frame_id=c["frame_id"] or "",
        loader_id=c["loader_id"] or "",
        name=c["name"] or "",
        timestamp=c["timestamp"],
        target=c["target"] or "",
        _session=session,
    )


def _parse_json(val: Any) -> Any:
    """Parse a JSON string into a dict/list, or return None."""
    if val is None:
        return None
    if isinstance(val, (dict, list)):
        return val
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError):
        return None


def _dict_from_har_entry(c: dict) -> dict:
    """Build a structured dict from a har_entries query result row."""
    d: dict[str, Any] = {
        "request": {
            "method": c["method"] or "",
            "url": c["url"] or "",
        },
        "response": {
            "status": c["status"] or 0,
        },
        "state": c["state"] or "",
        "type": c["type"] or "",
        "size": c["size"] or 0,
        "time_ms": c["time_ms"],
    }

    # Request details
    req_headers = _parse_json(c["request_headers"])
    if req_headers:
        d["request"]["headers"] = req_headers
    if c["post_data"]:
        d["request"]["postData"] = c["post_data"]

    # Response details
    if c["status_text"]:
        d["response"]["statusText"] = c["status_text"]
    resp_headers = _parse_json(c["response_headers"])
    if resp_headers:
        d["response"]["headers"] = resp_headers
    if c["mime_type"]:
        d["response"]["mimeType"] = c["mime_type"]

    # Timing
    timing = _parse_json(c["timing"])
    if timing:
        d["timing"] = timing

    # Error
    if c["error_text"]:
        d["error_text"] = c["error_text"]

    # Auth
    if c["auth_scheme"]:
        d["auth_scheme"] = c["auth_scheme"]
    if c["csrf_token_header"]:
        d["csrf_token_header"] = c["csrf_token_header"]

    # Initiator
    init_type = c["initiator_type"]
    if init_type:
        initiator: dict[str, Any] = {"type": init_type}
        if c["initiator_url"]:
            initiator["url"] = c["initiator_url"]
        if c["initiator_function"]:
            initiator["function"] = c["initiator_function"]
        if c["initiator_line"]:
            initiator["line"] = c["initiator_line"]
        d["initiator"] = initiator

    return d
