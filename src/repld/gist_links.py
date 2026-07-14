"""Cross-project gist links — ./gists/.links manifest + registry resolution.

`repld gist add <name>` resolves a registered gist (plus its same-dir sibling
imports) to absolute paths and records them in a committed ./gists/.links
manifest — no copying. At kernel boot, _load_links() populates the live
`_linked` overlay consulted by gists.py's finder/iterators *after* local
dirs, so local gists always shadow linked ones.

Shares the parse cache and registry with gists.py; the two modules import
each other and access attributes at call time (never `from x import y`),
which keeps the cycle safe and preserves test monkeypatching of
`gists.registry`.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

from . import gists
from .ipc import atomic_write_json

# Cross-project linked gists: name → absolute source path. Populated from
# ./gists/.links at install() time; consulted by the finder + iterators after
# local dirs (so local gists always shadow a linked one of the same name).
# Never rebound — gists.py holds references via this module's namespace.
_linked: dict[str, Path] = {}
_LINKS_FILENAME = ".links"


def linked_names() -> frozenset[str]:
    """Names currently in the live _linked overlay."""
    return frozenset(_linked)


def linked_path(name: str) -> Path | None:
    """Cross-project linked gist path for an exact name, if the file still exists."""
    p = _linked.get(name)
    return p if p is not None and p.is_file() else None


def linked_items() -> list[tuple[str, Path]]:
    """(name, path) pairs for linked gists whose file still exists, name-sorted."""
    return [(n, p) for n, p in sorted(_linked.items()) if p.is_file()]


def read_links(gists_dir: Path) -> dict[str, str]:
    """Read the link manifest. Returns {name: abspath}; {} if absent.

    Raises ValueError if the manifest exists but won't parse — callers must
    not guess: treating a corrupt manifest as empty would make `gist add`
    rewrite it and silently drop every other committed link.
    """
    path = gists_dir / _LINKS_FILENAME
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"corrupt link manifest {path}: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"corrupt link manifest {path}: expected a JSON object")
    return {str(k): str(v) for k, v in data.items()}


def write_links(gists_dir: Path, links: dict[str, str]) -> None:
    """Write the link manifest (pretty JSON, name-sorted)."""
    gists_dir.mkdir(parents=True, exist_ok=True)
    path = gists_dir / _LINKS_FILENAME
    ordered = {k: links[k] for k in sorted(links)}
    atomic_write_json(path, ordered, indent=2)


def _load_links(gists_dir: Path) -> None:
    """Populate the live _linked overlay from the manifest.

    Skips (with a stderr warning) any entry whose path no longer exists; never
    rewrites the manifest — it is committed, and silently editing it would dirty
    the working tree. Use `repld gist rm --stale` to drop dead links.
    """
    _linked.clear()
    try:
        links = read_links(gists_dir)
    except ValueError as e:
        print(
            f"repld: {e} — linked gists unavailable (fix or delete the file)",
            file=sys.stderr,
        )
        return
    for name, raw in links.items():
        p = Path(raw)
        if p.is_file():
            _linked[name] = p
        else:
            print(
                f"repld: linked gist '{name}' path gone: {raw} (repld gist rm {name})",
                file=sys.stderr,
            )


def _sibling_imports(path: Path) -> set[str]:
    """Top-level names imported by `path` that exist as `<name>.py` beside it.

    Same-directory match = sibling gist; everything else is stdlib/third-party.
    """
    siblings: set[str] = set()
    tree = gists._parse(path)
    if tree is None:
        return siblings
    src_dir = path.parent
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module.split(".")[0]]
        for n in names:
            if n != path.stem and (src_dir / f"{n}.py").is_file():
                siblings.add(n)
    return siblings


def link_targets(name: str) -> list[tuple[str, Path]]:
    """Resolve `name` via the registry + transitive same-dir sibling imports.

    Returns [(name, path), ...] — the full set that must be linked for `name` to
    import. Raises LookupError (listing known projects) if `name` isn't registered.
    """
    reg = gists.registry()
    if name not in reg:
        projects = sorted({v.get("project", "?") for v in reg.values()})
        raise LookupError(
            f"gist '{name}' is not registered. Known projects:\n  "
            + "\n  ".join(projects)
        )
    resolved: dict[str, Path] = {}
    queue = [name]
    while queue:
        cur = queue.pop()
        if cur in resolved:
            continue
        entry = reg.get(cur)
        # Siblings may not be registered themselves — fall back to a path beside
        # an already-resolved gist.
        if entry is not None:
            p = Path(entry["path"])
        else:
            candidates = (rp.parent / f"{cur}.py" for rp in resolved.values())
            p = next((c for c in candidates if c.is_file()), Path())
        if not p.is_file():
            if cur == name:
                raise LookupError(
                    f"gist '{name}' is registered at {p} but the file is gone"
                    " — import it from its home project to re-register, or"
                    " remove the entry from ~/.config/repld/gist-registry.json"
                )
            print(
                f"repld: sibling gist '{cur}' could not be resolved — "
                f"'{name}' may not import without it",
                file=sys.stderr,
            )
            continue
        resolved[cur] = p
        queue.extend(_sibling_imports(p) - resolved.keys())
    return list(resolved.items())


def add_link(name: str, gists_dir: Path) -> list[tuple[str, Path]]:
    """Link `name` (and its siblings) into gists_dir's manifest.

    Refuses on local collision — a target already present in ./gists or
    ~/.repld/gists, or resolving to a path inside this project. Returns the newly
    linked (name, path) pairs.
    """
    targets = link_targets(name)
    project_root = gists_dir.parent.resolve()
    for tname, tpath in targets:
        if (gists_dir / f"{tname}.py").is_file():
            raise FileExistsError(
                f"'{tname}' already exists locally: {gists_dir / f'{tname}.py'}"
            )
        global_gist = Path.home() / ".repld" / "gists" / f"{tname}.py"
        if global_gist.is_file():
            raise FileExistsError(f"'{tname}' already exists globally: {global_gist}")
        if project_root in tpath.resolve().parents:
            raise FileExistsError(f"'{tname}' already lives in this project: {tpath}")
    links = read_links(gists_dir)
    for tname, tpath in targets:
        links[tname] = str(tpath.resolve())
    write_links(gists_dir, links)
    return targets


def remove_link(name: str, gists_dir: Path) -> bool:
    """Drop `name` from the manifest (works on stale names). Returns True if removed.

    Leaves siblings in place — they may be shared with other linked gists.
    """
    links = read_links(gists_dir)
    if name not in links:
        return False
    del links[name]
    write_links(gists_dir, links)
    return True


def remove_stale_links(gists_dir: Path) -> list[str]:
    """Drop every manifest entry whose path is gone. Returns the removed names."""
    links = read_links(gists_dir)
    stale = [n for n, p in links.items() if not Path(p).is_file()]
    if stale:
        for n in stale:
            del links[n]
        write_links(gists_dir, links)
    return stale
