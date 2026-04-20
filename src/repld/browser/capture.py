"""Fetch body capture handler.

Enables Fetch domain interception on attach; stores request POST bodies
and response bodies in DuckDB. Peek-and-continue — no request modification.
"""

import asyncio
import logging
import time

from .cdp import CDPSession, _json_dumps_safe

__all__ = ["enable", "handle_paused"]

logger = logging.getLogger(__name__)


async def enable(session: CDPSession) -> None:
    """Enable Fetch interception on a CDPSession."""
    await session.execute(
        "Fetch.enable",
        {
            "patterns": [
                {"urlPattern": "*", "requestStage": "Request"},
                {"urlPattern": "*", "requestStage": "Response"},
            ]
        },
    )
    # Register our handler so _handle_event can dispatch to it
    session._fetch_handler = handle_paused
    logger.debug("Fetch capture enabled on %s", session.chrome_target_id)


async def handle_paused(session: CDPSession, params: dict) -> None:
    """Handle a Fetch.requestPaused event.

    Detects stage, captures body or POST data, then continues the request.
    Always calls continueRequest/continueResponse — never leaves a hung request.
    """
    request_id = params.get("requestId", "")
    # networkId correlates with the Network domain request_id for HAR view
    network_id = params.get("networkId", request_id)

    is_response = params.get("responseStatusCode") is not None

    try:
        if not session.capture_bodies:
            # Capture disabled — fast continue
            await _fast_continue(session, request_id, is_response)
            return

        if is_response:
            await _handle_response(session, request_id, network_id, params)
        else:
            await _handle_request(session, request_id, network_id, params)
    except Exception as exc:
        logger.debug("handle_paused error for %s: %s", request_id, exc)
        # Ensure we always continue even on unexpected errors
        try:
            await _fast_continue(session, request_id, is_response)
        except Exception:
            pass
    finally:
        session.paused_count = max(0, session.paused_count - 1)


async def _fast_continue(
    session: CDPSession, request_id: str, is_response: bool
) -> None:
    """Continue a paused request without body capture."""
    method = "Fetch.continueResponse" if is_response else "Fetch.continueRequest"
    try:
        await session.execute(method, {"requestId": request_id})
    except Exception as exc:
        logger.debug("fast_continue %s: %s", request_id, exc)


async def _handle_request(
    session: CDPSession, request_id: str, network_id: str, params: dict
) -> None:
    """Request stage: capture POST body, then continue."""
    # POST body may be in params.request.postData directly
    post_data = (params.get("request") or {}).get("postData")

    if post_data is None:
        # Try explicit CDP call
        try:
            result = await asyncio.wait_for(
                session.execute("Fetch.getRequestPostData", {"requestId": request_id}),
                timeout=5,
            )
            post_data = result.get("postData")
        except Exception:
            pass  # Not all requests have a body

    if post_data:
        _store_request_body(session, network_id, post_data)

    await session.execute("Fetch.continueRequest", {"requestId": request_id})


async def _handle_response(
    session: CDPSession, request_id: str, network_id: str, params: dict
) -> None:
    """Response stage: capture body, then continue."""
    status_code = params.get("responseStatusCode", 0)

    # Skip redirects — no body available per CDP spec
    if status_code in (301, 302, 303, 307, 308):
        await session.execute("Fetch.continueResponse", {"requestId": request_id})
        return

    # Skip SSE — getResponseBody would hang on streaming responses
    response_headers = params.get("responseHeaders", [])
    content_type = ""
    for h in response_headers:
        if h.get("name", "").lower() == "content-type":
            content_type = h.get("value", "")
            break
    if "text/event-stream" in content_type:
        await session.execute("Fetch.continueResponse", {"requestId": request_id})
        return

    t_start = time.monotonic()
    try:
        result = await asyncio.wait_for(
            session.execute("Fetch.getResponseBody", {"requestId": request_id}),
            timeout=5,
        )
        elapsed_ms = int((time.monotonic() - t_start) * 1000)
        body = result.get("body", "")
        b64 = result.get("base64Encoded", False)
        _store_response_body(
            session,
            network_id,
            body,
            b64,
            {"ok": True, "elapsed_ms": elapsed_ms},
        )
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - t_start) * 1000)
        _store_response_body(
            session,
            network_id,
            "",
            False,
            {"ok": False, "error": str(exc), "elapsed_ms": elapsed_ms},
        )
    finally:
        try:
            await session.execute("Fetch.continueResponse", {"requestId": request_id})
        except Exception as exc:
            logger.debug("continueResponse %s: %s", request_id, exc)


def _store_request_body(session: CDPSession, network_id: str, body: str) -> None:
    """Store a captured request POST body as a synthetic event."""

    event = {
        "method": "Network.requestBodyCaptured",
        "params": {"requestId": network_id, "body": body},
    }
    try:
        session.db.execute(
            "INSERT INTO events (event, method, request_id, target) VALUES (?, ?, ?, ?)",
            [
                _json_dumps_safe(event),
                "Network.requestBodyCaptured",
                network_id,
                session.target_info.get("targetId", ""),
            ],
        )
    except Exception as exc:
        logger.debug("store_request_body %s: %s", network_id, exc)


def _store_response_body(
    session: CDPSession,
    network_id: str,
    body: str,
    base64_encoded: bool,
    capture_meta: dict,
) -> None:
    """Store a captured response body as a synthetic event."""

    event = {
        "method": "Network.responseBodyCaptured",
        "params": {
            "requestId": network_id,
            "body": body,
            "base64Encoded": base64_encoded,
            "capture": capture_meta,
        },
    }
    try:
        session.db.execute(
            "INSERT INTO events (event, method, request_id, target) VALUES (?, ?, ?, ?)",
            [
                _json_dumps_safe(event),
                "Network.responseBodyCaptured",
                network_id,
                session.target_info.get("targetId", ""),
            ],
        )
    except Exception as exc:
        logger.debug("store_response_body %s: %s", network_id, exc)
