"""repld init: boot the FastAPI app on the kernel's shared loop.

    uv run repld --init examples/fastapi/repl.py   # from project root
    uv run repld --init repl.py                    # from examples/fastapi/

Leaves `app`, `state`, and `server` in `__main__` so the agent can introspect
routes, peek/mutate state, and patch handlers live.
"""

import asyncio
import sys
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).parent))
from app import app, state  # noqa: E402, F401

config = uvicorn.Config(
    app,
    host="127.0.0.1",
    port=8000,
    log_level="warning",
    loop="asyncio",
)
server = uvicorn.Server(config)
# uvicorn's SIGINT handler would fight the kernel's — drop it by replacing
# the bound method with a no-op. Pyright flags the assignment because it sees
# install_signal_handlers as a method, not an attribute; this is intentional.
server.install_signal_handlers = lambda: None  # pyright: ignore[reportAttributeAccessIssue]

asyncio.create_task(server.serve())

print("FastAPI on http://127.0.0.1:8000  (app, state, server in __main__)")
