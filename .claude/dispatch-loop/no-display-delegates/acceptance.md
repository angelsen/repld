# no-display-delegates: a no_display() result answers as its value

The master pane's item B for pane %22 (2026-10-23). Base: `3ab3819c`. No release, no version
bump, no `uv publish`, no `make deploy`.

## Why

`no_display()`'s docstring (`src/repld/runtime.py:/while still returning the/`) promises "the real
value for programmatic use", but `_NoDisplay` delegates nothing: only `run_cell()` (its
`unwrap_display`/`_unwrap_assigned` paths) and `defer()` unwrap it. A caller of a gist function
that ends `return no_display(text)` (the cairn's `gists/vps.py` `psql()`) gets the wrapper:
`str(r)` is `<repld.runtime._NoDisplay object at 0x...>`, `r == text` is False.

## What changes

1. `_NoDisplay` in `src/repld/runtime.py` delegates `__str__`, `__repr__` and `__eq__` to the
   wrapped `value`, and `__hash__` too (an `__eq__` without it makes the wrapper unhashable).
   `__format__` follows `__str__` only if an f-string of the wrapper needs it to; the oracle checks.
2. `_NoDisplay`'s own docstring says what the wrapper answers as its value, replacing "never meant
   to be instantiated or inspected elsewhere" only where it is now untrue.
3. Nothing else: `unwrap_display`, `_unwrap_assigned`, the kernel's display and `defer()` paths
   keep detecting the wrapper by `isinstance`, unchanged.

## Oracle (the worktree's first commit, with this file, before the implementation)

`tests/phases/pure.py`'s `_no_display_delegates()`, run from `phase_2_pure()`: a function
returning `runtime.no_display("count\n-----\n3")` gives its caller a value whose `str()`,
f-string and `repr()` equal the string's, `==` the string, `!=` it False; `no_display(3) == 3`,
`!= 4`; `unwrap_display` still returns `(value, True)`.

Reading on the base, by the dispatcher: `uv run python tests/smoketest.py --phase 2` exits 1 at
`FAIL: str() of a no_display() result: expected 'count\n-----\n3', got
'<repld.runtime._NoDisplay object at 0x...>'`; `str`, `==`, `repr` and `no_display(3) == 3` are
each False on the base.

**The floor**: `tests/phases/core.py`'s `no_display()` check (suppressed on display, bound to
`_`), `tests/phases/defer.py`'s, `tests/phases/headless.py`'s `_no_display_skips_queue`, and every
phase through 19.

## Decisions this round makes (Fredrik's to reverse)

- The lean is taken: the wrapper matches its docstring, rather than the docstring shrinking to
  "unwrapped only by a cell or `defer()`".
- `__hash__` is delegated with `__eq__`; no `__getattr__` or arithmetic delegation (the docstring
  promises the value's identity in text and equality, not a full proxy).

## Mechanics

- The gate is `uv run python tests/smoketest.py --phase 19`; it starts its own kernel, never the
  shared one. Phase 6 may use a throwaway headless Chrome.
- The oracle's assertions are not yours to change. No em dashes in code or strings.
- The oracle and this file are staged, uncommitted: commit them first, alone and unchanged, with
  the message your prompt gives. Then the fix in a second commit, message in the repo's style, with the attribution
  trailer your own reminder names. Never `git push`, never release.
- Permission to fail: an honest "phase N went red, here is why" beats a plausible pass.
