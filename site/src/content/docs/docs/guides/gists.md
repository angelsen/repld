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

A gist can register MCP tools that appear alongside built-in tools. Name a handler `_tool_{name}` with typed parameters and the schema is inferred automatically — no separate declaration needed:

```python
async def _tool_lookup_company(org_number: str) -> dict:
    """Look up a Norwegian company by org number."""
    from brreg import Brreg
    b = Brreg()
    return await b.company(org_number)
```

Type hints and defaults become the JSON schema (`str`→string, `int`→integer, `float`→number, `bool`→boolean, `list`→array, `dict`→object; no annotation defaults to string; no default marks the param required). The first docstring line becomes the tool description. Tools appear in `tools/list` automatically — no exec round-trip needed. `repld gist new <name>` scaffolds this pattern.

Legacy override: the older `__repld_tools__ = [...]` list + `_tool_*(args: dict)` convention still works for custom schemas, but prints a one-time deprecation warning per gist.

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
