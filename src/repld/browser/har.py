"""HAR view + console view SQL definitions for DuckDB.

Uses json_transform for typed extraction — each CTE parses the JSON event
once and exposes fields via struct dot notation.  Small CTEs (1–2 fields)
keep plain json_extract_string for brevity.

Ported from webtap's har.py with:
- Redirect fix: separate rows per redirect hop instead of MAX() GROUP BY
- is_final flag on request_hops: JOIN-level filtering replaces per-column
  CASE WHEN window-function guards in http_entries
- Added derived columns: initiator_*, curl_command, auth_scheme, auth_cookies,
  csrf_token_header, mime_family, is_asset, loader_id, frame_id
- console_entries view for Runtime.consoleAPICalled + Runtime.exceptionThrown + Log.entryAdded
"""

import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# HAR entries view
# ---------------------------------------------------------------------------

_HAR_ENTRIES_SQL = """
CREATE OR REPLACE VIEW har_entries AS
WITH

-- Paused Fetch events (unresolved)
paused_fetch AS (
    SELECT
        p.networkId as network_id,
        rowid as paused_id,
        p.responseStatusCode as fetch_status,
        p.responseHeaders as fetch_response_headers,
        CASE WHEN p.responseStatusCode IS NOT NULL THEN 'Response' ELSE 'Request' END as pause_stage,
        request_id as fetch_request_id
    FROM (
        SELECT rowid, request_id,
            (json_transform(event, '{"params": {
                "networkId": "VARCHAR",
                "responseStatusCode": "VARCHAR",
                "responseHeaders": "JSON"
            }}')).params as p
        FROM events
        WHERE method = 'Fetch.requestPaused'
    ) t
),

-- Resolved Fetch events
resolved_fetch AS (
    SELECT DISTINCT request_id as network_id
    FROM events
    WHERE method IN ('Network.loadingFinished', 'Network.loadingFailed')
),

-- Only unresolved paused events (latest per networkId)
active_paused AS (
    SELECT pf.*
    FROM paused_fetch pf
    WHERE pf.network_id IS NOT NULL
      AND pf.network_id NOT IN (SELECT network_id FROM resolved_fetch WHERE network_id IS NOT NULL)
    QUALIFY ROW_NUMBER() OVER (PARTITION BY pf.network_id ORDER BY pf.paused_id DESC) = 1
),

-- HTTP Request: one row per requestWillBeSent (includes redirects — each redirect emits a new event)
all_requests AS (
    SELECT
        rowid,
        request_id,
        p.wallTime as started_datetime,
        p.timestamp as started_timestamp,
        p.request.method as method,
        p.request.url as url,
        p.request.headers as request_headers,
        p.request.postData as post_data,
        p.type as resource_type,
        p.redirectResponse.status as redirect_from_status,
        p.initiator.type as initiator_type,
        p.initiator.url as initiator_url,
        p.initiator.stack.callFrames[1].functionName as initiator_function,
        CAST(p.initiator.stack.callFrames[1].lineNumber AS VARCHAR) as initiator_line,
        p.loaderId as loader_id,
        p.frameId as frame_id,
        target
    FROM (
        SELECT rowid, request_id, target,
            (json_transform(event, '{"params": {
                "wallTime": "VARCHAR",
                "timestamp": "VARCHAR",
                "request": {"method": "VARCHAR", "url": "VARCHAR", "headers": "JSON", "postData": "VARCHAR"},
                "type": "VARCHAR",
                "redirectResponse": {"status": "VARCHAR"},
                "initiator": {
                    "type": "VARCHAR",
                    "url": "VARCHAR",
                    "stack": {"callFrames": [{"functionName": "VARCHAR", "lineNumber": "BIGINT"}]}
                },
                "loaderId": "VARCHAR",
                "frameId": "VARCHAR"
            }}')).params as p
        FROM events
        WHERE method = 'Network.requestWillBeSent'
    ) t
),

-- Redirect index + is_final flag
-- is_final=true on the last hop in a redirect chain (or the only hop if no redirects)
request_hops AS (
    SELECT
        *,
        ROW_NUMBER() OVER (PARTITION BY request_id ORDER BY rowid) - 1 as redirect_index,
        ROW_NUMBER() OVER (PARTITION BY request_id ORDER BY rowid DESC) = 1 as is_final
    FROM all_requests
),

-- For each hop, the response is carried in the NEXT hop's redirectResponse
-- OR in the final Network.responseReceived
redirect_responses AS (
    SELECT
        rh.rowid as request_rowid,
        rh.request_id,
        rh.redirect_index,
        LEAD(rh.redirect_from_status) OVER (PARTITION BY rh.request_id ORDER BY rh.rowid) as redirect_status
    FROM request_hops rh
),

-- HTTP Response: extract from responseReceived (only the FINAL response, not redirect hops)
http_responses AS (
    SELECT
        request_id,
        MAX(p.response.status) as status,
        MAX(p.response.statusText) as status_text,
        MAX(p.response.headers) as response_headers,
        MAX(p.response.mimeType) as mime_type,
        MAX(p.response.timing) as timing
    FROM (
        SELECT request_id,
            (json_transform(event, '{"params": {"response": {
                "status": "VARCHAR",
                "statusText": "VARCHAR",
                "headers": "JSON",
                "mimeType": "VARCHAR",
                "timing": "JSON"
            }}}')).params as p
        FROM events
        WHERE method = 'Network.responseReceived'
    ) t
    GROUP BY request_id
),

-- HTTP Finished: timing and size
http_finished AS (
    SELECT
        request_id,
        MAX(json_extract_string(event, '$.params.timestamp')) as finished_timestamp,
        MAX(json_extract_string(event, '$.params.encodedDataLength')) as final_size
    FROM events
    WHERE method = 'Network.loadingFinished'
    GROUP BY request_id
),

-- HTTP Failed: error info
http_failed AS (
    SELECT
        request_id,
        MAX(json_extract_string(event, '$.params.errorText')) as error_text
    FROM events
    WHERE method = 'Network.loadingFailed'
    GROUP BY request_id
),

-- Request ExtraInfo: raw headers with cookies
request_extra AS (
    SELECT
        request_id,
        MAX(json_extract(event, '$.params.headers')) as raw_headers,
        MAX(json_extract(event, '$.params.associatedCookies')) as cookies
    FROM events
    WHERE method = 'Network.requestWillBeSentExtraInfo'
    GROUP BY request_id
),

-- Response ExtraInfo: Set-Cookie headers and true status
response_extra AS (
    SELECT
        request_id,
        MAX(json_extract(event, '$.params.headers')) as raw_headers,
        MAX(json_extract_string(event, '$.params.statusCode')) as true_status
    FROM events
    WHERE method = 'Network.responseReceivedExtraInfo'
    GROUP BY request_id
),

-- Captured request POST bodies (from Fetch request stage)
captured_request_bodies AS (
    SELECT
        request_id,
        MAX(json_extract_string(event, '$.params.body')) as body
    FROM events
    WHERE method = 'Network.requestBodyCaptured'
    GROUP BY request_id
),

-- Captured response bodies with status (ok/err), deduped to latest per request
captured_bodies AS (
    SELECT request_id, body_status
    FROM (
        SELECT
            request_id,
            CASE
                WHEN json_extract_string(event, '$.params.capture.ok') = 'true' THEN 'ok'
                ELSE 'err'
            END as body_status,
            ROW_NUMBER() OVER (PARTITION BY request_id ORDER BY rowid DESC) as rn
        FROM events
        WHERE method = 'Network.responseBodyCaptured'
    )
    WHERE rn = 1
),

-- WebSocket Created
ws_created AS (
    SELECT
        request_id,
        MIN(rowid) as first_rowid,
        'websocket' as protocol,
        MAX(json_extract_string(event, '$.params.url')) as url,
        MAX(target) as target
    FROM events
    WHERE method = 'Network.webSocketCreated'
    GROUP BY request_id
),

-- WebSocket Handshake
ws_handshake AS (
    SELECT
        request_id,
        MAX(p.wallTime) as started_datetime,
        MAX(p.timestamp) as started_timestamp,
        MAX(p.request.headers) as request_headers,
        MAX(p.response.status) as status,
        MAX(p.response.headers) as response_headers
    FROM (
        SELECT request_id,
            (json_transform(event, '{"params": {
                "wallTime": "VARCHAR",
                "timestamp": "VARCHAR",
                "request": {"headers": "JSON"},
                "response": {"status": "VARCHAR", "headers": "JSON"}
            }}')).params as p
        FROM events
        WHERE method IN ('Network.webSocketWillSendHandshakeRequest', 'Network.webSocketHandshakeResponseReceived')
    ) t
    GROUP BY request_id
),

-- WebSocket Frame Stats (aggregated)
ws_frames AS (
    SELECT
        request_id,
        SUM(CASE WHEN method = 'Network.webSocketFrameSent' THEN 1 ELSE 0 END) as frames_sent,
        SUM(CASE WHEN method = 'Network.webSocketFrameReceived' THEN 1 ELSE 0 END) as frames_received,
        SUM(LENGTH(COALESCE(json_extract_string(event, '$.params.response.payloadData'), ''))) as total_bytes,
        MAX(json_extract_string(event, '$.params.timestamp')) as last_frame_timestamp
    FROM events
    WHERE method IN ('Network.webSocketFrameSent', 'Network.webSocketFrameReceived')
    GROUP BY request_id
),

-- WebSocket Closed
ws_closed AS (
    SELECT
        request_id,
        MAX(json_extract_string(event, '$.params.timestamp')) as closed_timestamp
    FROM events
    WHERE method = 'Network.webSocketClosed'
    GROUP BY request_id
),

-- HTTP entries: one row per request_id × redirect_index hop
-- JOINs gated on rh.is_final restrict response/finish/error data to the last hop
http_entries AS (
    SELECT
        rh.rowid as id,
        rh.request_id,
        rh.redirect_index,
        'http' as protocol,
        rh.method,
        rh.url,
        -- Status: redirect hops use next-hop redirectResponse; final uses responseReceived
        CAST(COALESCE(rr.redirect_status, ap.fetch_status, respx.true_status, resp.status, '0') AS INTEGER) as status,
        resp.status_text,
        rh.resource_type as type,
        CAST(COALESCE(fin.final_size, '0') AS INTEGER) as size,
        CASE WHEN fin.finished_timestamp IS NOT NULL
            THEN CAST((CAST(fin.finished_timestamp AS DOUBLE) - CAST(rh.started_timestamp AS DOUBLE)) * 1000 AS INTEGER)
        END as time_ms,
        CASE
            WHEN NOT rh.is_final THEN 'redirect'
            WHEN fail.error_text IS NOT NULL THEN 'failed'
            WHEN fin.finished_timestamp IS NOT NULL THEN 'complete'
            WHEN resp.status IS NOT NULL THEN 'loading'
            WHEN ap.paused_id IS NOT NULL THEN 'paused'
            ELSE 'pending'
        END as state,
        ap.pause_stage,
        ap.paused_id,
        -- Prefer raw headers from ExtraInfo (includes Cookie header)
        COALESCE(reqx.raw_headers, rh.request_headers) as request_headers,
        COALESCE(crb.body, rh.post_data) as post_data,
        COALESCE(respx.raw_headers, ap.fetch_response_headers, resp.response_headers) as response_headers,
        resp.mime_type,
        resp.timing,
        fail.error_text,
        reqx.cookies as request_cookies,
        CAST(NULL AS BIGINT) as frames_sent,
        CAST(NULL AS BIGINT) as frames_received,
        CAST(NULL AS BIGINT) as ws_total_bytes,
        rh.started_datetime,
        CASE WHEN fin.finished_timestamp IS NOT NULL AND rh.started_datetime IS NOT NULL AND rh.started_timestamp IS NOT NULL
            THEN CAST(rh.started_datetime AS DOUBLE) + (CAST(fin.finished_timestamp AS DOUBLE) - CAST(rh.started_timestamp AS DOUBLE))
            ELSE CAST(rh.started_datetime AS DOUBLE)
        END as last_activity,
        rh.target,
        cb.body_status,
        -- Initiator fields
        rh.initiator_type,
        rh.initiator_url,
        rh.initiator_function,
        rh.initiator_line,
        rh.loader_id,
        rh.frame_id,
        -- Derived: auth_scheme from Authorization header
        CASE
            WHEN LOWER(json_extract_string(COALESCE(reqx.raw_headers, rh.request_headers), '$.Authorization')) LIKE 'bearer %' THEN 'bearer'
            WHEN LOWER(json_extract_string(COALESCE(reqx.raw_headers, rh.request_headers), '$.Authorization')) LIKE 'basic %' THEN 'basic'
            WHEN json_extract_string(COALESCE(reqx.raw_headers, rh.request_headers), '$.Authorization') IS NOT NULL THEN 'other'
            ELSE NULL
        END as auth_scheme,
        -- Derived: cookie names from Cookie header
        json_extract_string(COALESCE(reqx.raw_headers, rh.request_headers), '$.Cookie') as auth_cookies,
        -- Derived: CSRF token header name
        CASE
            WHEN json_extract_string(COALESCE(reqx.raw_headers, rh.request_headers), '$.X-CSRF-Token') IS NOT NULL THEN 'X-CSRF-Token'
            WHEN json_extract_string(COALESCE(reqx.raw_headers, rh.request_headers), '$.X-XSRF-TOKEN') IS NOT NULL THEN 'X-XSRF-TOKEN'
            WHEN json_extract_string(COALESCE(reqx.raw_headers, rh.request_headers), '$.X-CSRFToken') IS NOT NULL THEN 'X-CSRFToken'
            WHEN json_extract_string(COALESCE(reqx.raw_headers, rh.request_headers), '$.authenticity_token') IS NOT NULL THEN 'authenticity_token'
            ELSE NULL
        END as csrf_token_header,
        -- Derived: mime_family bucket
        CASE
            WHEN resp.mime_type LIKE '%json%' THEN 'json'
            WHEN resp.mime_type LIKE '%html%' THEN 'html'
            WHEN resp.mime_type LIKE '%javascript%' OR resp.mime_type LIKE '%ecmascript%' THEN 'js'
            WHEN resp.mime_type LIKE '%css%' THEN 'css'
            WHEN resp.mime_type LIKE '%image/%' THEN 'image'
            WHEN resp.mime_type LIKE '%font/%' OR resp.mime_type LIKE '%woff%' THEN 'font'
            WHEN resp.mime_type LIKE '%video/%' OR resp.mime_type LIKE '%audio/%' THEN 'media'
            ELSE 'other'
        END as mime_family,
        -- Derived: is_asset (images, fonts, CSS, media)
        CASE
            WHEN rh.resource_type IN ('Image', 'Font', 'Stylesheet', 'Media') THEN true
            WHEN resp.mime_type LIKE '%image/%' OR resp.mime_type LIKE '%font/%'
              OR resp.mime_type LIKE '%css%' OR resp.mime_type LIKE '%woff%' THEN true
            ELSE false
        END as is_asset,
        -- Derived: curl_command reconstruction
        CONCAT(
            'curl -X ', COALESCE(rh.method, 'GET'), ' ', chr(39), rh.url, chr(39),
            CASE WHEN crb.body IS NOT NULL
                 THEN CONCAT(' --data-raw ', chr(39), REPLACE(crb.body, chr(39), chr(39) || '\' || chr(39) || chr(39)), chr(39))
                 ELSE '' END
        ) as curl_command
    FROM request_hops rh
    LEFT JOIN redirect_responses rr ON rh.rowid = rr.request_rowid
    LEFT JOIN http_responses resp ON rh.request_id = resp.request_id AND rh.is_final
    LEFT JOIN response_extra respx ON rh.request_id = respx.request_id AND rh.is_final
    LEFT JOIN http_finished fin ON rh.request_id = fin.request_id AND rh.is_final
    LEFT JOIN http_failed fail ON rh.request_id = fail.request_id AND rh.is_final
    LEFT JOIN active_paused ap ON rh.request_id = ap.network_id AND rh.is_final
    LEFT JOIN request_extra reqx ON rh.request_id = reqx.request_id AND rh.is_final
    LEFT JOIN captured_request_bodies crb ON rh.request_id = crb.request_id
    LEFT JOIN captured_bodies cb ON rh.request_id = cb.request_id AND rh.is_final
),

-- WebSocket entries
websocket_entries AS (
    SELECT
        ws.first_rowid as id,
        ws.request_id,
        0 as redirect_index,
        ws.protocol,
        'WS' as method,
        ws.url,
        CAST(COALESCE(hs.status, '101') AS INTEGER) as status,
        CAST(NULL AS VARCHAR) as status_text,
        'WebSocket' as type,
        CAST(COALESCE(wf.total_bytes, 0) AS INTEGER) as size,
        CASE
            WHEN wc.closed_timestamp IS NOT NULL
            THEN CAST((CAST(wc.closed_timestamp AS DOUBLE) - CAST(hs.started_timestamp AS DOUBLE)) * 1000 AS INTEGER)
            ELSE NULL
        END as time_ms,
        CASE
            WHEN wc.closed_timestamp IS NOT NULL THEN 'closed'
            WHEN hs.status IS NOT NULL THEN 'open'
            ELSE 'connecting'
        END as state,
        CAST(NULL AS VARCHAR) as pause_stage,
        CAST(NULL AS BIGINT) as paused_id,
        hs.request_headers,
        CAST(NULL AS VARCHAR) as post_data,
        hs.response_headers,
        'websocket' as mime_type,
        CAST(NULL AS JSON) as timing,
        CAST(NULL AS VARCHAR) as error_text,
        CAST(NULL AS JSON) as request_cookies,
        wf.frames_sent,
        wf.frames_received,
        wf.total_bytes as ws_total_bytes,
        hs.started_datetime,
        CASE
            WHEN wf.last_frame_timestamp IS NOT NULL AND hs.started_datetime IS NOT NULL AND hs.started_timestamp IS NOT NULL
            THEN CAST(hs.started_datetime AS DOUBLE) + (CAST(wf.last_frame_timestamp AS DOUBLE) - CAST(hs.started_timestamp AS DOUBLE))
            ELSE CAST(hs.started_datetime AS DOUBLE)
        END as last_activity,
        ws.target,
        CAST(NULL AS VARCHAR) as body_status,
        CAST(NULL AS VARCHAR) as initiator_type,
        CAST(NULL AS VARCHAR) as initiator_url,
        CAST(NULL AS VARCHAR) as initiator_function,
        CAST(NULL AS VARCHAR) as initiator_line,
        CAST(NULL AS VARCHAR) as loader_id,
        CAST(NULL AS VARCHAR) as frame_id,
        CAST(NULL AS VARCHAR) as auth_scheme,
        CAST(NULL AS VARCHAR) as auth_cookies,
        CAST(NULL AS VARCHAR) as csrf_token_header,
        'other' as mime_family,
        false as is_asset,
        CONCAT('# WebSocket: ', ws.url) as curl_command
    FROM ws_created ws
    LEFT JOIN ws_handshake hs ON ws.request_id = hs.request_id
    LEFT JOIN ws_frames wf ON ws.request_id = wf.request_id
    LEFT JOIN ws_closed wc ON ws.request_id = wc.request_id
)

SELECT * FROM http_entries
UNION ALL
SELECT * FROM websocket_entries
ORDER BY id DESC
"""

