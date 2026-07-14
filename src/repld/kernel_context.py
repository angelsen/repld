"""KernelContext — the interface Dispatcher (and its mixins) need from the
kernel: task lifecycle + the shared asyncio loop.

Its own module because both protocol.py's Dispatcher and
browser_dispatch.py's BrowserDispatchMixin depend on it; living inside
protocol.py made browser_dispatch.py import back into the module that
imports it.
"""

import asyncio
import threading
from typing import Protocol


class KernelContext(Protocol):
    loop: asyncio.AbstractEventLoop

    def start_task(self, src: str) -> tuple[str, threading.Event]: ...
    def snapshot(self, task_id: str) -> dict | None: ...
    def mark_nudged(self, task_id: str) -> None: ...
    def cancel_task(self, task_id: str) -> bool: ...
