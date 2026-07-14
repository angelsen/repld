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

    def _filtered_query(
        self, source: str, conditions: list[str], bind_params: list[Any], tail: str
    ) -> list[dict]:
        """SELECT * FROM `source` with an optional WHERE built from conditions."""
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        sql = f"SELECT * FROM {source} {where} {tail}"
        return self._session.query_dicts(sql, bind_params if bind_params else None)

    @staticmethod
    def _like_pattern(url: str) -> str:
        """Convert a `*`-glob URL filter to a SQL LIKE pattern."""
        pattern = url.replace("*", "%")
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
            conditions.append("url LIKE ?")
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
            conditions.append("CAST(timestamp AS DOUBLE) >= ?")
            bind_params.append(since)

        rows = self._filtered_query(
            "console_entries", conditions, bind_params, "LIMIT 200"
        )
        return Rows(_row_from_console(r, self._session) for r in rows)

    def control_observations(self) -> list[dict]:
        """Parsed __controls__ observations from console.debug messages."""
        rows = self._session.query(
            f"SELECT text FROM console_entries WHERE level = 'debug' AND text LIKE '{_CONTROLS_PREFIX}%' ORDER BY id DESC LIMIT 100"
        )
        results = []
        for row in rows:
            raw = row[0]
            if not isinstance(raw, str):
                continue
            prefix = f"{_CONTROLS_PREFIX} "
            if raw.startswith(prefix):
                raw = raw[len(prefix) :]
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
                "request_id IN (SELECT request_id FROM har_summary WHERE url LIKE ?)"
            )
            bind_params.append(self._like_pattern(url))
        if event_name:
            conditions.append("event_name = ?")
            bind_params.append(event_name)
        if since is not None:
            conditions.append("CAST(timestamp AS DOUBLE) >= ?")
            bind_params.append(since)

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
            conditions.append("CAST(timestamp AS DOUBLE) >= ?")
            bind_params.append(since)

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
