"""Project scaffolding (`repld init`).

Writes the per-project files repld needs (.mcp.json, .gitignore additions)
without touching anything project-specific. Idempotent: re-running surfaces
existing state instead of overwriting it.
"""

import json
from pathlib import Path

_REPLD_MCP_ENTRY = {
    "type": "stdio",
    "command": "repld",
    "args": ["bridge"],
    "env": {},
}

_GITIGNORE_ENTRIES = [".pyrepl.lock", ".pyrepl.sock"]


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


_NEXT_STEPS = """\
Next:
  1. (Optional) Write repl.py to pre-load project state (clients, sessions,
     app handles). See examples/fastapi/repl.py for the shape.
  2. Start the kernel:
       repld                       # bare kernel
       repld --init repl.py        # with project bootstrap
  3. Open Claude Code in this directory:
       claude
     The MCP bridge connects automatically via .mcp.json.

Suggested CLAUDE.md addition:

  ## repld
  This project uses repld. Bring up the kernel with `repld --init repl.py`.
  Long cells return done:false; channel push on completion.
  For agent docs: `!repld help`.
"""


def run_init(argv: list[str]) -> int:
    if argv and argv[0] in ("-h", "--help"):
        print("repld init — scaffold .mcp.json + .gitignore in cwd")
        print()
        print("Run with no arguments. Idempotent.")
        return 0
    if argv:
        print(f"unknown argument: {argv[0]}")
        return 2
    cwd = Path.cwd()
    print(_write_mcp_json(cwd))
    print(_update_gitignore(cwd))
    print()
    print(_NEXT_STEPS)
    return 0
