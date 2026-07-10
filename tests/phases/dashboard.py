"""Phase 14: Dashboard HTTP — Host-header allowlist rejects DNS rebinding."""

import http.client
import json

from harness import Kernel, assert_eq, assert_true


def _get_status(port: int, host_header: str) -> int:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.putrequest("GET", "/", skip_host=True)
        conn.putheader("Host", host_header)
        conn.endheaders()
        return conn.getresponse().status
    finally:
        conn.close()


def phase_14_dashboard(kernel: Kernel) -> None:
    """GET / serves the panel for a loopback Host and 403s a rebound hostname."""
    lock = json.loads((kernel.cwd / ".pyrepl.lock").read_text())
    port = lock.get("dashboard_port")
    assert_true(bool(port), f"lockfile has dashboard_port (got {lock!r})")

    status = _get_status(port, f"127.0.0.1:{port}")
    assert_eq(status, 200, "GET / with loopback Host")
    print(f"  ✓ dashboard on :{port} serves GET / for loopback Host")

    status = _get_status(port, f"evil.example:{port}")
    assert_eq(status, 403, "GET / with rebound Host")
    print("  ✓ forged Host → 403 (DNS-rebinding guard)")
