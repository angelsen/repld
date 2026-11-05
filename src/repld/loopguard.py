"""Loop ownership, enforced instead of remembered.

The kernel's loop mutates state that the sync half of the browser API reads
from IPC reader threads and `asyncio.to_thread` workers.  Two things here make
the rule structural:

  * `LoopOwned`: a dict whose every iteration is a snapshot and whose every
    mutation must come from the owning loop.
  * `loop_only`: the same check for methods writing state a dict cannot wrap
    (e.g. `CDPSession._event_count`).

Stdlib only and imports nothing from repld, so every browser module can use it.
"""

import asyncio
import functools
import logging
import os
import traceback
from collections.abc import (
    Callable,
    ItemsView,
    Iterator,
    KeysView,
    MutableMapping,
    ValuesView,
)
from typing import Any

logger = logging.getLogger(__name__)

# "warn" logs each offending call site once; "raise" is what the smoketest runs under.
_MODE = os.environ.get("REPLD_LOOP_GUARD", "warn")
_reported: set[tuple[str, int]] = set()


class LoopOwnershipError(RuntimeError):
    """Raised when loop-owned state is written from a foreign thread."""


class LoopOwned[K, V](MutableMapping[K, V]):
    """A mapping owned by one event loop.

    Ownership binds late, to the loop running the first mutation: these are
    constructed wherever `BrowserPool`/`BrowserSession` are, which need not be
    the loop.  Until then, and once the owner has closed, writes are unchecked.
    """

    # Wraps a dict rather than subclassing one: `dict.copy` on a subclass that
    # overrides `__iter__` falls back to a Python-level merge through `keys()`,
    # which is neither atomic nor, here, terminating.
    def __init__(self, name: str) -> None:
        """Bind a diagnostic *name* shown in violation messages."""
        self._name = name
        self._d: dict[K, V] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def _check(self) -> None:
        """Verify the caller is on the owning loop, or adopt one."""
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if self._loop is None or self._loop.is_closed():
            self._loop = running
            return
        if running is not self._loop:
            _violation(self._name)

    # -- reads (snapshot) ----

    # Every view is over `self._d.copy()`, one C call on an exact dict, so the
    # snapshot is atomic under the GIL.  The inherited views iterate live keys.
    def __iter__(self) -> Iterator[K]:
        """Iterate a snapshot of the keys."""
        return iter(self._d.copy())

    def keys(self) -> KeysView[K]:
        """Return a snapshot of the keys."""
        return self._d.copy().keys()

    def values(self) -> ValuesView[V]:
        """Return a snapshot of the values."""
        return self._d.copy().values()

    def items(self) -> ItemsView[K, V]:
        """Return a snapshot of the key-value pairs."""
        return self._d.copy().items()

    def __getitem__(self, key: K) -> V:
        """Look up *key* in the inner dict."""
        return self._d[key]

    def __len__(self) -> int:
        """Return the number of entries."""
        return len(self._d)

    def __contains__(self, key: object) -> bool:
        """Test membership without a snapshot."""
        return key in self._d

    def __repr__(self) -> str:
        """Show the name and current contents."""
        return f"LoopOwned({self._name}, {self._d!r})"

    # -- writes (ownership-checked) ----

    def __setitem__(self, key: K, value: V) -> None:
        """Store *key* after verifying loop ownership."""
        self._check()
        self._d[key] = value

    def __delitem__(self, key: K) -> None:
        """Remove *key* after verifying loop ownership."""
        self._check()
        del self._d[key]

    # The inherited `pop`/`clear` are get-then-delete loops; these stay one step.
    def pop(self, key: K, *default: Any) -> Any:  # type: ignore[override]
        """Pop *key* in one step, after verifying loop ownership."""
        self._check()
        return self._d.pop(key, *default)

    def clear(self) -> None:
        """Drop all entries after verifying loop ownership."""
        self._check()
        self._d.clear()


def _violation(what: str) -> None:
    """Log or raise when *what* is written off its owning loop."""
    if _MODE == "off":
        return
    if _MODE == "raise":
        raise LoopOwnershipError(f"{what} written off its owning loop")
    frame = next(
        f for f in reversed(traceback.extract_stack()) if f.filename != __file__
    )
    site = (frame.filename, frame.lineno or 0)
    if site in _reported:
        return
    _reported.add(site)
    logger.warning("%s written off its owning loop at %s:%d", what, *site)


def on_loop(loop: asyncio.AbstractEventLoop | None) -> bool:
    """Whether the calling thread is running *loop* right now."""
    if loop is None:
        return False
    try:
        return asyncio.get_running_loop() is loop
    except RuntimeError:
        return False


def loop_only[F: Callable[..., Any]](method: F) -> F:
    """Guard a method of an object carrying `_loop`; an unset `_loop` is unchecked."""

    @functools.wraps(method)
    def guarded(self: Any, *args: Any, **kwargs: Any) -> Any:
        loop = getattr(self, "_loop", None)
        if loop is not None and not loop.is_closed() and not on_loop(loop):
            _violation(f"{type(self).__name__}.{method.__name__}")
        return method(self, *args, **kwargs)

    return guarded  # type: ignore[return-value]
