from __future__ import annotations

from typing import TYPE_CHECKING

from .cli import main
from .content import Image, tool

if TYPE_CHECKING:
    __version__: str
    from collections.abc import Callable, Coroutine
    from pathlib import Path
    from typing import Any

    def notify(content: Any, **meta: Any) -> None: ...
    def defer(
        coro: Coroutine | Callable[[], Coroutine], label: str | None = None
    ) -> str: ...
    def every(
        seconds: float, *, label: str | None = None, delay: float = 0.0
    ) -> Callable: ...
    def load_dotenv() -> None: ...
    def socket_path() -> Path: ...
    async def ask(
        prompt: str,
        *,
        tab: Any = None,
        default: str | None = None,
        timeout: float | None = None,
    ) -> str: ...
    async def confirm(
        prompt: str,
        *,
        tab: Any = None,
        default: bool | None = None,
        timeout: float | None = None,
    ) -> bool: ...
    async def choose(
        prompt: str,
        options: list[str],
        *,
        tab: Any = None,
        default: str | None = None,
        timeout: float | None = None,
    ) -> str: ...
    def no_display(value: Any) -> Any: ...

    from .browser import LazyBrowser as _LazyBrowser

    browser: _LazyBrowser


__all__ = ["Image", "load_dotenv", "main", "socket_path", "tool"]


def __getattr__(name: str):
    """Resolve `load_dotenv` and `__version__` lazily, off the bridge's import.

    `repld/__init__.py` is imported by every `repld bridge`, which is spawned
    once per Claude Code session and deliberately stays cheap — pulling in
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
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
