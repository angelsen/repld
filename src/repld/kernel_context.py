"""Protocol the Dispatcher (and its mixins) need from the kernel.

Task lifecycle plus the shared asyncio loop, in its own module because both
`protocol.py` and `browser_dispatch.py` depend on it: living inside
`protocol.py` made `browser_dispatch.py` import back into the module that
imports it.
"""

import asyncio
import threading
from collections.abc import Callable
from typing import Protocol


class KernelContext(Protocol):
    """Kernel surface that the IPC dispatcher calls from its reader threads."""

    loop: asyncio.AbstractEventLoop

    def start_task(
        self, src: str, *, origin: object = None
    ) -> tuple[str, threading.Event]:
        """Create a tracked task for `src` and schedule it on the loop."""
        ...

    def start_peer_call(
        self,
        target: str,
        func: Callable,
        args: list,
        kwargs: dict,
        caller: dict | None,
        *,
        origin: object = None,
    ) -> tuple[str, threading.Event]:
        """Schedule a resolved `peer/call` target as a tracked task."""
        ...

    def wait_ready(self) -> None:
        """Block until the project bootstrap has run, or its budget expires."""
        ...

    def snapshot(self, task_id: str) -> dict | None:
        """Return a JSON-safe snapshot of the given task, or `None` if unknown."""
        ...

    def mark_nudged(self, task_id: str) -> bool:
        """Promise a completion push for `task_id`; `False` if already done."""
        ...

    def cancel_task(self, task_id: str) -> bool:
        """Request cancellation of a running cell; `True` if scheduled."""
        ...