# ---------------------------------------------------------------------------
# HAR summary view — lightweight list for network() command
# ---------------------------------------------------------------------------

_HAR_SUMMARY_SQL = """
CREATE OR REPLACE VIEW har_summary AS
SELECT
    id,
    request_id,
    redirect_index,
    protocol,
    method,
    status,
    url,
    type,
    size,
    time_ms,
    state,
    pause_stage,
    paused_id,
    frames_sent,
    frames_received,
    started_datetime,
    last_activity,
    target,
    body_status,
    mime_family,
    is_asset,
    initiator_type,
    initiator_url
FROM har_entries
"""

# ---------------------------------------------------------------------------
# Console entries view
# ---------------------------------------------------------------------------

_CONSOLE_ENTRIES_SQL = """
CREATE OR REPLACE VIEW console_entries AS
SELECT
    rowid as id,
    COALESCE(
        p.type,
        p.entry.level,
        CASE WHEN method = 'Runtime.exceptionThrown' THEN 'error' END,
        'log'
    ) as level,
    COALESCE(
        p.source,
        p.entry.source,
        CASE WHEN method = 'Runtime.exceptionThrown' THEN 'javascript' END,
        'console-api'
    ) as source,
    COALESCE(
        p.args[1].value,
        p.entry.text,
        p.exceptionDetails.exception.description,
        p.exceptionDetails.text,
        ''
    ) as text,
    COALESCE(
        p.stackTrace.callFrames[1].url,
        p.entry.url,
        p.exceptionDetails.url
    ) as stack_url,
    COALESCE(
        CAST(p.stackTrace.callFrames[1].lineNumber AS VARCHAR),
        CAST(p.entry.lineNumber AS VARCHAR),
        CAST(p.exceptionDetails.lineNumber AS VARCHAR)
    ) as stack_line,
    COALESCE(
        p.stackTrace.callFrames[1].functionName,
        p.exceptionDetails.stackTrace.callFrames[1].functionName
    ) as stack_function,
    COALESCE(p.timestamp, p.entry.timestamp) as timestamp,
    target
FROM (
    SELECT rowid, method, target,
        (json_transform(event, '{"params": {
            "type": "VARCHAR",
            "source": "VARCHAR",
            "args": [{"value": "VARCHAR"}],
            "stackTrace": {"callFrames": [{"url": "VARCHAR", "lineNumber": "BIGINT", "functionName": "VARCHAR"}]},
            "timestamp": "VARCHAR",
            "entry": {
                "level": "VARCHAR",
                "source": "VARCHAR",
                "text": "VARCHAR",
                "url": "VARCHAR",
                "lineNumber": "BIGINT",
                "timestamp": "VARCHAR"
            },
            "exceptionDetails": {
                "exception": {"description": "VARCHAR"},
                "text": "VARCHAR",
                "url": "VARCHAR",
                "lineNumber": "BIGINT",
                "stackTrace": {"callFrames": [{"functionName": "VARCHAR"}]}
            }
        }}')).params as p
    FROM events
    WHERE method IN ('Runtime.consoleAPICalled', 'Runtime.exceptionThrown', 'Log.entryAdded')
) t
ORDER BY rowid DESC
"""


