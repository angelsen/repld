import argparse
import sys
from importlib import import_module

# name → (module, func, one-line help). Single source for both dispatch and
# the --help listing, so they can't drift. Handlers are lazy-imported on match,
# keeping `repld bridge` (spawned every session) a dict lookup + one import.
_SUBCOMMANDS = {
    "bridge": ("bridge", "run_bridge", "stdio MCP bridge (Claude Code spawns this)"),
    "exec": ("exec_cmd", "run_exec", "one-shot code or interactive REPL"),
    "log": ("log_cmd", "run_log", "recent kernel activity (-f to follow)"),
    "status": ("lifecycle_cmd", "run_status", "kernel pid/uptime + live siblings"),
    "stop": (
        "lifecycle_cmd",
        "run_stop",
        "stop this project's kernel (--all for every one)",
    ),
    "restart": (
        "lifecycle_cmd",
        "run_restart",
        "stop, then start a fresh headless kernel",
    ),
    "dashboard": (
        "dashboard_cmd",
        "run_dashboard",
        "open the kernel's web control panel",
    ),
    "gate": ("gate_cmd", "run_gate", "list / answer pending human gates"),
    "help": ("help", "run_help", "agent/human docs"),
    "gist": ("gist_cmd", "run_gist", "new / fetch / add / rm / list / lint gists"),
    "browser": (
        "relaunch",
        "run_browser",
        "re-exec via `uv run` with duckdb/websockets/pillow",
    ),
}


# Subcommands that end up running a kernel process, and so need to be under
# the project's interpreter. A bare `repld` (no subcommand, or kernel flags)
# counts too and is handled separately in main().
BINDING_COMMANDS = frozenset({"bridge", "restart"})


def _subcommands_text() -> str:
    lines = ["subcommands:"]
    width = max(len(n) for n in _SUBCOMMANDS)
    for name, (_, _, desc) in _SUBCOMMANDS.items():
        lines.append(f"  {name:<{width}} {desc}")
    return "\n".join(lines)


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] in ("--version", "-V"):
        from importlib.metadata import version

        print(f"repld-tool {version('repld-tool')}")
        return

    # Commands that *become* a kernel, or spawn one, may re-exec under the
    # project's interpreter here and never return. Everything else is left
    # alone: routing it through `uv run` costs ~250ms per invocation and buys
    # nothing. Notably absent is `exec`, which only resolves a lock path and
    # talks IPC — the code runs in the kernel, which is already bound.
    if not argv or argv[0].startswith("-") or argv[0] in BINDING_COMMANDS:
        from . import bind

        bind.rebind_exec(argv)

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
        "Run `repld help` for the substrate-level overview. Register with "
        "`claude mcp add repld -- repld bridge`. A `repld_init.py` in the cwd "
        "is executed into __main__ at boot, for every kernel in this project.",
        epilog=_subcommands_text(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--socket",
        default=None,
        help="Unix socket path (default: $XDG_RUNTIME_DIR/repld/projects/<slug>/kernel.sock)",
    )
    parser.add_argument(
        "--no-display",
        action="store_true",
        help="Skip the display thread (headless/CI mode; kernel still runs IPC).",
    )
    args = parser.parse_args(argv)

    from .kernel import run_kernel

    raise SystemExit(run_kernel(socket_path=args.socket, display=not args.no_display))
