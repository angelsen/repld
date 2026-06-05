from __future__ import annotations

import importlib.metadata
from typing import TYPE_CHECKING

from .cli import main

__version__ = importlib.metadata.version("repld-tool")

if TYPE_CHECKING:
    from typing import Any, Callable, Coroutine

    def notify(content: Any, **meta: Any) -> None: ...
    def defer(coro: Coroutine, label: str | None = None) -> str: ...
    def every(seconds: float, *, label: str | None = None) -> Callable: ...
    async def ask(
        prompt: str,
        *,
        default: str | None = None,
        timeout: float | None = None,
    ) -> str: ...
    async def confirm(
        prompt: str,
        *,
        default: bool | None = None,
        timeout: float | None = None,
    ) -> bool: ...
    async def choose(
        prompt: str,
        options: list[str],
        *,
        default: str | None = None,
        timeout: float | None = None,
    ) -> str: ...

__all__ = ["main"]
