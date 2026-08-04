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

Describe an individual parameter by wrapping its type in `Annotated`:

```python
from typing import Annotated

async def _tool_lookup_company(
    org_number: Annotated[str, "Nine-digit Norwegian organisation number"],
    include_roles: Annotated[bool, "Also fetch board members"] = False,
) -> dict: ...
```

:::caution[Removed in 0.2]
The pre-0.1.0 `__repld_tools__ = [...]` list plus `_tool_*(args: dict)` convention is gone. It is now **ignored**, not warned about, so a gist still declaring one quietly loses its tools. Give each argument its own typed parameter, move the tool description to the docstring's first line, and put per-parameter descriptions in `Annotated`. `repld gist lint` is the only thing that will tell you.
:::

## Cross-project linking

Gists are tracked in a central registry (`~/.config/repld/gist-registry.json`). Link a gist from another project without copying:

```bash
repld gist add weather    # resolves from registry, writes ./gists/.links
repld gist list           # shows local + linked + linkable
repld gist rm weather     # unlink
repld gist rm --stale     # clean up broken links
```

The `.links` manifest records absolute paths and is meant to be committed — stale entries are skipped at load rather than rewritten. Local gists always shadow linked ones of the same name.

## Starting from someone else's gist

`fetch` is `new`'s sibling, not `add`'s: it **copies** the `.py` files out of a GitHub gist into `./gists` (or `~/.repld/gists` with `--global`), stamping a `# source:` header.

```bash
repld gist fetch https://gist.github.com/someone/abc123
repld gist fetch <url> --name weather --global
```

Nothing tracks the file afterwards, so `rm` — which only unlinks — is not how you undo it; delete the file. Only `gist.github.com` ids are accepted, and the fetched file's `__repld_deps__` is deliberately _not_ installed: this is code from a URL, and its dependency list shouldn't drive an install before you've read the file.

## Linting

```bash
repld gist lint              # everything a kernel here would import
repld gist lint --local      # just ./gists — usable as a per-project CI gate
repld gist lint weather
```

Checks the docstring first line, documented return shapes, undeclared `__repld_deps__`, and the removed `__repld_tools__` API. Suppress a rule inline with `# gistlint: ignore=<rule>`.

## Conventions

- **Module docstring first line** becomes the gist's description in MCP instructions
- **`__repld_usage__`** overrides the auto-generated import hint
- **`__repld_help__`** overrides the first-line description
- **Async classes** should have a `connect()` classmethod that resolves browser tabs
- **Return dicts/lists**, not custom objects — the agent works with JSON-serializable data
