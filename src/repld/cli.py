import argparse
import sys


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] == "bridge":
        from .bridge import run_bridge

        raise SystemExit(run_bridge(argv[1:]))

    parser = argparse.ArgumentParser(
        prog="repld",
        description="Persistent Python runtime with MCP channel push.",
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
