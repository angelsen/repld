# Implementation Tasks: Gist Resource Templates

**Status:** Complete
**Spec:** [requirements.md](./requirements.md) | [design.md](./design.md)

## Task Breakdown

### Task 1: gists.py — introspect() + helpers
**Completed**

**Acceptance:**
- [x] `introspect("shopify_sd")` returns formatted API with class, methods, signatures, docstrings
- [x] `introspect("nonexistent")` raises FileNotFoundError
- [x] Top-level functions (no class) are formatted directly
- [x] Private methods excluded, `__init__` shown as constructor args
- [x] Type annotations rendered via `ast.unparse`

**Notes:** Added top-level `import ast` to gists.py (design had deferred per-function imports; top-level is cleaner and ruff-clean).

---

### Task 2: protocol.py — resource templates endpoint + gist URI routing
**Completed**

**Acceptance:**
- [x] `resources/templates/list` returns `{resourceTemplates: [{uriTemplate: "repld://gists/{name}", ...}]}`
- [x] `resources/read` with `uri: "repld://gists/shopify_sd"` returns introspected text
- [x] Unknown gist name returns MCP error
- [x] Existing static resources (`repld://browser/*`) still work

---

### Task 3: Verify end-to-end
**Completed**

**Acceptance:**
- [x] `ruff check + format` clean
- [x] `basedpyright` 0 errors
- [x] `smoketest --phase 8` passes (new phase 8 added for gist resource templates)
- [x] Live smoketest confirmed: `resources/read` on `repld://gists/test_api` returns formatted API

---

## Task Dependencies

```
Task 1 → Task 2 → Task 3
```

## Parallel Tracks

None — sequential. Task 2 depends on `introspect()` from Task 1.
