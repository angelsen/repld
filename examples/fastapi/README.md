# FastAPI example

Live-poke a FastAPI app from a Claude Code session. The kernel runs both the
HTTP server and the REPL; the agent reaches into `__main__` for `app`,
`state`, and `server`.

## Setup

From the repo root:

```bash
uv sync --extra web
```

## Run

```bash
cd examples/fastapi
uv run repld --init repl.py
```

Wire the MCP server (once per project, here `examples/fastapi/`):

```bash
claude mcp add -s project repld -- repld bridge
```

## Things to try from the agent

```python
# Routes
[r.path for r in app.routes]

# Hit it
import httpx
httpx.get("http://127.0.0.1:8000/").json()
httpx.post("http://127.0.0.1:8000/incr").json()
httpx.post("http://127.0.0.1:8000/messages?text=hi").json()

# Peek state
state

# Add a route at runtime — no restart
@app.get("/added-at-runtime")
def runtime_route():
    return {"added": "live"}

httpx.get("http://127.0.0.1:8000/added-at-runtime").json()
```

The server runs on the kernel's shared asyncio loop — no separate process.
