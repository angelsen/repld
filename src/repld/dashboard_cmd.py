"""`repld dashboard` — open the running kernel's control panel.

The served page embeds the API token, so `GET /` is authenticated too and
this command is what supplies the credential: it reads the token from the
kernel's 0600 hint file and opens `/?token=…`. The page drops the query from
the address bar once it loads, having been given a cookie for that port.

Which makes this the only convenient way in, deliberately — the token is what
separates "the user who owns this kernel" from "any process on the box", and
loopback is not a uid boundary.
"""

import sys
import webbrowser
from urllib.parse import quote

from . import cli_args, paths, state

_USAGE = """\
repld dashboard — open this project's kernel dashboard

  repld dashboard [--print] [--socket PATH]

  --print   print the URL instead of opening a browser
"""


def run_dashboard(argv: list[str]) -> int:
    if cli_args.wants_help(argv):
        print(_USAGE)
        return 0

    sock_path, rest = paths.resolve_socket_path(argv)
    lock_path = paths.lock_for(sock_path)
    bad = cli_args.check_args(
        "repld dashboard", rest, _USAGE, flags=("--print",), positionals=0
    )
    if bad is not None:
        return bad
    print_only = "--print" in rest

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

    token = state.dashboard_token(paths.hint_for(sock_path))
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
    # The port, not the URL — the same reason the boot banner and `repld status`
    # print one. The URL carries the API token, and once the browser has it
    # there is nothing left to gain by also leaving it in shell scrollback.
    # `--print` is there for when you do want the credential.
    print(f"opened the dashboard on port {port}")
    return 0
