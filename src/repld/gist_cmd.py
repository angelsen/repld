"""The `repld gist` command group — scaffold, link, unlink, list.

`new` scaffolds a local gist file; `add`/`rm`/`list` manage cross-project links
(registry resolution + the ./gists/.links manifest, see `gist_links.py`).
Dispatched from `cli.py`'s _SUBCOMMANDS table.
"""

from pathlib import Path

from . import cli_args

_GIST_TEMPLATE = '''\
"""{name} — TODO: one-line description."""

from typing import Annotated

# __repld_deps__ = ["httpx>=0.27"]  # uncomment to auto-install at boot

# --- core logic (portable — keeps on graduation) ---


async def example(id: int) -> dict:
    """TODO: what this does. -> {{id, ...}}"""
    # token = os.environ["API_TOKEN"]  # secrets via env vars, never hardcode
    return {{"id": id}}


# --- repld wiring (shed on graduation — replace with @mcp.tool or @router.get) ---


async def _tool_{name}_example(
    id: Annotated[int, "TODO: what this id refers to"],
) -> dict:
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
    return entry[0](rest)


def _print_gist_usage() -> None:
    print("repld gist — manage tool gists")
    print()
    width = max(len(spec) for _, spec, _, _ in _GIST_COMMANDS.values())
    for _, spec, desc, _ in _GIST_COMMANDS.values():
        print(f"  repld gist {spec:<{width}}    {desc}")


def _usage(verb: str) -> str:
    """A verb's usage text, composed from the one table that also lists it."""
    _, spec, desc, detail = _GIST_COMMANDS[verb]
    line = f"repld gist {spec} — {desc}"
    return f"{line}\n{detail}" if detail else line


def _shadow_conflict(
    stem: str,
    *,
    to_global: bool,
    local_dir: Path,
    global_dir: Path,
    links: dict[str, str],
) -> str | None:
    """Error text if *stem* would shadow a gist outside the destination dir.

    A name that resolves in two places silently shadows one of them at import
    time — `_GistFinder` consults local dirs before the linked overlay — so
    every command that writes a fresh `.py` into a gists dir enforces this:
    `new` and `fetch` alike, for the reason `add_link` already did. The
    destination dir's *own* collision is left to the caller, because the two
    disagree there: `fetch` overwrites under `--force`, `new` never does.
    """
    other = (local_dir if to_global else global_dir) / f"{stem}.py"
    if other.is_file():
        scope = "locally" if other.parent == local_dir else "globally"
        return f"'{stem}' already exists {scope}: {other}"
    if stem in links:
        return f"'{stem}' is already linked from {links[stem]}"
    return None


def _gist_new(argv: list[str]) -> int:
    from . import gist_links

    usage = _usage("new")
    if cli_args.wants_help(argv):
        print(usage)
        return 0
    bad = cli_args.check_args("repld gist new", argv, usage, positionals=1)
    if bad is not None:
        return bad
    if not argv:
        print(usage)
        return 2
    name = argv[0]
    if not name.isidentifier():
        print(f"error: '{name}' is not a valid Python identifier")
        return 2
    cwd = Path.cwd()
    gists_dir = cwd / "gists"
    path = gists_dir / f"{name}.py"
    if path.exists():
        print(f"error: {path} already exists")
        return 1
    try:
        links = gist_links.read_links(gists_dir)
    except ValueError as e:
        print(f"error: {e}")
        return 1
    conflict = _shadow_conflict(
        name,
        to_global=False,
        local_dir=gists_dir,
        global_dir=Path.home() / ".repld" / "gists",
        links=links,
    )
    if conflict:
        print(f"error: {conflict}")
        return 1
    gists_dir.mkdir(exist_ok=True)
    path.write_text(_GIST_TEMPLATE.format(name=name))
    print(f"created: {path}")
    print()
    print("Next steps:")
    print(f"  1. Edit {path} — rename the example tool, add your logic")
    print("  2. Core functions at top (portable), repld wiring at bottom (shed later)")
    print("  3. Tools appear in tools/list automatically on next MCP call")
    print("  4. Read repld://docs/production when ready to graduate")
    return 0


_GIST_HOSTS = ("gist.github.com", "gist.githubusercontent.com")

_FETCH_HEADER = (
    "# source: {url}\n"
    "# fetched: {date} by `repld gist fetch` — a snapshot, not a link.\n"
    "# Edits here are yours; re-fetch with --force to take the gist's version back.\n"
)


def _parse_gist_ref(ref: str) -> str:
    """The gist id from a gist.github.com URL, a raw URL, or a bare id.

    Deliberately narrow: only GitHub gists, never an arbitrary URL to a `.py`.
    The file lands somewhere a kernel imports at boot, so the set of places it
    can come from should be one a reader recognises.
    """
    import re
    from urllib.parse import urlparse

    if "/" not in ref and ":" not in ref:
        candidate = ref
    else:
        u = urlparse(ref)
        if u.scheme not in ("http", "https") or u.hostname not in _GIST_HOSTS:
            raise ValueError(
                f"not a GitHub gist URL: {ref}\n"
                "       expected https://gist.github.com/<user>/<id>, or a bare gist id"
            )
        # /<user>/<id>, /<id>, and raw URLs (/<user>/<id>/raw/<rev>/<file>)
        parts = [p for p in u.path.split("/") if p]
        if not parts:
            raise ValueError(f"no gist id in {ref}")
        candidate = parts[1] if len(parts) > 1 else parts[0]
    if not re.fullmatch(r"[0-9a-f]{8,}", candidate):
        raise ValueError(f"'{candidate}' is not a gist id")
    return candidate


def _fetch_gist(gist_id: str, timeout: float = 20.0) -> tuple[str, dict[str, str]]:
    """``(description, {filename: source})`` for a gist, `.py` files only.

    A secret gist is readable by id without a token, which is the common case
    for one you made yourself; anything needing auth is reported as such rather
    than half-attempted.
    """
    import json
    import urllib.error
    import urllib.request

    api = f"https://api.github.com/gists/{gist_id}"
    req = urllib.request.Request(
        api,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "repld"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            meta = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise LookupError(
                f"gist {gist_id} not found — deleted, or private to another "
                "account (repld does not send credentials)"
            ) from e
        raise LookupError(f"GitHub returned HTTP {e.code} for {api}") from e
    except urllib.error.URLError as e:
        raise LookupError(f"could not reach GitHub: {e.reason}") from e
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise LookupError(f"unreadable response from {api}: {e}") from e

    files: dict[str, str] = {}
    for fname, entry in (meta.get("files") or {}).items():
        if not fname.endswith(".py"):
            continue
        content = entry.get("content")
        if content is None or entry.get("truncated"):
            # Files over ~1 MB come back truncated with the body at raw_url.
            try:
                with urllib.request.urlopen(entry["raw_url"], timeout=timeout) as r:
                    content = r.read().decode("utf-8")
            except (urllib.error.URLError, KeyError, UnicodeDecodeError) as e:
                raise LookupError(f"could not read {fname}: {e}") from e
        files[fname] = content
    return meta.get("description") or "", files


def _gist_fetch(argv: list[str]) -> int:
    import datetime

    from . import gist_deps, gist_links

    usage = _usage("fetch")
    if cli_args.wants_help(argv):
        print(usage)
        return 0
    to_global = "--global" in argv
    force = "--force" in argv
    rest = [a for a in argv if a not in ("--global", "--force")]
    rename: str | None = None
    if "--name" in rest:
        i = rest.index("--name")
        if i + 1 >= len(rest):
            print("error: --name needs a value")
            return 2
        rename = rest[i + 1]
        del rest[i : i + 2]
    unknown = [a for a in rest if a.startswith("-")]
    if unknown:
        print(f"error: unknown flag '{unknown[0]}'\n\n{usage}")
        return 2
    if len(rest) != 1:
        print(usage)
        return 2

    try:
        gist_id = _parse_gist_ref(rest[0])
        description, files = _fetch_gist(gist_id)
    except (ValueError, LookupError) as e:
        print(f"error: {e}")
        return 1
    if not files:
        print(f"error: gist {gist_id} contains no .py files")
        return 1
    if rename is not None:
        if len(files) > 1:
            print(
                f"error: --name needs a single-file gist; this one has "
                f"{len(files)}: {', '.join(sorted(files))}"
            )
            return 1
        if not rename.isidentifier():
            print(f"error: '{rename}' is not a valid Python identifier")
            return 2
        files = {f"{rename}.py": next(iter(files.values()))}

    local_dir = Path.cwd() / "gists"
    global_dir = Path.home() / ".repld" / "gists"
    dest_dir = global_dir if to_global else local_dir
    try:
        links = gist_links.read_links(local_dir)
    except ValueError as e:
        # A corrupt manifest is an error to report, not a traceback — `add`,
        # `rm` and `list` all treat it that way.
        print(f"error: {e}")
        return 1
    for fname in sorted(files):
        stem = fname[: -len(".py")]
        if not stem.isidentifier():
            print(f"error: '{fname}' is not importable as a module name")
            return 1
        target = dest_dir / fname
        if target.exists() and not force:
            print(f"error: {target} already exists (use --force to overwrite)")
            return 1
        conflict = _shadow_conflict(
            stem,
            to_global=to_global,
            local_dir=local_dir,
            global_dir=global_dir,
            links=links,
        )
        if conflict:
            print(f"error: {conflict}")
            return 1

    url = f"https://gist.github.com/{gist_id}"
    header = _FETCH_HEADER.format(url=url, date=datetime.date.today().isoformat())
    dest_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for fname, content in sorted(files.items()):
        target = dest_dir / fname
        target.write_text(header + content, encoding="utf-8")
        written.append(target)

    if description:
        print(f"{description}\n")
    for target in written:
        print(f"fetched: {target}  ({target.stat().st_size:,} bytes)")

    # Deliberately *not* installed here, unlike `gist add`. That links code you
    # wrote in another project; this is code from a URL, and its dependency
    # list should not drive an install before you have read the file. The next
    # kernel boot prompts for them anyway, wherever there is a terminal.
    declared = gist_deps.scan_deps(paths=written)
    if declared:
        names = sorted({d.requirement for d in declared})
        print(f"\ndeclares dependencies not installed here: {', '.join(names)}")

    print()
    print("Next steps:")
    print(f"  1. Read {written[0]} — it runs in your kernel on the next boot")
    print(f"  2. repld gist lint {written[0].stem} — check it over")
    deps_note = " (and to be prompted for its deps)" if declared else ""
    print(f"  3. Restart the kernel to load it{deps_note}")
    return 0


def _gist_add(argv: list[str]) -> int:
    from . import gist_deps, gist_links

    usage = _usage("add")
    if cli_args.wants_help(argv):
        print(usage)
        return 0
    bad = cli_args.check_args("repld gist add", argv, usage, positionals=1)
    if bad is not None:
        return bad
    if not argv:
        print(usage)
        return 2
    name = argv[0]
    gists_dir = Path.cwd() / "gists"
    try:
        linked = gist_links.add_link(name, gists_dir)
    except (LookupError, FileExistsError, ValueError) as e:
        print(f"error: {e}")
        return 1

    others = [n for n, _ in linked if n != name]
    src = dict(linked).get(name)
    print(f"linked: {name}  ({src})")
    if others:
        print(f"  + siblings: {', '.join(others)}")

    # Resolve deps for just the newly linked files (interactive prompt).
    missing = gist_deps.scan_deps(paths=[p for _, p in linked])
    if missing:
        gist_deps.install_deps(missing)

    print()
    print("Restart the kernel to load the linked gist(s).")
    return 0


def _gist_rm(argv: list[str]) -> int:
    from . import gist_links

    usage = _usage("rm")
    if cli_args.wants_help(argv):
        print(usage)
        return 0
    bad = cli_args.check_args(
        "repld gist rm", argv, usage, flags=("--stale",), positionals=1
    )
    if bad is not None:
        return bad
    if "--stale" in argv and any(not a.startswith("-") for a in argv):
        # --stale drops every dead link; pairing it with a name reads as
        # "only this one", which it never meant.
        print("repld gist rm: --stale takes no name\n")
        print(usage)
        return 2
    gists_dir = Path.cwd() / "gists"
    try:
        if argv and argv[0] == "--stale":
            dropped = gist_links.remove_stale_links(gists_dir)
            if dropped:
                print(f"dropped stale link(s): {', '.join(dropped)}")
            else:
                print("no stale links")
            return 0
        if not argv:
            print(usage)
            return 2
        name = argv[0]
        if gist_links.remove_link(name, gists_dir):
            print(f"unlinked: {name}")
            return 0
        print(f"error: '{name}' is not linked")
        return 1
    except ValueError as e:
        print(f"error: {e}")
        return 1


def _gist_lint(argv: list[str]) -> int:
    from . import gist_lint
    from . import gists as _gists

    usage = _usage("lint")
    if cli_args.wants_help(argv):
        print(usage)
        return 0
    bad = cli_args.check_args(
        "repld gist lint", argv, usage, flags=("--local",), positionals=None
    )
    if bad is not None:
        return bad
    local_only = "--local" in argv
    argv = [a for a in argv if a != "--local"]
    gists_dir = Path.cwd() / "gists"
    # Same dirs + precedence a live kernel resolves against (kernel.py boot).
    # Needed even under --local: _find_gist resolves names against them.
    # create=False — linting is a read-only question and must not conjure the
    # directories it finds nothing in.
    _gists.install([Path.home() / ".repld" / "gists", gists_dir], create=False)

    if argv:
        paths = []
        missing = []
        nonlocal_hits = []
        for name in argv:
            # Under --local, look in ./gists first rather than asking
            # _find_gist, which walks the kernel's precedence (global before
            # local) and has no local_only notion: with both a global and a
            # local `helpers`, it returned the global one and this rejected
            # the name outright — while the no-name form linted that same
            # local file happily.
            local_hit = gists_dir / f"{name}.py"
            p = (
                local_hit
                if local_only and local_hit.is_file()
                else _gists._find_gist(name)
            )
            if p is None:
                missing.append(name)
            elif local_only and p.parent.resolve() != gists_dir.resolve():
                nonlocal_hits.append((name, p))
            else:
                paths.append(p)
        if missing or nonlocal_hits:
            for name in missing:
                print(f"error: '{name}' not found (local, global, or linked)")
            for name, p in nonlocal_hits:
                print(f"error: '{name}' is not a local gist (found at {p})")
            return 2
    else:
        # include_private: privates aren't gists, but they are imported, so
        # their deps/shape/legacy problems are real. lint_file() skips the
        # firstline rule on them, which is the only one that doesn't apply.
        paths = sorted(
            gists_dir.glob("*.py")
            if local_only
            else _gists._iter_gist_files(include_private=True)
        )
        if not paths:
            print("no gists found")
            return 0

    findings = gist_lint.lint_paths(paths)
    for f in findings:
        print(f)
    if findings:
        print(
            f"\n{len(findings)} finding(s) in {len({f.path for f in findings})} file(s)"
        )
        return 1
    print(f"clean: {len(paths)} gist(s) checked")
    return 0


def _gist_list(argv: list[str]) -> int:
    from . import gist_links
    from . import gists as _gists

    usage = _usage("list")
    if cli_args.wants_help(argv):
        print(usage)
        return 0
    if argv:
        print(f"repld gist list: unknown argument {argv[0]!r}\n")
        print(usage)
        return 2

    gists_dir = Path.cwd() / "gists"

    # Local gists (./gists), excluding privates.
    local = sorted(
        p.stem for p in gists_dir.glob("*.py") if _gists.is_public_gist_file(p)
    )
    if local:
        print("local (./gists):")
        for name in local:
            sig = _gists.signature_for_path(gists_dir / f"{name}.py")
            print(f"  {name:<20} {sig}")

    # Linked gists, flagging stale entries. A corrupt manifest is an error,
    # not an empty section: continuing would print a listing that silently
    # omits every linked gist and still exit 0, so a script gating on this
    # reads a truncated answer as a complete one. `add`, `rm` and `fetch`
    # all return 1 here.
    try:
        links = gist_links.read_links(gists_dir)
    except ValueError as e:
        print(f"error: {e}")
        return 1
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


# name → (handler, arg spec, one-line help, extra lines for `<verb> --help`).
#
# The single source for dispatch, for the `repld gist --help` listing, *and*
# for each verb's own usage line. The verbs used to keep a third copy apiece
# and four of the six had drifted from this table — under a comment claiming
# they couldn't, which is what a "single source" is worth when nothing reads
# from it. `_usage()` is the thing that makes the claim true.
#
# Keep the spec short: `_print_gist_usage` pads every entry to the longest
# one, so a flag list belongs in the detail lines, not here.
_GIST_COMMANDS = {
    "new": (_gist_new, "new <name>", "scaffold ./gists/<name>.py", ""),
    "fetch": (
        _gist_fetch,
        "fetch <gist-url>",
        "download a GitHub gist into ./gists",
        "Like `new`, but seeded from someone else's working code.\n"
        "flags: --global (install to ~/.repld/gists), --name NAME, --force",
    ),
    "add": (_gist_add, "add <name>", "link a gist registered in another project", ""),
    "rm": (_gist_rm, "rm <name> | --stale", "unlink a gist (or all dead links)", ""),
    "list": (_gist_list, "list", "show local + linked + linkable gists", ""),
    "lint": (
        _gist_lint,
        "lint [--local] [name...]",
        "check gist(s) against best practices",
        "Default scope is everything a kernel here would import — "
        "~/.repld/gists, ./gists, and linked.\n"
        "--local restricts to ./gists, so the exit code is usable as a gate "
        "for this project alone.",
    ),
}
