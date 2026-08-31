---
title: Gists guide
description: Writing reusable Python modules that wrap any web app's API.
---

A gist is a plain Python file in `./gists/` (project-local) or `~/.repld/gists/` (global) that the kernel hot-reloads on import. Re-import after editing — the kernel evicts the stale module and loads the new one.

## Writing a gist

```bash
repld gist new myapp
```

This scaffolds `./gists/myapp.py` split the way a gist is meant to grow: a docstring, a commented-out `__repld_deps__`, a plain `async def` holding the logic, and an `Annotated`-typed `_tool_*` wrapper below it. The top half is portable and survives if you ever [graduate the gist](/repld/production/) into a real service; the bottom half is the repld wiring you shed.

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

The kernel scans these at boot and asks before installing anything missing. Two other forms are accepted: `"."` installs the gist's own project as editable, and `"path:some/dir"` just prepends a local directory to `sys.path` for vendored code with nothing to install.

```python
__repld_deps__ = [
    "httpx>=0.27",    # PEP 508 requirement — installed
    ".",              # the project containing this gist, editable
    "path:./vendor",  # no install — straight onto sys.path
]
```

Installs land in a shared, interpreter-versioned directory (`~/.local/share/repld/deps/py3.12`), never in your project's venv — `uv sync` would prune them, and a kernel bound to a project runs under an ephemeral `uv run` overlay that differs every invocation. The directory is _appended_ to `sys.path`, so your project's own packages always win.

:::caution[Headless kernels need one deliberate install]
The prompt has to be answered at the kernel's own stdin, and since kernels spawn lazily the usual one is headless with stdin on `/dev/null`. It doesn't read that as consent: it reports what's missing and prints the `uv pip install --target …` command to run. Start `repld` in a pane once to be asked instead. `repld exec` can't answer it — that's a separate process talking over the socket, while the install runs in the kernel.
:::

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
repld gist add weather  # resolves from registry, writes ./gists/.links
repld gist list         # shows local + linked + linkable
repld gist rm weather   # unlink
repld gist rm --stale   # clean up broken links
```

The `.links` manifest records absolute paths and is meant to be committed — stale entries are skipped at load rather than rewritten. Local gists always shadow linked ones of the same name.

## Starting from someone else's gist

`fetch` is `new`'s sibling, not `add`'s: it **copies** the `.py` files out of a GitHub gist into `./gists` (or `~/.repld/gists` with `--global`), stamping a `# source:` header.

```bash
repld gist fetch https://gist.github.com/someone/abc123
repld gist fetch <url> --name weather --global
```

Nothing tracks the file afterwards, so `rm` — which only unlinks — is not how you undo it; delete the file. A bare gist id works too, as does a raw `gist.githubusercontent.com` URL, but nothing else: the file lands somewhere a kernel imports at boot, so the set of origins stays one a reader recognises. The fetched file's `__repld_deps__` is deliberately _not_ installed — this is code from a URL, and its dependency list shouldn't drive an install before you've read the file.

## Linting

```bash
repld gist lint          # everything a kernel here would import
repld gist lint --local  # just ./gists — usable as a per-project CI gate
repld gist lint weather
```

Checks the docstring first line, documented return shapes, undeclared `__repld_deps__`, and the removed `__repld_tools__` API. Suppress a rule inline with `# gistlint: ignore=<rule>`.

## Conventions

- **Module docstring first line** becomes the gist's description in MCP instructions
- **`__repld_usage__`** overrides the auto-generated import hint
- **`__repld_help__`** overrides the first-line description
- **Async classes** should have a `connect()` classmethod that resolves browser tabs
- **Return dicts/lists**, not custom objects — the agent works with JSON-serializable data
