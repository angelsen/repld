"""Tiny FastAPI app for the repld example.

Nothing fancy — a counter, a message list, and one async route. The point is
to have something stateful so the agent can inspect / mutate live from
`exec` calls.
"""

from fastapi import FastAPI

app = FastAPI(title="repld example")

state = {"counter": 0, "messages": []}


@app.get("/")
def hello():
    return {"hello": "from repld example", "counter": state["counter"]}


@app.post("/incr")
def incr():
    state["counter"] += 1
    return {"counter": state["counter"]}


@app.get("/messages")
def list_messages():
    return state["messages"]


@app.post("/messages")
def add_message(text: str):
    state["messages"].append(text)
    return {"count": len(state["messages"])}
