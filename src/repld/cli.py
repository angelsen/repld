import argparse
import sys


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] == "bridge":
        from .bridge import run_bridge

        raise SystemExit(run_bridge(argv[1:]))
    if argv and argv[0] == "init":
        from .scaffold import run_init

        raise SystemExit(run_init(argv[1:]))
    if argv and argv[0] == "exec":
        from .exec_cmd import run_exec

        raise SystemExit(run_exec(argv[1:]))
    if argv and argv[0] == "help":
        from .help import run_help

        raise SystemExit(run_help(argv[1:]))
    if argv and argv[0] == "gist":
        from .scaffold import run_gist

        raise SystemExit(run_gist(argv[1:]))

    parser = argparse.ArgumentParser(
        prog="repld",
        description="Persistent Python runtime with MCP channel push. "
        "Run `repld help` for the substrate-level overview, "
        "`repld init` to scaffold a project.",
    )
    parser.add_argument(
        "--socket",
        default=None,
        help="Unix socket path (default: ./.pyrepl.sock)",
    )
    parser.add_argument(
        "--no-display",
        action="store_true",
        help="Skip the display thread (headless/CI mode; kernel still runs IPC).",
    )
    parser.add_argument(
        "--init",
        default=None,
        metavar="FILE",
        help="Python file to execute into __main__ before accepting connections.",
    )
    args = parser.parse_args(argv)

    from .kernel import run_kernel

    raise SystemExit(
        run_kernel(
            socket_path=args.socket,
            display=not args.no_display,
            init_file=args.init,
        )
    )
