"""A stand-in for the kernel's `repld` module, so a gist's wiring is testable in a plain pytest venv.

`StandIn` carries the gate and push builtins (`ask`, `confirm`, `choose`,
`form`, `notify`, `defer`, `every`, `no_display`) with the kernel's
signatures: gates answer from queues and record their prompts, the rest record
their calls. `peers`, `peer_caller`, `browser` and the session helpers are not faked; assign
your own (`standin.peers = ...`) or the read raises `NotInKernelError`.
`installed()` and the `repld_standin` fixture put one in `sys.modules["repld"]`;
test pure logic by importing it directly, and only the wiring against this.
Under pytest the fixture comes with an installed `repld-tool` (a `pytest11`
entry point); with `PYTEST_DISABLE_PLUGIN_AUTOLOAD` set, name `repld.testing` in `pytest_plugins`.
"""

from __future__ import annotations

import inspect
import re
import sys
import types
from collections.abc import Callable, Coroutine, Iterable, Iterator
from contextlib import contextmanager
from typing import Any

from .content import Image, tool
from .gates import GateDeclined

__all__ = ["StandIn", "installed"]


class StandIn(types.ModuleType):
    """A `repld` module whose builtins answer from queues and record every call.

    A gate with its queue empty answers its `default`, and without one is
    declined: `confirm` returns False, the others raise `GateDeclined`. The
    queues are plain lists; assign or extend them between calls.
    """

    Image = Image
    GateDeclined = GateDeclined
    tool = staticmethod(tool)

    def __init__(
        self,
        *,
        confirms: Iterable[bool] = (),
        asks: Iterable[str] = (),
        choices: Iterable[str] = (),
        forms: Iterable[dict] = (),
    ) -> None:
        """Queue the answers `confirm`, `ask`, `choose` and `form` give in order."""
        super().__init__("repld")
        self.confirms, self.asks_queue = list(confirms), list(asks)
        self.choices, self.forms = list(choices), list(forms)
        self.asked: list[str] = []
        self.notified: list[tuple[Any, dict[str, Any]]] = []
        self.deferred: list[str | None] = []
        self.tickers: list[tuple[float, dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        """Name the builtin this stand-in does not fake, instead of a bare AttributeError."""
        from .kernel_only import _KERNEL_ONLY, NotInKernelError

        if name in _KERNEL_ONLY:
            raise NotInKernelError(
                f"the repld stand-in does not fake {name}; assign your own: standin.{name} = ..."
            )
        raise AttributeError(f"module 'repld' has no attribute {name!r}")

    def notify(
        self,
        content: Any,
        *,
        session: str | None = None,
        exclude: str | None = None,
        **meta: Any,
    ) -> bool | None:
        """Record a push as `(content, meta)`; `session` and `exclude` land in meta when given."""
        if bad := [k for k in meta if not re.fullmatch(r"[A-Za-z0-9_]+", k)]:
            raise ValueError(
                f"notify meta keys must be letters, digits and underscores: {bad}"
            )
        extra = {"session": session, "exclude": exclude}
        self.notified.append(
            (content, {**meta, **{k: v for k, v in extra.items() if v is not None}})
        )
        return True if session is not None else None

    def defer(
        self, coro: Coroutine | Callable[[], Coroutine], label: str | None = None
    ) -> str:
        """Record the label without running anything, refusing what the kernel's `defer` refuses.

        A coroutine is closed unawaited; a zero-arg callable is recorded and never called.
        """
        if inspect.iscoroutinefunction(coro):
            raise TypeError(
                f"defer() expects a coroutine object, got {coro.__name__!r}"
            )
        if not inspect.iscoroutine(coro):
            if not callable(coro):
                raise TypeError(
                    f"defer() expects a coroutine, got {type(coro).__name__}"
                )
            self.deferred.append(label)
            return f"task-{len(self.deferred)}"
        coro.close()
        self.deferred.append(label)
        return f"task-{len(self.deferred)}"

    def every(
        self,
        seconds: float,
        *,
        label: str | None = None,
        delay: float = 0.0,
        tab: str | None = None,
    ) -> Callable:
        """Record the ticker and return the decorated function unregistered."""
        self.tickers.append((seconds, {"label": label, "delay": delay, "tab": tab}))
        return lambda fn: fn

    def no_display(self, value: Any) -> Any:
        """Return `value` unchanged."""
        return value

    async def ask(
        self,
        prompt: str,
        *,
        tab: Any = None,
        default: str | None = None,
        timeout: float | None = None,
        secret: bool = False,
    ) -> str:
        """Record `prompt` and answer the next queued string."""
        self.asked.append(prompt)
        return self._answer(self.asks_queue, default)

    async def confirm(
        self,
        prompt: str,
        *,
        tab: Any = None,
        default: bool | None = None,
        timeout: float | None = None,
        on_open: Callable[[str], Any] | None = None,
    ) -> bool:
        """Record `prompt` and answer the next queued bool; declined reads False."""
        self.asked.append(prompt)
        try:
            return self._answer(self.confirms, default)
        except GateDeclined:
            return False

    async def choose(
        self,
        prompt: str,
        options: list[str],
        *,
        tab: Any = None,
        default: str | None = None,
        timeout: float | None = None,
    ) -> str:
        """Record `prompt` and answer the next queued option."""
        self.asked.append(prompt)
        return self._answer(self.choices, default)

    async def form(
        self,
        prompt: str,
        fields: dict,
        *,
        required: list[str] | None = None,
        timeout: float | None = None,
    ) -> dict:
        """Record `prompt` and answer the next queued dict."""
        self.asked.append(prompt)
        return self._answer(self.forms, None)

    @staticmethod
    def _answer[T](queue: list[T], default: T | None) -> T:
        """Pop the next answer, else `default`, else decline."""
        if queue:
            return queue.pop(0)
        if default is None:
            raise GateDeclined("no answer queued")
        return default


@contextmanager
def installed(**answers: Any) -> Iterator[StandIn]:
    """Make `import repld` yield a `StandIn` inside the block, and forget modules bound to it on exit.

    Import the gist under test inside the block. On exit every module whose
    globals hold the stand-in or one of its bound methods (`import repld`,
    `from repld import notify`) and that was first imported inside the block leaves `sys.modules`, so the next block
    imports it afresh; a module that stored one deeper is not found.
    """
    standin = StandIn(**answers)
    saved = sys.modules.get("repld")
    before = set(sys.modules)
    sys.modules["repld"] = standin
    try:
        yield standin
    finally:
        if saved is None:
            del sys.modules["repld"]
        else:
            sys.modules["repld"] = saved
        for name in set(sys.modules) - before:
            mod = sys.modules[name]
            if isinstance(mod, types.ModuleType) and _is_bound_to(mod, standin):
                del sys.modules[name]


def _is_bound_to(mod: types.ModuleType, standin: StandIn) -> bool:
    """Whether `mod`'s globals hold `standin` or a method of it."""
    return any(
        v is standin or (type(v) is types.MethodType and v.__self__ is standin)
        for v in vars(mod).values()
    )


try:
    import pytest    # pyright: ignore[reportMissingImports]
except ImportError:  # the module stays usable without pytest
    pass
else:
    __all__.append("repld_standin")

    @pytest.fixture
    def repld_standin() -> Iterator[StandIn]:
        """A `StandIn` installed as `repld`; set its `confirms`, `asks_queue`, `choices` and `forms` lists to queue answers."""
        with installed() as standin:
            yield standin
