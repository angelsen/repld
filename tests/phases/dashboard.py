"""Phase 14: Dashboard HTTP — Host-header allowlist and page authentication."""

import http.client
import json
import socket

from harness import Kernel, assert_eq, assert_true

from repld import paths


def _declared_length(port: int, length: int, token: str | None) -> int:
    """Status for a POST /api that *declares* `length` and sends no body.

    Raw socket rather than `_request`, which derives Content-Length from the
    bytes it is given — the whole point here is a header that lies. Returns 0
    when the server accepted the declaration and is waiting for a body that
    never comes, which is what "not rejected" looks like from out here.
    """
    req = f"POST /api HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
    if token is not None:
        req += f"Authorization: Bearer {token}\r\n"
    req += f"Content-Length: {length}\r\n\r\n"
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        s.sendall(req.encode())
        data = s.recv(256)
    except socket.timeout:
        return 0
    finally:
        s.close()
    if not data:
        return 0
    return int(data.split(b" ")[1])


def _flood_headers(port: int, count: int) -> int:
    """Status for a GET / that sends *count* headers and no terminating blank.

    Raw socket, like `_declared_length`: `http.client` terminates the header
    block for you, and never sending that terminator is the whole point.
    Returns 0 if the server just kept reading — which is what the bug looked
    like from out here, for as long as the client cared to keep typing.
    """
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        s.sendall(f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n".encode())
        for i in range(count):
            try:
                s.sendall(b"X-Pad-%d: %s\r\n" % (i, b"a" * 64))
            except OSError:
                break  # server answered and closed part-way through
        data = s.recv(256)
    except (socket.timeout, OSError):
        return 0
    finally:
        s.close()
    if not data:
        return 0
    return int(data.split(b" ")[1])


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

    # Content-Length feeds readexactly() directly, so an unbounded one lets an
    # authenticated caller allocate the kernel to death from a single header —
    # and the 5 s read timeout is no defence at loopback speed. The order
    # matters as much as the cap: an anonymous caller must still see 401, so
    # the limit isn't something to probe for without the token.
    from repld.dashboard import _MAX_BODY_BYTES

    assert_eq(
        _declared_length(port, _MAX_BODY_BYTES + 1, token),
        413,
        "POST /api over the body cap",
    )
    assert_eq(
        _declared_length(port, 10_000_000_000, None),
        401,
        "oversized POST /api without a token is still 401, not 413",
    )
    assert_eq(
        _declared_length(port, _MAX_BODY_BYTES, token),
        0,
        "a body exactly at the cap is accepted (awaits the body, no 413)",
    )
    print(
        f"  ✓ POST /api body capped at {_MAX_BODY_BYTES >> 10}KiB, after the auth check"
    )

    # The header block is the *other* unbounded read, and the one that mattered
    # more: it runs before the Host check and before either auth path, so it was
    # the only thing an unauthenticated caller could make the kernel do. It
    # accepted 200,000 headers on one connection without answering.
    from repld.dashboard import _MAX_HEADER_LINES

    assert_eq(
        _flood_headers(port, _MAX_HEADER_LINES + 50),
        431,
        "a header flood is refused rather than accumulated",
    )
    print(f"  ✓ header block capped at {_MAX_HEADER_LINES} lines, before any auth")

    # One request per connection, so the response has to say so — otherwise an
    # HTTP/1.1 client pools a socket the server has already closed.
    _, _, headers = _request(port, "/", cookie=f"repld_token_{port}={token}")
    assert_eq(headers.get("connection"), "close", "responses declare Connection: close")
    print("  ✓ Connection: close — the server serves one request per connection")

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
