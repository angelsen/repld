"""Phase 14: Dashboard HTTP — Host-header allowlist and page authentication."""

import http.client
import json

from harness import Kernel, assert_eq, assert_true

from repld import paths


def _request(
    port: int,
    path: str = "/",
    *,
    host_header: str | None = None,
    cookie: str | None = None,
    auth: str | None = None,
    method: str = "GET",
    body: bytes | None = None,
) -> tuple[int, str, dict[str, str]]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.putrequest(method, path, skip_host=True)
        conn.putheader("Host", host_header or f"127.0.0.1:{port}")
        if cookie:
            conn.putheader("Cookie", cookie)
        if auth:
            conn.putheader("Authorization", auth)
        if body is not None:
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders()
        if body is not None:
            conn.send(body)
        resp = conn.getresponse()
        return (
            resp.status,
            resp.read().decode("utf-8", "replace"),
            {k.lower(): v for k, v in resp.getheaders()},
        )
    finally:
        conn.close()


def phase_14_dashboard(kernel: Kernel) -> None:
    """Host allowlist, and the token that gates the page rather than riding in it."""
    lock = json.loads(kernel.lock_path.read_text())
    port = lock.get("dashboard_port")
    assert_true(bool(port), f"lockfile has dashboard_port (got {lock!r})")

    hint_path = paths.hint_for(paths.socket_path(kernel.cwd))
    token = json.loads(hint_path.read_text()).get("token")
    assert_true(bool(token), "hint file carries the API token")

    status, body, _ = _request(port, f"/?token={token}")
    assert_eq(status, 200, "GET / with the token")
    print(f"  ✓ dashboard on :{port} serves GET / for loopback Host + token")

    status, _, _ = _request(
        port, f"/?token={token}", host_header=f"evil.example:{port}"
    )
    assert_eq(status, 403, "GET / with rebound Host")
    print("  ✓ forged Host → 403 (DNS-rebinding guard)")

    # The page embeds the token so its own JS can call POST /api, which means
    # serving it unauthenticated handed the credential to any process on the
    # box — loopback is not a uid boundary, and every file holding this data
    # is written 0600.
    status, body, _ = _request(port)
    assert_eq(status, 401, "GET / without a token")
    assert_true(token not in body, "and the 401 body carries no token")
    status, _, _ = _request(port, "/?token=deadbeef")
    assert_eq(status, 401, "GET / with the wrong token")
    print("  ✓ unauthenticated GET / is refused and leaks no token")

    # The cookie is what makes a refresh work after the page drops ?token=
    # from the address bar.
    status, body, headers = _request(port, f"/?token={token}")
    assert_true(
        f"repld_token_{port}={token}" in headers.get("set-cookie", ""),
        f"authenticated page sets a per-port cookie (got {headers.get('set-cookie')!r})",
    )
    assert_true("HttpOnly" in headers.get("set-cookie", ""), "cookie is HttpOnly")
    assert_eq(headers.get("cache-control"), "no-store", "tokenised page is not cached")
    status, body, _ = _request(port, cookie=f"repld_token_{port}={token}")
    assert_eq(status, 200, "GET / with the cookie alone")
    assert_true(token in body, "and the page still gets its token inlined")
    status, _, _ = _request(port, cookie=f"repld_token_{port}=deadbeef")
    assert_eq(status, 401, "GET / with a wrong cookie")
    print("  ✓ per-port cookie carries a refresh; a wrong one doesn't")

    # Bearer-only for the API: a cookie rides along on requests the user did
    # not initiate, so it must never be sufficient on its own.
    call = json.dumps({"method": "state", "id": 1}).encode()
    status, _, _ = _request(
        port, "/api", method="POST", body=call, cookie=f"repld_token_{port}={token}"
    )
    assert_eq(status, 401, "POST /api refuses a cookie")
    status, api_body, _ = _request(
        port, "/api", method="POST", body=call, auth=f"Bearer {token}"
    )
    assert_eq(status, 200, "POST /api accepts the Bearer token")
    print("  ✓ POST /api stays Bearer-only — a cookie does not authenticate it")

    # The sidebar links to sibling dashboards, which now refuse an
    # unauthenticated GET / too, so each entry has to carry its own token.
    status, api_body, _ = _request(
        port,
        "/api",
        method="POST",
        body=json.dumps({"method": "sessions", "id": 2}).encode(),
        auth=f"Bearer {token}",
    )
    rows = json.loads(api_body)["result"]
    assert_true(
        all("dashboard_token" in r for r in rows),
        f"every session entry carries a dashboard_token (got {rows!r})",
    )
    assert_true(
        any(r.get("dashboard_token") == token for r in rows),
        "including this kernel's own",
    )
    print("  ✓ sessions RPC carries per-session tokens for the sidebar links")
