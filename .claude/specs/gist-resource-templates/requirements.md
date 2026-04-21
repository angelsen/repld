# Feature: Gist Resource Templates

## Overview

Expose gist API surfaces as MCP resource templates so agents can discover what a gist does without importing it. The agent sees existence in INSTRUCTIONS (line 1 of docstring), then reads `repld://gists/{name}` for the full introspected API — classes, methods, signatures, docstrings — all parsed from AST without executing the file.

## What It Does

- `resources/templates/list` returns a template: `repld://gists/{name}` with description "Introspected API reference for a gist module."
- `resources/read` with URI `repld://gists/shopify_sd` returns:
  ```
  SD(tab)
    .synonyms() -> dict              List all synonym groups
    .create_synonym(terms: list[str]) -> dict  Create a synonym group
    .filters() -> dict               List all filter settings
    .boosts() -> dict                List product boost rules
    .settings() -> dict              Get search & discovery settings
  ```
- Introspection is pure AST — no import, no side effects. Parses classes, functions, type annotations, and one-line docstrings from the source file.
- For modules with top-level functions (no class), shows function signatures directly.
- For modules with a class, shows `ClassName(init_args)` then `.method(args) -> return_type  docstring_line_1`.
- Private methods (starting with `_`) are excluded.

## Constraints

- Stdlib only (`ast` module). No new dependencies.
- AST parsing happens on each `resources/read` — no caching needed (files are small).
- Must handle both `~/.repld/gists/` and `./gists/` directories (same as `scan()`).
- If the gist file doesn't exist, return an MCP error.

## Out of Scope

- Completion API for template parameters (agent just constructs the URI).
- Nested package introspection (only top-level `.py` files).
- Runtime type info from imported modules (AST only).