_SSE_ENTRIES_SQL = """
CREATE OR REPLACE VIEW sse_entries AS
SELECT
    rowid as id,
    p.requestId as request_id,
    p.eventName as event_name,
    p.eventId as event_id,
    p.data as data,
    p.timestamp as timestamp,
    target
FROM (
    SELECT rowid, target,
        (json_transform(event, '{"params": {
            "requestId": "VARCHAR",
            "eventName": "VARCHAR",
            "eventId": "VARCHAR",
            "data": "VARCHAR",
            "timestamp": "VARCHAR"
        }}')).params as p
    FROM events
    WHERE method = 'Network.eventSourceMessageReceived'
) t
ORDER BY rowid ASC
"""


_LIFECYCLE_ENTRIES_SQL = """
CREATE OR REPLACE VIEW lifecycle_entries AS
SELECT
    rowid as id,
    p.frameId as frame_id,
    p.loaderId as loader_id,
    p.name as name,
    p.timestamp as timestamp,
    target
FROM (
    SELECT rowid, target,
        (json_transform(event, '{"params": {
            "frameId": "VARCHAR",
            "loaderId": "VARCHAR",
            "name": "VARCHAR",
            "timestamp": "VARCHAR"
        }}')).params as p
    FROM events
    WHERE method = 'Page.lifecycleEvent'
) t
ORDER BY rowid ASC
"""


def _create_views(db_execute) -> None:
    """Create all HAR, console, SSE, and lifecycle views. Called from CDPSession init."""
    db_execute(_HAR_ENTRIES_SQL)
    db_execute(_HAR_SUMMARY_SQL)
    db_execute(_CONSOLE_ENTRIES_SQL)
    db_execute(_SSE_ENTRIES_SQL)
    db_execute(_LIFECYCLE_ENTRIES_SQL)
    logger.debug("HAR + console + SSE + lifecycle views created")
