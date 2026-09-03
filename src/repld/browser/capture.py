"""Fetch body capture handler.

Enables Fetch domain interception on attach. Captures:
- Request POST bodies (all requests)
- Response bodies for non-asset responses; stored only if under 500KB

Redirects, SSE streams, and assets pass through untouched. Captured bodies
are replayed to the page via fulfillRequest.

**Capturing a body is not free, so it is not the default for everything.**
Interception is registered on `*` at both stages, unscoped by resource type.
`Fetch.RequestPattern` does support a `resourceType` filter, but it's
inclusive-only — excluding `_ASSET_RESOURCE_TYPES` would take one pattern per
*remaining* type across both stages, not one exclusion — so the filtering
happens here instead, in `_should_capture_body`. The cost isn't only the
skipped types' round trip: every request still pauses at both stages and
crosses the DevTools WebSocket into the Python kernel before Chrome may
proceed, even ones `_should_capture_body` discards outright — that pause
alone is enough added latency to break a timing-sensitive page, with no
network-level symptom at all (ExtJS 6's eval-based class loader threw and
the page never painted). `browser.no_capture()` is the escape hatch until
interception itself is scoped by resource type. Anything that reaches
`Fetch.getResponseBody` has its
whole body pulled over the DevTools WebSocket as base64 and then pushed back
the other way via `fulfillRequest`, because `getResponseBody` consumes the
internal buffer and a plain `continueResponse` after it delivers an empty
response. That is a round trip plus roughly 2.7x the resource's bytes, on every
matching response, added to page-load latency on any tab `get()`/`open()`
touched. Worth it for the JSON/XHR/document traffic the whole capture store
exists to make greppable; not worth it for images, fonts, stylesheets, media
and scripts, which `tab.network()` hides by default and `format_observation`
collapses to a byte count. Skipping them costs nothing that isn't recoverable:
`CDPSession.fetch_body` already falls back to `Network.getResponseBody` for
anything with no stored body, so an asset body is still one call away when
someone actually wants it.
"""

import asyncio
import base64
import logging
import time

from .cdp import _STREAMING_MIME_TYPES, CDPSession

__all__ = ["disable", "enable", "handle_paused"]

logger = logging.getLogger(__name__)

_MAX_BODY_SIZE = 500_000

# What counts as an asset, mirroring har.py's `is_asset` derivation exactly —
# the same five resource types and the same mime markers. That column is this
# codebase's one definition of "network noise" (it drives `tab.network()`'s
# `include_assets=False` default and `format_observation`'s asset rollup), so
# capture and query have to agree about which requests it names. Two lists,
# kept together deliberately: `resourceType` is Chrome's own classification and
# is present on every paused response, while the mime check catches the ones it
# labels `Other`/`XHR` but whose Content-Type gives them away.
_ASSET_RESOURCE_TYPES = frozenset({"Image", "Font", "Stylesheet", "Media", "Script"})
_ASSET_MIME_MARKERS = ("image/", "font/", "css", "woff", "video/", "audio/")


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
    session._fetch_handler = handle_paused
    logger.debug("Fetch capture enabled on %s", session.chrome_target_id)


async def disable(session: CDPSession) -> None:
    """Disable Fetch interception on a CDPSession.

    Propagates, and clears the handler only after Chrome has confirmed —
    the mirror of `enable`, whose failure has always reached the caller.

    Both halves of that matter. This used to swallow the `Fetch.disable`
    exception and clear `_fetch_handler` regardless, which made
    `CDPSession.disable_fetch`'s rollback unreachable and reported a failed
    disable as a success. Worse than the bad report: `_fetch_handler` is the
    only thing that ever resumes a paused request, so clearing it while
    Chrome is still intercepting hangs every subsequent request on the tab
    with `settle` waiting on it. Leaving the handler in place on failure
    keeps those requests flowing, and the raise lets `disable_fetch` restore
    the flags to what Chrome is actually doing.
    """
    await session.execute("Fetch.disable")
    session._fetch_handler = None
    logger.debug("Fetch capture disabled on %s", session.chrome_target_id)


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
        # Unguarded on purpose: _fast_continue swallows its own exceptions, so
        # a try/except here would never fire. Chrome still needs the continue —
        # a paused request nobody resumes hangs the page.
        await _fast_continue(session, request_id, is_response)


