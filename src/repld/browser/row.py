"""Row dataclass and factory functions for network/console query results."""

import json
from dataclasses import dataclass
from typing import Any

from .cdp import CDPSession

__all__ = ["Row", "Rows"]


@dataclass
class Row:
    """A row from a HAR or console query."""

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

    # Back-reference for .body()
    _session: CDPSession | None = None

    def body(self) -> dict:
        """Fetch the response body for this request."""
        if self._session is None:
            return {"error": "no session"}
        return self._session.fetch_body(self.request_id)

    def __repr__(self) -> str:
        if self.method and self.url:
            size_str = f"{self.size / 1024:.1f}KB" if self.size else "0B"
            time_str = f"{self.time_ms}ms" if self.time_ms is not None else "?"
            rid = f" rid={self.request_id}" if self.request_id else ""
            return f"<Request {self.method} {self.url} -> {self.status} ({time_str}, {size_str}){rid}>"
        if self.level:
            text = self.text if len(self.text) <= 200 else self.text[:200] + "…"
            loc = ""
            if self.stack_url:
                loc = f" @ {self.stack_url}"
                if self.stack_line:
                    loc += f":{self.stack_line}"
            return f"<Console {self.level}: {text}{loc}>"
        if self.data is not None:
            data = self.data if len(self.data) <= 200 else self.data[:200] + "…"
            name = f" {self.event_name}" if self.event_name else ""
            return f"<SSE{name}: {data}>"
        return f"<Row id={self.id}>"


class Rows(list):
    """List subclass with one-entry-per-line repr for grep-friendly spill files."""

    def __repr__(self) -> str:
        if not self:
            return "[]"
        return "\n".join(repr(r) for r in self)


def _row_from_har(cols: tuple, session: CDPSession) -> Row:
    """Build a Row from a har_summary query result tuple."""
    # har_summary columns: id, request_id, redirect_index, protocol, method, status,
    #   url, type, size, time_ms, state, pause_stage, paused_id, frames_sent,
    #   frames_received, started_datetime, last_activity, target, body_status,
    #   mime_family, is_asset, initiator_type, initiator_url
    return Row(
        id=cols[0] or 0,
        request_id=cols[1] or "",
        redirect_index=cols[2] or 0,
        protocol=cols[3] or "",
        method=cols[4] or "",
        status=cols[5] or 0,
        url=cols[6] or "",
        type=cols[7] or "",
        size=cols[8] or 0,
        time_ms=cols[9],
        state=cols[10] or "",
        pause_stage=cols[11],
        paused_id=cols[12],
        frames_sent=cols[13],
        frames_received=cols[14],
        started_datetime=cols[15],
        last_activity=cols[16],
        target=cols[17] or "",
        body_status=cols[18],
        mime_family=cols[19] or "",
        is_asset=bool(cols[20]),
        initiator_type=cols[21],
        initiator_url=cols[22],
        _session=session,
    )


def _row_from_console(cols: tuple, session: CDPSession) -> Row:
    """Build a Row from a console_entries query result tuple."""
    # console_entries columns: id, level, source, text, stack_url, stack_line,
    #   stack_function, timestamp, target
    return Row(
        id=cols[0] or 0,
        level=cols[1] or "",
        source=cols[2] or "",
        text=cols[3] or "",
        stack_url=cols[4],
        stack_line=cols[5],
        stack_function=cols[6],
        timestamp=cols[7],
        target=cols[8] or "",
        _session=session,
    )


def _row_from_sse(cols: tuple, session: CDPSession) -> Row:
    """Build a Row from an sse_entries query result tuple."""
    # sse_entries columns: id, request_id, event_name, event_id, data,
    #   timestamp, target
    return Row(
        id=cols[0] or 0,
        request_id=cols[1] or "",
        event_name=cols[2] or "",
        event_id=cols[3] or "",
        data=cols[4] if cols[4] is not None else "",
        timestamp=cols[5],
        target=cols[6] or "",
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


def _dict_from_har_entry(cols: tuple) -> dict:
    """Build a structured dict from a har_entries query result tuple.

    har_entries columns (0-indexed):
      0  id, 1  request_id, 2  redirect_index, 3  protocol, 4  method,
      5  url, 6  status, 7  status_text, 8  type, 9  size, 10 time_ms,
      11 state, 12 pause_stage, 13 paused_id, 14 request_headers,
      15 post_data, 16 response_headers, 17 mime_type, 18 timing,
      19 error_text, 20 request_cookies, 21 frames_sent, 22 frames_received,
      23 ws_total_bytes, 24 started_datetime, 25 last_activity, 26 target,
      27 body_status, 28 initiator_type, 29 initiator_url,
      30 initiator_function, 31 initiator_line, 32 loader_id, 33 frame_id,
      34 auth_scheme, 35 auth_cookies, 36 csrf_token_header, 37 mime_family,
      38 is_asset, 39 curl_command
    """
    d: dict[str, Any] = {
        "request": {
            "method": cols[4] or "",
            "url": cols[5] or "",
        },
        "response": {
            "status": cols[6] or 0,
        },
        "state": cols[11] or "",
        "type": cols[8] or "",
        "size": cols[9] or 0,
        "time_ms": cols[10],
    }

    # Request details
    req_headers = _parse_json(cols[14])
    if req_headers:
        d["request"]["headers"] = req_headers
    if cols[15]:
        d["request"]["postData"] = cols[15]

    # Response details
    if cols[7]:
        d["response"]["statusText"] = cols[7]
    resp_headers = _parse_json(cols[16])
    if resp_headers:
        d["response"]["headers"] = resp_headers
    if cols[17]:
        d["response"]["mimeType"] = cols[17]

    # Timing
    timing = _parse_json(cols[18])
    if timing:
        d["timing"] = timing

    # Error
    if cols[19]:
        d["error_text"] = cols[19]

    # Auth
    if cols[34]:
        d["auth_scheme"] = cols[34]
    if cols[36]:
        d["csrf_token_header"] = cols[36]

    # Initiator
    init_type = cols[28]
    if init_type:
        initiator: dict[str, Any] = {"type": init_type}
        if cols[29]:
            initiator["url"] = cols[29]
        if cols[30]:
            initiator["function"] = cols[30]
        if cols[31]:
            initiator["line"] = cols[31]
        d["initiator"] = initiator

    return d
