# Design: Gist Resource Templates

## Architecture Overview

Two changes:
1. `gists.py` gains an `introspect(name)` function that AST-parses a gist file and returns a formatted API summary string.
2. `protocol.py` handles `resources/templates/list` and routes `repld://gists/{name}` URIs in `_read_resource`.

```
Agent → resources/templates/list
         ↓
protocol.py returns [{uriTemplate: "repld://gists/{name}", ...}]
         ↓
Agent → resources/read(uri="repld://gists/shopify_sd")
         ↓
protocol.py._read_resource extracts "shopify_sd" from URI
         ↓
gists.introspect("shopify_sd") → AST parse → formatted string
         ↓
Returns {contents: [{uri, mimeType: "text/plain", text: ...}]}
```

## Component Changes

### `src/repld/gists.py` — add `introspect(name: str) -> str`

```python
def introspect(name: str) -> str:
    """AST-introspect a gist module. Returns formatted API summary."""
    import ast

    # Find the file (same resolution as scan)
    path = _find_gist(name)
    if path is None:
        raise FileNotFoundError(f"No gist '{name}' in {_installed_dirs}")

    tree = ast.parse(path.read_text("utf-8"))
    lines: list[str] = []

    # Module docstring
    mod_doc = ast.get_docstring(tree)
    if mod_doc:
        lines.append(mod_doc.split("\n")[0].strip())
        lines.append("")

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            _format_class(node, lines)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            _format_function(node, lines, indent="")

    return "\n".join(lines)


def _find_gist(name: str) -> Path | None:
    """Resolve gist name to file path."""
    for d in _installed_dirs:
        p = d / f"{name}.py"
        if p.is_file():
            return p
    return None


def _format_class(node: ast.ClassDef, lines: list[str]) -> None:
    """Format a class: ClassName(init_args) + public methods."""
    import ast as _ast

    # Find __init__ for constructor signature
    init_args = ""
    for item in node.body:
        if isinstance(item, ast.FunctionDef) and item.name == "__init__":
            init_args = _format_args(item.args, skip_self=True)
            break

    lines.append(f"{node.name}({init_args})")

    # Class docstring
    cls_doc = _ast.get_docstring(node)
    if cls_doc:
        lines.append(f"  {cls_doc.split(chr(10))[0].strip()}")
        lines.append("")

    # Public methods
    for item in node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if item.name.startswith("_"):
                continue
            _format_function(item, lines, indent="  ", is_method=True)


def _format_function(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    lines: list[str],
    indent: str = "",
    is_method: bool = False,
) -> None:
    """Format one function/method line."""
    import ast as _ast

    prefix = "." if is_method else ""
    args = _format_args(node.args, skip_self=is_method)
    ret = ""
    if node.returns:
        ret = f" -> {_ast.unparse(node.returns)}"

    sig = f"{indent}{prefix}{node.name}({args}){ret}"

    # One-line docstring
    doc = _ast.get_docstring(node)
    if doc:
        first_line = doc.split("\n")[0].strip()
        sig += f"  # {first_line}"

    lines.append(sig)


def _format_args(args: ast.arguments, skip_self: bool = False) -> str:
    """Format function arguments as compact string."""
    import ast as _ast

    parts: list[str] = []
    all_args = args.args[1:] if skip_self else args.args

    for arg in all_args:
        s = arg.arg
        if arg.annotation:
            s += f": {_ast.unparse(arg.annotation)}"
        parts.append(s)

    # keyword-only args
    for arg in args.kwonlyargs:
        s = arg.arg
        if arg.annotation:
            s += f": {_ast.unparse(arg.annotation)}"
        s += "="
        parts.append(s)

    return ", ".join(parts)
```

### `src/repld/protocol.py` — handle templates/list + gist URI routing

```python
RESOURCE_TEMPLATES = [
    {
        "uriTemplate": "repld://gists/{name}",
        "name": "gist-api",
        "description": "Introspected API reference for a gist module (classes, methods, signatures).",
        "mimeType": "text/plain",
    },
]
```

In `handle()`:
```python
if method == "resources/templates/list":
    return {"jsonrpc": "2.0", "id": rid, "result": {"resourceTemplates": RESOURCE_TEMPLATES}}
```

In `_read_resource()`, add before the "unknown resource" fallback:
```python
if uri.startswith("repld://gists/"):
    name = uri.removeprefix("repld://gists/")
    text = self._resource_gist(name)
```

New method:
```python
def _resource_gist(self, name: str) -> str:
    from . import gists
    return gists.introspect(name)
```

## Method Signatures

```python
# gists.py
def introspect(name: str) -> str: ...
def _find_gist(name: str) -> Path | None: ...
def _format_class(node: ast.ClassDef, lines: list[str]) -> None: ...
def _format_function(node, lines, indent="", is_method=False) -> None: ...
def _format_args(args: ast.arguments, skip_self=False) -> str: ...

# protocol.py
RESOURCE_TEMPLATES: list[dict]  # new module-level list
# _read_resource gains a new URI prefix case
# handle() gains resources/templates/list dispatch
```
