"""The `repld gist` command group — scaffold, link, unlink, list.

`new` scaffolds a local gist file; `add`/`rm`/`list` manage cross-project links
(registry resolution + the ./gists/.links manifest, see `gists.py`). Dispatched
from `cli.py`'s _SUBCOMMANDS table.
"""

from pathlib import Path

_GIST_TEMPLATE = '''\
"""{name} — TODO: one-line description."""

import json

# __repld_deps__ = ["httpx>=0.27"]  # uncomment to auto-install at boot

__repld_tools__ = [
    {{
        "name": "{name}_example",
        "description": "TODO: what this tool does",
        "inputSchema": {{
            "type": "object",
            "properties": {{
                "id": {{"type": "integer", "description": "TODO: describe"}},
            }},
            "required": ["id"],
        }},
    }},
]


async def _tool_{name}_example(args: dict) -> str:
    """TODO: what this returns. Document the shape: -> {{id, ...}}"""
    return json.dumps({{"id": args["id"]}})
'''


def run_gist(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        _print_gist_usage()
        return 0 if argv else 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "new":
        return _gist_new(rest)
    if cmd == "add":
        return _gist_add(rest)
    if cmd == "rm":
        return _gist_rm(rest)
    if cmd == "list":
        return _gist_list(rest)
    print(f"repld gist: unknown command '{cmd}'\n")
    _print_gist_usage()
    return 2


def _print_gist_usage() -> None:
    print("repld gist — manage tool gists")
    print()
    print("  repld gist new <name>    scaffold ./gists/<name>.py")
    print("  repld gist add <name>    link a gist registered in another project")
    print("  repld gist rm <name>     unlink (use --stale to drop all dead links)")
    print("  repld gist list          show local + linked + linkable gists")


def _gist_new(argv: list[str]) -> int:
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
    print(f"  1. Edit {path} — rename the example tool, add your own")
    print("  2. Tools appear in tools/list automatically on next MCP call")
    print("  3. Handler convention: _tool_{tool_name}(args: dict) -> str | dict")
    return 0


def _gist_add(argv: list[str]) -> int:
    from . import gists as _gists

    if not argv or argv[0] in ("-h", "--help"):
        print("repld gist add <name> — link a gist registered in another project")
        return 2
    name = argv[0]
    gists_dir = Path.cwd() / "gists"
    try:
        linked = _gists.add_link(name, gists_dir)
    except LookupError as e:
        print(f"error: {e}")
        return 1
    except FileExistsError as e:
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

    gists_dir = Path.cwd() / "gists"
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
    links = _gists.read_links(gists_dir)
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
