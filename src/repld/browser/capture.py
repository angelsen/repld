"""Fetch body capture handler.

Enables Fetch domain interception on attach. Captures:
- Request POST bodies (all requests)
- Response bodies (JSON API responses only, < 500KB)

Large responses, assets, and streams pass through untouched via
continueResponse — only small JSON bodies are read and replayed
via fulfillRequest.
"""

import asyncio
import base64
import logging
import time

from .cdp import CDPSession, _json_dumps_safe

__all__ = ["enable", "handle_paused"]

logger = logging.getLogger(__name__)

_MAX_BODY_SIZE = 500_000


async def enable(session: CDPSession) -> None:
    """Enable Fetch interception on a CDPSession."""
    await session.execute(
        "Fetch.enable",
        {
            "patterns": [
                {"urlPattern": "*", "requestStage": "Request"},
                # Response-stage: only API paths, not assets/scripts/images.
                # Captures JSON bodies proactively; everything else uses
                # Network.getResponseBody on demand.
                {"urlPattern": "*/api/*", "requestStage": "Response"},
                {"urlPattern": "*/graphql*", "requestStage": "Response"},
            ]
        },
    )
    session._fetch_handler = handle_paused
    logger.debug("Fetch capture enabled on %s", session.chrome_target_id)


async def handle_paused(session: CDPSession, params: dict) -> None:
    """Handle a Fetch.requestPaused event.

    Request stage: capture POST body, continue.
    Response stage: capture body for small JSON responses, pass through everything else.
    """
    request_id = params.get("requestId", "")
    network_id = params.get("networkId", request_id)

    is_response = params.get("responseStatusCode") is not None

    try:
        if not session.capture_bodies:
            await _fast_continue(session, request_id, is_response)
            return

        if is_response:
            await _handle_response(session, request_id, network_id, params)
        else:
            await _handle_request(session, request_id, network_id, params)
    except Exception as exc:
        logger.debug("handle_paused error for %s: %s", request_id, exc)
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


def _should_capture_body(params: dict) -> bool:
    """Decide whether to capture a response body based on headers.

    Captures JSON responses under 500KB.  Skips redirects, SSE, assets,
    and anything too large — those pass through untouched.
    """
    status = params.get("responseStatusCode", 0)
    if status in (301, 302, 303, 307, 308):
        return False

    response_headers = params.get("responseHeaders", [])
    content_type = ""
    content_length = -1
    for h in response_headers:
        name = h.get("name", "").lower()
        if name == "content-type":
            content_type = h.get("value", "").lower()
        elif name == "content-length":
            try:
                content_length = int(h.get("value", -1))
            except (ValueError, TypeError):
                pass

    if "text/event-stream" in content_type:
        return False

    if "json" not in content_type:
        return False

    if 0 < content_length > _MAX_BODY_SIZE:
        return False

    return True


async def _handle_response(
    session: CDPSession, request_id: str, network_id: str, params: dict
) -> None:
    """Response stage: capture body for JSON API responses, pass through the rest."""
    if not _should_capture_body(params):
        await session.execute("Fetch.continueResponse", {"requestId": request_id})
        return

    status_code = params.get("responseStatusCode", 200)
    response_headers = params.get("responseHeaders", [])

    t_start = time.monotonic()
    body = ""
    b64 = False
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

    # Replay body to the page via fulfillRequest.  getResponseBody consumes
    # the internal buffer — continueResponse after it delivers empty.
    if body:
        try:
            await session.execute(
                "Fetch.fulfillRequest",
                {
                    "requestId": request_id,
                    "responseCode": status_code,
                    "responseHeaders": response_headers,
                    "body": body if b64 else base64.b64encode(body.encode()).decode(),
                },
            )
            return
        except Exception as exc:
            logger.debug("fulfillRequest %s: %s", request_id, exc)

    # Fallback: body empty or fulfillRequest failed — let original through
    try:
        await session.execute("Fetch.continueResponse", {"requestId": request_id})
    except Exception as exc:
        logger.debug("continueResponse fallback %s: %s", request_id, exc)


async def _handle_request(
    session: CDPSession, request_id: str, network_id: str, params: dict
) -> None:
    """Request stage: capture POST body, then continue."""
    request = params.get("request") or {}
    post_data = request.get("postData")

    if post_data is None and request.get("method", "GET").upper() in (
        "POST",
        "PUT",
        "PATCH",
    ):
        try:
            result = await asyncio.wait_for(
                session.execute("Fetch.getRequestPostData", {"requestId": request_id}),
                timeout=5,
            )
            post_data = result.get("postData")
        except Exception:
            pass

    if post_data:
        _store_request_body(session, network_id, post_data)

    await session.execute("Fetch.continueRequest", {"requestId": request_id})


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