async def _fast_continue(
    session: CDPSession, request_id: str, is_response: bool
) -> None:
    """Continue a paused request without body capture.

    Uses send_nowait — no roundtrip wait for Chrome's ack.
    """
    method = "Fetch.continueResponse" if is_response else "Fetch.continueRequest"
    try:
        await session.send_nowait(method, {"requestId": request_id})
    except Exception as exc:
        logger.debug("fast_continue %s: %s", request_id, exc)


def _should_capture_body(params: dict) -> bool:
    """Decide whether to even attempt a response body fetch.

    Skips redirects (CDP puts them in kRedirectReceived state —
    getResponseBody errors), SSE (infinite stream — getResponseBody
    blocks forever), and assets (see the module docstring: the fetch +
    fulfill round trip costs more than the body is worth, and
    `fetch_body` can still get it on demand).

    NOT a size gate: Content-Length is absent on chunked responses and,
    when present, reflects the wire (possibly compressed) size rather
    than the decoded body getResponseBody returns — the actual cap is
    enforced in _handle_response after the body comes back.
    """
    status = params.get("responseStatusCode", 0)
    if status in (301, 302, 303, 307, 308):
        return False

    if params.get("resourceType", "") in _ASSET_RESOURCE_TYPES:
        return False

    response_headers = params.get("responseHeaders", [])
    content_type = ""
    for h in response_headers:
        if h.get("name", "").lower() == "content-type":
            content_type = h.get("value", "").lower()
            break

    # `cdp._STREAMING_MIME_TYPES`, not a literal: "which mime types have a body
    # that never ends" is one fact with two consumers that must agree, the same
    # way the asset lists above mirror har.py's `is_asset`. cdp drops these from
    # `_inflight` so settle stops waiting on them; here we skip the body fetch,
    # because getResponseBody on a stream blocks until it closes. A type added
    # to one list and not the other reintroduces exactly one of those hangs.
    if any(mime in content_type for mime in _STREAMING_MIME_TYPES):
        return False

    return not any(marker in content_type for marker in _ASSET_MIME_MARKERS)


async def _handle_response(
    session: CDPSession, request_id: str, network_id: str, params: dict
) -> None:
    """Response stage: capture body if under size cap, pass through the rest."""
    if not _should_capture_body(params):
        await session.send_nowait("Fetch.continueResponse", {"requestId": request_id})
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
        decoded_size = len(base64.b64decode(body)) if b64 else len(body.encode())
        if decoded_size > _MAX_BODY_SIZE:
            _store_response_body(
                session,
                network_id,
                "",
                False,
                {
                    "ok": False,
                    "error": f"body too large ({decoded_size} bytes > {_MAX_BODY_SIZE})",
                    "elapsed_ms": elapsed_ms,
                },
            )
        else:
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
        await session.send_nowait("Fetch.continueResponse", {"requestId": request_id})
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

    await session.send_nowait("Fetch.continueRequest", {"requestId": request_id})


def _store_request_body(session: CDPSession, network_id: str, body: str) -> None:
    """Store a captured request POST body as a synthetic event."""
    event = {
        "method": "Network.requestBodyCaptured",
        "params": {"requestId": network_id, "body": body},
    }
    try:
        session.store_event(event, "Network.requestBodyCaptured", network_id)
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
        session.store_event(event, "Network.responseBodyCaptured", network_id)
    except Exception as exc:
        logger.debug("store_response_body %s: %s", network_id, exc)
