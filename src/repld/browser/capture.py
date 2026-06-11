"""Fetch body capture handler.

Enables Fetch domain interception on attach for request POST body capture.
Response bodies are read on demand via Network.getResponseBody — intercepting
at response stage consumed the body before the page could read it.
"""

import asyncio
import logging

from .cdp import CDPSession, _json_dumps_safe

__all__ = ["enable", "handle_paused"]

logger = logging.getLogger(__name__)


async def enable(session: CDPSession) -> None:
    """Enable Fetch interception on a CDPSession.

    Only intercepts at request stage (for POST body capture).  Response bodies
    are read on demand via Network.getResponseBody — intercepting at response
    stage consumed the body before the page could read it, breaking sites that
    use brotli/gzip encoding (e.g. TikTok).
    """
    await session.execute(
        "Fetch.enable",
        {
            "patterns": [
                {"urlPattern": "*", "requestStage": "Request"},
            ]
        },
    )
    session._fetch_handler = handle_paused
    logger.debug("Fetch capture enabled on %s", session.chrome_target_id)


async def handle_paused(session: CDPSession, params: dict) -> None:
    """Handle a Fetch.requestPaused event.

    Only request-stage events arrive (response-stage interception is disabled).
    Captures POST body, then continues the request.
    Always calls continueRequest — never leaves a hung request.
    """
    request_id = params.get("requestId", "")
    network_id = params.get("networkId", request_id)

    is_response = params.get("responseStatusCode") is not None

    try:
        if is_response or not session.capture_bodies:
            await _fast_continue(session, request_id, is_response)
            return

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


async def _handle_request(
    session: CDPSession, request_id: str, network_id: str, params: dict
) -> None:
    """Request stage: capture POST body, then continue."""
    post_data = (params.get("request") or {}).get("postData")

    if post_data is None:
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
