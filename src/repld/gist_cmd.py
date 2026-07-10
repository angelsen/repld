"""The `repld gist` command group — scaffold, link, unlink, list.

`new` scaffolds a local gist file; `add`/`rm`/`list` manage cross-project links
(registry resolution + the ./gists/.links manifest, see `gists.py`). Dispatched
from `cli.py`'s _SUBCOMMANDS table.
"""

from pathlib import Path

_GIST_TEMPLATE = '''\
"""{name} — TODO: one-line description."""

# __repld_deps__ = ["httpx>=0.27"]  # uncomment to auto-install at boot

# --- core logic (portable — keeps on graduation) ---


async def example(id: int) -> dict:
    """TODO: what this does. -> {{id, ...}}"""
    # token = os.environ["API_TOKEN"]  # secrets via env vars, never hardcode
    return {{"id": id}}


# --- repld wiring (shed on graduation — replace with @mcp.tool or @router.get) ---


async def _tool_{name}_example(id: int) -> dict:
    """TODO: what this tool does."""
    return await example(id)
'''


def run_gist(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        _print_gist_usage()
        return 0 if argv else 2
    cmd, rest = argv[0], argv[1:]
    entry = _GIST_COMMANDS.get(cmd)
    if entry is None:
        print(f"repld gist: unknown command '{cmd}'\n")
        _print_gist_usage()
        return 2
    func, _, _ = entry
    return func(rest)


def _print_gist_usage() -> None:
    print("repld gist — manage tool gists")
    print()
    width = max(len(usage) for _, usage, _ in _GIST_COMMANDS.values())
    for _, usage, desc in _GIST_COMMANDS.values():
        print(f"  repld gist {usage:<{width}}    {desc}")


def _gist_new(argv: list[str]) -> int:
    if argv and argv[0] in ("-h", "--help"):
        print("repld gist new <name> — scaffold ./gists/<name>.py")
        return 0
    if not argv:
        _print_gist_usage()
        return 2
    name = argv[0]
    if not name.isidentifier():
        print(f"error: '{name}' is not a valid Python identifier")
        return 2
    cwd = Path.cwd()
    gists_dir = cwd / "gists"
    gists_dir.mkdir(exist_ok=True)
    path = gists_dir / f"{name}.py"
    if path.exists():
        print(f"error: {path} already exists")
        return 1
    path.write_text(_GIST_TEMPLATE.format(name=name))
    print(f"created: {path}")
    print()
    print("Next steps:")
    print(f"  1. Edit {path} — rename the example tool, add your logic")
    print("  2. Core functions at top (portable), repld wiring at bottom (shed later)")
    print("  3. Tools appear in tools/list automatically on next MCP call")
    print("  4. Read repld://docs/production when ready to graduate")
    return 0


def _gist_add(argv: list[str]) -> int:
    from . import gists as _gists

    if argv and argv[0] in ("-h", "--help"):
        print("repld gist add <name> — link a gist registered in another project")
        return 0
    if not argv:
        print("repld gist add <name> — link a gist registered in another project")
        return 2
    name = argv[0]
    gists_dir = Path.cwd() / "gists"
    try:
        linked = _gists.add_link(name, gists_dir)
    except (LookupError, FileExistsError, ValueError) as e:
        print(f"error: {e}")
        return 1

    others = [n for n, _ in linked if n != name]
    src = dict(linked).get(name)
    print(f"linked: {name}  ({src})")
    if others:
        print(f"  + siblings: {', '.join(others)}")

    # Resolve deps for just the newly linked files (interactive prompt).
    missing = _gists.scan_deps(paths=[p for _, p in linked])
    if missing:
        _gists.install_deps(missing)

    print()
    print("Restart the kernel to load the linked gist(s).")
    return 0


def _gist_rm(argv: list[str]) -> int:
    from . import gists as _gists

    if argv and argv[0] in ("-h", "--help"):
        print("repld gist rm <name> | --stale — unlink a gist (or all dead links)")
        return 0
    gists_dir = Path.cwd() / "gists"
    try:
        if argv and argv[0] == "--stale":
            dropped = _gists.remove_stale_links(gists_dir)
            if dropped:
                print(f"dropped stale link(s): {', '.join(dropped)}")
            else:
                print("no stale links")
            return 0
        if not argv:
            print("repld gist rm <name> | --stale")
            return 2
        name = argv[0]
        if _gists.remove_link(name, gists_dir):
            print(f"unlinked: {name}")
            return 0
        print(f"error: '{name}' is not linked")
        return 1
    except ValueError as e:
        print(f"error: {e}")
        return 1


def _gist_list(argv: list[str]) -> int:
    from . import gists as _gists

    gists_dir = Path.cwd() / "gists"

    # Local gists (./gists), excluding privates.
    local = sorted(p.stem for p in gists_dir.glob("*.py") if not p.name.startswith("_"))
    if local:
        print("local (./gists):")
        for name in local:
            sig = _gists.signature_for_path(gists_dir / f"{name}.py")
            print(f"  {name:<20} {sig}")

    # Linked gists, flagging stale entries.
    try:
        links = _gists.read_links(gists_dir)
    except ValueError as e:
        print(f"error: {e}")
        links = {}
    if links:
        print("linked:")
        stale = 0
        for name in sorted(links):
            path = Path(links[name])
            if path.is_file():
                print(f"  {name:<20} {path}")
            else:
                stale += 1
                print(f"  {name:<20} {path}  (stale)")
        if stale:
            print()
            print(f"{stale} stale link(s) — repld gist rm --stale to drop")

    # Linkable: registered in other projects, not already here. Makes the valid
    # `gist add <name>` targets discoverable from the terminal (the _registry
    # MCP resource is agent-only).
    here = set(local) | set(links)
    cwd = Path.cwd().resolve()
    linkable: dict[str, list[tuple[str, str]]] = {}
    for name, entry in _gists.registry().items():
        if name in here or name.startswith("_"):
            continue
        path = Path(entry.get("path", ""))
        if not path.is_file() or cwd in path.resolve().parents:
            continue
        linkable.setdefault(entry.get("project", "?"), []).append(
            (name, entry.get("description", "") or "")
        )
    if linkable:
        print("linkable (other projects — repld gist add <name>):")
        for project in sorted(linkable):
            print(f"  {project}")
            for name, desc in sorted(linkable[project]):
                print(f"    {name:<20} {desc[:60]}")

    if not local and not links and not linkable:
        print("no gists in ./gists")
    return 0


# name → (handler, usage suffix, one-line help). Single source for both
# dispatch and the usage listing, so they can't drift.
_GIST_COMMANDS = {
    "new": (_gist_new, "new <name>", "scaffold ./gists/<name>.py"),
    "add": (_gist_add, "add <name>", "link a gist registered in another project"),
    "rm": (_gist_rm, "rm <name>", "unlink (use --stale to drop all dead links)"),
    "list": (_gist_list, "list", "show local + linked + linkable gists"),
}
