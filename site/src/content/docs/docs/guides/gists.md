---
title: Gists guide
description: Writing reusable Python modules that wrap any web app's API.
---

A gist is a plain Python file in `./gists/` (project-local) or `~/.repld/gists/` (global) that the kernel hot-reloads on import. Re-import after editing — the kernel evicts the stale module and loads the new one.

## Writing a gist

```bash
repld gist new myapp
```

This scaffolds `./gists/myapp.py` with a docstring, `__repld_usage__`, and a starter class.

A typical gist wraps a web app's internal API:

```python
# gists/myapp.py
"""MyApp — accounts, transactions, reports."""

class MyApp:
    def __init__(self, tab):
        self._tab = tab

    @classmethod
    async def connect(cls):
        import repld
        tab = await repld.browser.get("*myapp.com*")
        return cls(tab)

    async def accounts(self):
        return (await self._tab.fetch("/api/accounts"))["body"]

    async def create_order(self, items):
        return await self._tab.fetch("/api/orders", method="POST", body={"items": items})
```

```python
from myapp import MyApp
app = await MyApp.connect()
await app.accounts()
```

## Auto-reload

Re-importing a gist reloads it:

```python
from myapp import MyApp  # first import
# ... edit myapp.py ...
from myapp import MyApp  # picks up changes
```

The kernel tracks mtimes and evicts stale modules from `sys.modules`.

## Dependencies

Declare external dependencies:

```python
__repld_deps__ = ["httpx>=0.27", "pandas>=2.3"]
```

The kernel scans these at boot and prompts to install missing packages. Use `"."` to install the gist's own project as editable:

```python
__repld_deps__ = ["."]  # installs the project containing this gist
```

## MCP tool registration

A gist can register MCP tools that appear alongside built-in tools:

```python
__repld_tools__ = [
    {
        "name": "lookup_company",
        "description": "Look up a Norwegian company by org number",
        "inputSchema": {
            "type": "object",
            "properties": {"org_number": {"type": "string"}},
            "required": ["org_number"],
        },
    }
]

async def _tool_lookup_company(args):
    from brreg import Brreg
    b = Brreg()
    return await b.company(args["org_number"])
```

Tools appear in `tools/list` automatically. The handler is `_tool_{name}` — the kernel discovers it by convention.

## Cross-project linking

Gists are tracked in a central registry (`~/.config/repld/gist-registry.json`). Link a gist from another project without copying:

```bash
repld gist add weather    # resolves from registry, writes ./gists/.links
repld gist list           # shows local + linked + linkable
repld gist rm weather     # unlink
repld gist rm --stale     # clean up broken links
```

The `.links` manifest records absolute paths. Local gists always shadow linked ones of the same name.

## Conventions

- **Module docstring first line** becomes the gist's description in MCP instructions
- **`__repld_usage__`** overrides the auto-generated import hint
- **`__repld_help__`** overrides the first-line description
- **Async classes** should have a `connect()` classmethod that resolves browser tabs
- **Return dicts/lists**, not custom objects — the agent works with JSON-serializable data
