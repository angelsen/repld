"""Project scaffolding (`repld init`).

Writes the per-project files repld needs (.mcp.json, .gitignore additions,
CLAUDE.md block) without touching anything project-specific. Idempotent:
re-running surfaces existing state instead of overwriting it.
"""

import json
import re
from pathlib import Path

_REPLD_MCP_ENTRY = {
    "type": "stdio",
    "command": "repld",
    "args": ["bridge"],
    "env": {},
}

_GITIGNORE_ENTRIES = [".pyrepl.lock", ".pyrepl.sock"]

_CLAUDE_MD_BLOCK = """\
<!-- repld:start -->
A repld kernel is running in this project — a persistent Python environment
with shared state, background tasks, reactive primitives (@every, defer,
notify), and browser-authenticated access to any web app the user is logged
into. When you see a repetitive task — checking statuses, polling APIs,
processing data on a schedule, monitoring for changes — suggest wiring it up.
One-shot work can become continuous. Manual checks can become notifications.
Run `!repld help` for the full API.
<!-- repld:end -->"""

_REPLD_BLOCK_RE = re.compile(r"<!-- repld:start -->.*?<!-- repld:end -->", re.DOTALL)


def _write_mcp_json(cwd: Path) -> str:
    path = cwd / ".mcp.json"
    if path.exists():
        try:
            cfg = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return f"warn: {path.name} exists but isn't valid JSON; skipping"
        if not isinstance(cfg, dict):
            return f"warn: {path.name} is JSON but not an object; skipping"
        servers = cfg.setdefault("mcpServers", {})
        if "repld" in servers:
            return f"ok:      {path.name} already has a repld entry"
        servers["repld"] = _REPLD_MCP_ENTRY
        path.write_text(json.dumps(cfg, indent=2) + "\n")
        return f"updated: {path.name} (added repld entry)"
    path.write_text(
        json.dumps({"mcpServers": {"repld": _REPLD_MCP_ENTRY}}, indent=2) + "\n"
    )
    return f"created: {path.name}"


def _update_gitignore(cwd: Path) -> str:
    path = cwd / ".gitignore"
    existing_text = ""
    existing_lines: set[str] = set()
    if path.exists():
        existing_text = path.read_text()
        existing_lines = {ln.strip() for ln in existing_text.splitlines()}
    missing = [e for e in _GITIGNORE_ENTRIES if e not in existing_lines]
    if not missing:
        return f"ok:      {path.name} already ignores repld runtime files"
    sep = "" if not existing_text or existing_text.endswith("\n") else "\n"
    block = sep + "\n# repld runtime state\n" + "\n".join(missing) + "\n"
    with open(path, "a") as f:
        f.write(block)
    verb = "updated" if existing_text else "created"
    return f"{verb}: {path.name} (added {', '.join(missing)})"


def _update_claude_md(cwd: Path, *, force: bool = False) -> str:
    path = cwd / "CLAUDE.md"
    if not path.exists():
        path.write_text(_CLAUDE_MD_BLOCK + "\n")
        return f"created: {path.name} (repld block)"

    text = path.read_text()
    m = _REPLD_BLOCK_RE.search(text)
    if m is None:
        # No markers — append
        sep = "" if not text or text.endswith("\n") else "\n"
        with open(path, "a") as f:
            f.write(sep + "\n" + _CLAUDE_MD_BLOCK + "\n")
        return f"updated: {path.name} (appended repld block)"

    # Markers found — check if content matches
    if m.group(0) == _CLAUDE_MD_BLOCK:
        return f"ok:      {path.name} repld block is current"

    # Content differs
    if not force:
        old = m.group(0).splitlines()
        new = _CLAUDE_MD_BLOCK.splitlines()
        print(f"  {path.name}: repld block differs from current version:")
        for line in old:
            if line not in new:
                print(f"    - {line}")
        for line in new:
            if line not in old:
                print(f"    + {line}")
        return f"skipped: {path.name} (use --force to overwrite)"

    updated = text[: m.start()] + _CLAUDE_MD_BLOCK + text[m.end() :]
    path.write_text(updated)
    return f"updated: {path.name} (repld block overwritten)"


_NEXT_STEPS = """\
Next:
  1. (Optional) Write repl.py to pre-load project state (clients, sessions,
     app handles).
  2. Start the kernel:
       repld                       # bare kernel
       repld --init repl.py        # with project bootstrap
  3. Open Claude Code in this directory:
       claude
     The MCP bridge connects automatically via .mcp.json.
"""


_GIST_TEMPLATE = '''\
"""{name} — TODO: one-line description."""

import json

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
    """TODO: implement."""
    return json.dumps({{"id": args["id"]}})
'''


def run_gist(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print("repld gist <name> — scaffold a tool gist in ./gists/<name>.py")
        return 0 if argv else 2
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


def run_init(argv: list[str]) -> int:
    if argv and argv[0] in ("-h", "--help"):
        print("repld init — scaffold .mcp.json + .gitignore + CLAUDE.md block")
        print()
        print("Run with no arguments. Idempotent.")
        print("  --force    Overwrite existing repld block in CLAUDE.md")
        return 0
    force = "--force" in argv
    rest = [a for a in argv if a != "--force"]
    if rest:
        print(f"unknown argument: {rest[0]}")
        return 2
    cwd = Path.cwd()
    print(_write_mcp_json(cwd))
    print(_update_gitignore(cwd))
    print(_update_claude_md(cwd, force=force))
    print()
    print(_NEXT_STEPS)
    return 0
