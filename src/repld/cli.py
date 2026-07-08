import argparse
import sys
from importlib import import_module

# name → (module, func, one-line help). Single source for both dispatch and
# the --help listing, so they can't drift. Handlers are lazy-imported on match,
# keeping `repld bridge` (spawned every session) a dict lookup + one import.
_SUBCOMMANDS = {
    "bridge": ("bridge", "run_bridge", "stdio MCP bridge (Claude Code spawns this)"),
    "init": ("scaffold", "run_init", "scaffold .mcp.json + .gitignore in cwd"),
    "exec": ("exec_cmd", "run_exec", "one-shot code or interactive REPL"),
    "help": ("help", "run_help", "agent/human docs"),
    "gist": ("gist_cmd", "run_gist", "new / add / rm / list gists"),
    "browser": (
        "relaunch",
        "run_browser",
        "re-exec via `uv run` with duckdb/websockets",
    ),
}


def _subcommands_text() -> str:
    lines = ["subcommands:"]
    for name, (_, _, desc) in _SUBCOMMANDS.items():
        lines.append(f"  {name:<9} {desc}")
    return "\n".join(lines)


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] in ("--version", "-V"):
        from importlib.metadata import version

        print(f"repld-tool {version('repld-tool')}")
        return

    sub = _SUBCOMMANDS.get(argv[0]) if argv else None
    if sub:
        mod, func, _ = sub
        handler = getattr(import_module(f".{mod}", __package__), func)
        raise SystemExit(handler(argv[1:]))

    # A bare word that isn't a known subcommand (and isn't a kernel flag) — show
    # the command list rather than letting argparse fall through to the kernel.
    if argv and not argv[0].startswith("-"):
        print(f"repld: unknown command '{argv[0]}'\n", file=sys.stderr)
        print(_subcommands_text(), file=sys.stderr)
        raise SystemExit(2)

    parser = argparse.ArgumentParser(
        prog="repld",
        description="Persistent Python runtime with MCP channel push. "
        "Run `repld help` for the substrate-level overview, "
        "`repld init` to scaffold a project.",
        epilog=_subcommands_text(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
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
