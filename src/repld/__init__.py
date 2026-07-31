from __future__ import annotations

import importlib.metadata
from typing import TYPE_CHECKING

from .cli import main

__version__ = importlib.metadata.version("repld-tool")

if TYPE_CHECKING:
    from typing import Any, Callable, Coroutine

    def notify(content: Any, **meta: Any) -> None: ...
    def defer(coro: Coroutine, label: str | None = None) -> str: ...
    def every(
        seconds: float, *, label: str | None = None, delay: float = 0.0
    ) -> Callable: ...
    def load_dotenv() -> None: ...
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


__all__ = ["main", "load_dotenv"]


def __getattr__(name: str):
    """Resolve `load_dotenv` without importing the kernel at package import.

    `repld/__init__.py` is imported by every `repld bridge`, which is spawned
    once per Claude Code session and deliberately stays cheap — pulling in
    `kernel` would drag protocol/tasks/gists along for a function only kernel-
    side code calls. Inside a kernel the module is already imported, so this
    costs nothing there.

    PEP 562 only fires for names not found normally, so the builtins that
    `_inject_builtins` setattr's onto this module still take precedence.
    """
    if name == "load_dotenv":
        from .kernel import load_dotenv

        return load_dotenv
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
