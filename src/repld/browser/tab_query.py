"""Tab's DuckDB-backed query surface.

Split out of tab.py: network/console/sse/lifecycle history, response
bodies, and full HAR entries all read from `self._session`'s DuckDB store
and share no state with the JS/DOM half of Tab.
"""

import json
from typing import Any

from .cdp import _CONTROLS_PREFIX, CDPSession
from .row import (
    Row,
    Rows,
    _dict_from_har_entry,
    _row_from_console,
    _row_from_har,
    _row_from_lifecycle,
    _row_from_sse,
)


class TabQueryMixin:
    """DuckDB-backed query methods, mixed into browser.tab.Tab.

    Assumes `self._session` (CDPSession) from Tab.__init__.
    """

    _session: CDPSession

    # ------------------------------------------------------------------
    # `since=` — one time base across four methods
    # ------------------------------------------------------------------
    #
    # **`since=` is epoch seconds everywhere: pass `time.time()`.** It has to be
    # converted per view, because the CDP events behind them carry three
    # different clocks and the views store each one raw:
    #
    #   network()   har_summary.last_activity  — epoch SECONDS, derived from
    #                                            `requestWillBeSent.wallTime`
    #   console()   Runtime.Timestamp          — epoch MILLISECONDS
    #   sse()       Network.MonotonicTime      — seconds since an arbitrary
    #   lifecycle()                              origin, not an epoch at all
    #
    # Comparing the caller's number against the raw column meant only
    # `network()` did what it looked like. `tab.console(since=time.time())`
    # compared ~1.7e9 against ~1.7e12 and matched every row — the opposite of
    # what was asked, silently — and `sse()`/`lifecycle()` compared it against
    # a number that counts from browser start, so it matched nothing.
    # `docs/browser.md` carried this as an open question ("timestamp, row_id,
    # or sentinel?"); this is the answer, chosen because `time.time()` is what
    # a caller reaches for and the only base a caller can produce.

    def _monotonic_offset(self) -> float | None:
        """`wallTime - timestamp` for this tab, or None if nothing pins it.

        Both numbers are on `Network.requestWillBeSent`, which is how
        `har_summary.last_activity` converts monotonic durations to wall clock
        already. Read fresh rather than cached: the monotonic origin is the
        browser process's, so a cached offset outlives the thing it describes
        across a Chrome restart, and this only runs when a caller actually
        passes `since=`.
        """
        rows = self._session.query(
            """
            SELECT
                CAST(json_extract_string(event, '$.params.wallTime') AS DOUBLE),
                CAST(json_extract_string(event, '$.params.timestamp') AS DOUBLE)
            FROM events
            WHERE method = 'Network.requestWillBeSent'
              AND json_extract(event, '$.params.wallTime') IS NOT NULL
            ORDER BY rowid DESC LIMIT 1
            """
        )
        if not rows or rows[0][0] is None or rows[0][1] is None:
            return None
        return float(rows[0][0]) - float(rows[0][1])

    def _since_monotonic(self, since: float) -> float:
        """Epoch-seconds *since* on this tab's monotonic clock.

        With no request recorded there is nothing to anchor the conversion, so
        this returns a bound nothing can satisfy — under-returning rather than
        handing back the whole table, which is the direction the bug went. It
        takes a store with zero network events to get there, i.e. a tab that
        has only just attached.
        """
        offset = self._monotonic_offset()
        return float("inf") if offset is None else since - offset

    def _filtered_query(
        self, source: str, conditions: list[str], bind_params: list[Any], tail: str
    ) -> list[dict]:
        """SELECT * FROM `source` with an optional WHERE built from conditions."""
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        sql = f"SELECT * FROM {source} {where} {tail}"
        return self._session.query_dicts(sql, bind_params if bind_params else None)

    # `*` is the only wildcard this filter offers, so LIKE's own metacharacters
    # have to be escaped out of the literal text — `_` matches any single
    # character, and it is common enough in URLs (`/api/user_profile`) that
    # leaving it live made the filter quietly over-match. Paired with
    # `_LIKE_ESCAPE` on every comparison built from this; a pattern with
    # backslashes in it means nothing without the ESCAPE clause.
    _LIKE_ESCAPE = " ESCAPE '\\'"

    @staticmethod
    def _like_pattern(url: str) -> str:
        """Convert a `*`-glob URL filter to a SQL LIKE pattern.

        Escaping runs before the `*` translation, or the `%` it produces would
        be escaped along with the user's literal ones.
        """
        escaped = url.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = escaped.replace("*", "%")
        if not pattern.startswith("%"):
            pattern = "%" + pattern
        if not pattern.endswith("%"):
            pattern = pattern + "%"
        return pattern

    def network(
        self,
        *,
        url: str | None = None,
        method: str | None = None,
        status: int | None = None,
        type: str | None = None,
        since: float | None = None,
        include_assets: bool = False,
    ) -> list[Row]:
        """Query the HAR summary view with optional filters."""
        conditions: list[str] = []
        bind_params: list[Any] = []

        if url:
            conditions.append("url LIKE ?" + self._LIKE_ESCAPE)
            bind_params.append(self._like_pattern(url))
        if method:
            conditions.append("method = ?")
            bind_params.append(method.upper())
        if status is not None:
            conditions.append("status = ?")
            bind_params.append(status)
        if type:
            conditions.append("type = ?")
            bind_params.append(type)
        if since is not None:
            # `last_activity` is already epoch seconds (derived from wallTime),
            # so this is the one view `since` needs no conversion for.
            conditions.append("CAST(last_activity AS DOUBLE) >= ?")
            bind_params.append(since)
        if not include_assets:
            conditions.append("is_asset = false")

        rows = self._filtered_query(
            "har_summary", conditions, bind_params, "ORDER BY id DESC LIMIT 500"
        )
        return Rows(_row_from_har(r, self._session) for r in rows)

    def console(
        self,
        *,
        level: str | None = None,
        source: str | None = None,
        since: float | None = None,
    ) -> list[Row]:
        """Query the console_entries view with optional filters."""
        conditions: list[str] = []
        bind_params: list[Any] = []

        if level:
            conditions.append("level = ?")
            bind_params.append(level)
        if source:
            conditions.append("source = ?")
            bind_params.append(source)
        if since is not None:
            # Runtime.Timestamp is epoch *milliseconds*; `since` is seconds.
            conditions.append("CAST(timestamp AS DOUBLE) >= ?")
            bind_params.append(since * 1000.0)

        rows = self._filtered_query(
            "console_entries", conditions, bind_params, "ORDER BY id DESC LIMIT 200"
        )
        return Rows(_row_from_console(r, self._session) for r in rows)

    def control_observations(self) -> list[dict]:
        """Parsed __controls__ observations from console.debug messages.

        Reads the base `events` table rather than `console_entries`: an
        observation is logged as `console.debug("__controls__", json)`, and
        the view's `text` column is only ever the *first* console argument,
        so the payload isn't in it at all. Same two-argument shape the live
        push path parses (`cdp._check_controls_observation`) — note the JSON
        path indices are 0-based here, unlike the view's 1-based DuckDB list
        subscripts.
        """
        rows = self._session.query(
            """
            SELECT json_extract_string(event, '$.params.args[1].value')
            FROM events
            WHERE method = 'Runtime.consoleAPICalled'
              AND json_extract_string(event, '$.params.type') = 'debug'
              AND json_extract_string(event, '$.params.args[0].value') = ?
            ORDER BY rowid DESC LIMIT 100
            """,
            [_CONTROLS_PREFIX],
        )
        results = []
        for row in rows:
            raw = row[0]
            if not isinstance(raw, str):
                continue
            try:
                results.append(json.loads(raw))
            except (json.JSONDecodeError, TypeError):
                continue
        return results

    def sse(
        self,
        *,
        url: str | None = None,
        event_name: str | None = None,
        since: float | None = None,
    ) -> list[Row]:
        """Query SSE (EventSource) messages received on this tab."""
        conditions: list[str] = []
        bind_params: list[Any] = []

        if url:
            conditions.append(
                "request_id IN (SELECT request_id FROM har_summary"
                " WHERE url LIKE ?" + self._LIKE_ESCAPE + ")"
            )
            bind_params.append(self._like_pattern(url))
        if event_name:
            conditions.append("event_name = ?")
            bind_params.append(event_name)
        if since is not None:
            # Network.MonotonicTime — not an epoch. See `_since_monotonic`.
            conditions.append("CAST(timestamp AS DOUBLE) >= ?")
            bind_params.append(self._since_monotonic(since))

        rows = self._filtered_query(
            "sse_entries", conditions, bind_params, "ORDER BY id DESC LIMIT 500"
        )
        return Rows(_row_from_sse(r, self._session) for r in rows)

    def lifecycle(
        self,
        *,
        name: str | None = None,
        since: float | None = None,
    ) -> list[Row]:
        """Query Page.lifecycleEvent events for this tab.

        Event names (from Chromium source):
          init, DOMContentLoaded, load, firstPaint, firstContentfulPaint,
          firstImagePaint, firstMeaningfulPaintCandidate, firstMeaningfulPaint,
          networkAlmostIdle, networkIdle, InteractiveTime, commit (catch-up only)
        """
        conditions: list[str] = []
        bind_params: list[Any] = []

        if name:
            conditions.append("name = ?")
            bind_params.append(name)
        if since is not None:
            # Network.MonotonicTime, as for sse(). See `_since_monotonic`.
            conditions.append("CAST(timestamp AS DOUBLE) >= ?")
            bind_params.append(self._since_monotonic(since))

        rows = self._filtered_query(
            "lifecycle_entries",
            conditions,
            bind_params,
            "ORDER BY id DESC LIMIT 500",
        )
        return Rows(_row_from_lifecycle(r, self._session) for r in rows)

    def clear(self) -> None:
        """Clear all captured events (network + console + SSE + lifecycle) for this tab."""
        self._session.clear_events()

    def body(self, request_id: str | int) -> dict:
        """Fetch the response body for a request_id."""
        return self._session.fetch_body(str(request_id))

    def request(self, request_id: str | int) -> dict:
        """Return the full HAR entry for a request_id as a structured dict.

        Returns request/response headers, postData, auth scheme, timing —
        everything except the response body (use .body() for that).
        """
        rows = self._session.query_dicts(
            "SELECT * FROM har_entries WHERE request_id = ? ORDER BY id DESC LIMIT 1",
            [str(request_id)],
        )
        if not rows:
            raise RuntimeError(f"No request found for id: {request_id}")
        return _dict_from_har_entry(rows[0])
