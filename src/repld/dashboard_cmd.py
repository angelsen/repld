"""`repld dashboard` — open the running kernel's control panel.

The served page embeds the API token, so `GET /` is authenticated too and
this command is what supplies the credential: it reads the token from the
kernel's 0600 hint file and opens `/?token=…`. The page drops the query from
the address bar once it loads, having been given a cookie for that port.

Which makes this the only convenient way in, deliberately — the token is what
separates "the user who owns this kernel" from "any process on the box", and
loopback is not a uid boundary.
"""

import json
import sys
import webbrowser
from urllib.parse import quote

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

    sock_path, rest = paths.resolve_socket_path(argv)
    lock_path = paths.lock_for(sock_path)
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

    try:
        token = json.loads(paths.hint_for(sock_path).read_text()).get("token") or ""
    except (OSError, json.JSONDecodeError, ValueError):
        token = ""
    if not token:
        print(
            "repld dashboard: no API token in the kernel's hint file — the "
            "dashboard will refuse to serve its page. Restart the kernel.",
            file=sys.stderr,
        )
        return 1

    # Literal 127.0.0.1, never localhost/[::1]: the dashboard's Host allowlist
    # is what stops DNS rebinding, and an IPv6 loopback Host would be 403'd.
    url = f"http://127.0.0.1:{port}/?token={quote(token)}"
    if print_only or not webbrowser.open(url):
        print(url)
        return 0
    print(f"opened {url}")
    return 0
