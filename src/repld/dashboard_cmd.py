"""`repld dashboard` — open the running kernel's control panel.

`GET /` needs no token (the token is embedded in the served page and only
`POST /api` checks it), so this is purely a port lookup plus a browser open.
"""

import sys
import webbrowser

from . import paths, state

_USAGE = """\
repld dashboard — open this project's kernel dashboard

  repld dashboard [--print] [--socket PATH]

  --print   print the URL instead of opening a browser
"""


def run_dashboard(argv: list[str]) -> int:
    if any(a in ("-h", "--help") for a in argv):
        print(_USAGE)
        return 0

    lock_path, rest = paths.resolve_lock_path(argv)
    print_only = "--print" in rest
    unknown = [a for a in rest if a != "--print"]
    if unknown:
        print(f"repld dashboard: unknown argument {unknown[0]!r}\n")
        print(_USAGE)
        return 2

    lock = state.read_lock(lock_path)
    if isinstance(lock, str):
        print(f"repld dashboard: {lock}", file=sys.stderr)
        return 1
    port = lock.get("dashboard_port")
    if not port:
        print(
            "repld dashboard: kernel is running but its dashboard didn't start "
            "(see the kernel's stderr)",
            file=sys.stderr,
        )
        return 1

    # Literal 127.0.0.1, never localhost/[::1]: the dashboard's Host allowlist
    # is what stops DNS rebinding, and an IPv6 loopback Host would be 403'd.
    url = f"http://127.0.0.1:{port}/"
    if print_only or not webbrowser.open(url):
        print(url)
        return 0
    print(f"opened {url}")
    return 0
