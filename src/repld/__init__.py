"""Public surface of the `repld` package: re-exports, TYPE_CHECKING stubs for builtins, and lazy accessors.

`Image`, `tool`, `main` and `NotInKernelError` are imported eagerly (stdlib cost only).
Everything else is resolved lazily through `__getattr__` so the bridge
import stays cheap.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .cli import main
from .content import Image, tool
from .kernel_only import _KERNEL_ONLY, NotInKernelError

if TYPE_CHECKING:
    __version__: str
    from collections.abc import Callable, Coroutine
    from pathlib import Path
    from typing import Any

    def notify(content: Any, **meta: Any) -> None:
        """Push a channel notification to connected Claude Code sessions."""
        ...

    def defer(
        coro: Coroutine | Callable[[], Coroutine], label: str | None = None
    ) -> str:
        """Schedule `coro` as a background task and return its task id."""
        ...

    def every(
        seconds: float, *, label: str | None = None, delay: float = 0.0
    ) -> Callable:
        """Register a periodic ticker that fires every `seconds`."""
        ...

    def load_dotenv() -> None:
        """Read `.env` into `os.environ` without overriding existing vars."""
        ...

    def socket_path() -> Path:
        """Return the unix socket path for the kernel bound to this project."""
        ...

    async def ask(
        prompt: str,
        *,
        tab: Any = None,
        default: str | None = None,
        timeout: float | None = None,
    ) -> str:
        """Block the cell on a free-text human answer to `prompt`."""
        ...

    async def confirm(
        prompt: str,
        *,
        tab: Any = None,
        default: bool | None = None,
        timeout: float | None = None,
    ) -> bool:
        """Block the cell on a yes/no human answer to `prompt`."""
        ...

    async def choose(
        prompt: str,
        options: list[str],
        *,
        tab: Any = None,
        default: str | None = None,
        timeout: float | None = None,
    ) -> str:
        """Block the cell on a human pick from `options`."""
        ...

    def no_display(value: Any) -> Any:
        """Return `value` without printing it (still binds `_`/`_N`)."""
        ...

    from .browser import LazyBrowser as _LazyBrowser

    browser: _LazyBrowser


__all__ = ["Image", "load_dotenv", "main", "socket_path", "tool"]


def __getattr__(name: str) -> object:
    """Resolve `load_dotenv`, `socket_path` and `__version__` lazily, off the bridge's import; raise `NotInKernelError` for a kernel-only builtin.

    `repld/__init__.py` is imported by every `repld bridge`, which is spawned
    once per Claude Code session and deliberately stays cheap; pulling in
    `kernel` would drag protocol/tasks/gists along for a function only kernel-
    side code calls. Inside a kernel the module is already imported, so this
    costs nothing there.

    PEP 562 only fires for names not found normally, so the builtins that
    `_inject_builtins` setattr's onto this module still take precedence.
    """
    if name == "__version__":
        # `importlib.metadata` is ~24 ms, half the bridge's import; only the
        # discovery paths (cache check, static fallback) and `initialize`
        # authors read it, and an attached bridge reads it never.
        import importlib.metadata

        version = importlib.metadata.version("repld-tool")
        globals()["__version__"] = version
        return version
    if name == "load_dotenv":
        from .kernel import load_dotenv

        return load_dotenv
    if name == "socket_path":
        from .ipc import socket_path

        return socket_path
    if name in _KERNEL_ONLY:
        raise NotInKernelError(
            f"repld.{name} is injected by a running repld kernel and is not available here; "
            "in a test use `repld.testing` (the `repld_standin` fixture)"
        )
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
