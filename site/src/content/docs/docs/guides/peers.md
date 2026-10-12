---
title: Peers guide
description: Call another project's kernel as an awaitable service, instead of installing its dependencies.
---

A kernel is a long-lived process with its own interpreter, venv, dependencies and warm state: an open model, a logged-in session, a connection pool. Peers let one kernel call into another as an awaitable object, so the first does not have to install what the second already has.

The function runs in the other kernel. Only the arguments and a JSON result cross.

```python
peer = peers.get("voice-api")                        # a directory name, or a path
text = await peer.call("voice.transcribe", "/tmp/a.wav", lang="en")
```

The client lives inside the calling kernel, not the MCP bridge, so cells, gists, `@every` tickers and `defer()` tasks can all use it.

## Why not `repld gist add`

`repld gist add` links another project's gist and runs it in *your* kernel, so your kernel needs its dependencies. A peer call leaves the code where it is and sends the call there. Use `gist add` for small helpers you want to own; use peers when the other project has the heavy dependencies, the warm state, or a different Python.

## Finding a peer

```python
peers.list()                            # live kernels: name, cwd, pid, socket_path, started_at, self
peer = peers.get("voice-api")           # basename of exactly one live kernel's directory
peer = peers.get("/home/me/voice-api")  # or a path (it need not be running)
```

`peers.get` is synchronous and cheap; nothing connects until the first `await`. A name that matches no live kernel raises `PeerNotFound` and lists the ones that are running. A name that matches two raises `PeerNotFound` listing both paths; pass a path.

## Calling

```python
await peer.call("voice.transcribe", "/tmp/a.wav", lang="en")  # a gist function
await peer.call("os.path.join", "a", "b")                     # any importable name
await peer.call("warm_model")                                 # something in its __main__
```

The target is any dotted name in the other kernel. The first segment is a name in its `__main__` if it has one, otherwise a module imported there (gists through the same auto-reloading import a cell uses). Later segments are attribute lookups, and the result must be callable. There is no export list: reach is exactly what `exec` has in that kernel.

Async functions run on the other kernel's loop. Everything else runs in a worker thread, so a blocking function does not stall that kernel. Each call is a tracked task over there, with a task id, spilled output, and an entry in `repld log` and `repld tasks`.

## Values and bulk data

Arguments and results are JSON. A value that cannot be serialized raises `TypeError` before anything is sent; a result that cannot be serialized, or is over 8 MiB, comes back as a `PeerCallError`.

Bulk data goes by file path. Both kernels run as the same user on the same machine, so write the data to a file and pass the path, or have the callee write its result and return the path. `$XDG_RUNTIME_DIR` is usually RAM-backed, so put multi-gigabyte files on disk.

## Errors tell you whether it ran

```python
try:
    await peer.call("voice.transcribe", "/tmp/a.wav")
except peers.PeerRefused as e:        # never ran: e.reason is unresolved, bad_args, cycle, depth or self
    ...
except peers.PeerUnavailable:         # never ran: no kernel there, or it predates peers
    ...
except peers.PeerCallError as e:      # ran and raised
    print(e.remote_type, e.remote_message, e.spill_path)
except peers.PeerDisconnected as e:   # unknown whether it ran; e.clean says if the peer said goodbye
    ...
```

A call is never retried once its request is written, so a disconnect leaves the decision to you. `PeerCallError` carries the remote traceback, a 4 KB preview of what the target printed, and the path of its full output.

## Async

Each call opens a connection of its own, so calls are independent:

```python
a, b = await asyncio.gather(peer.call("svc.slow", 1), peer.call("svc.slow", 1))
```

runs both at once. For a time limit, use the caller's own:

```python
async with asyncio.timeout(30):
    await peer.call("svc.long_job")
```

Cancelling the awaiting task, by timeout, `task.cancel()`, or the MCP `cancel` of a deferred cell, closes the connection; the other kernel notices and cancels the target at its next `await`. A threaded (sync) target cannot be interrupted, so its thread runs to completion and the result is discarded.

## Who asked

Every call carries its caller: the calling kernel, the Claude Code session that started the chain, the calling task, and the full chain of kernels. Inside a function called this way:

```python
import repld

caller = repld.peer_caller()      # None unless another kernel called this one
```

The other kernel records it. `repld log` shows `# peer call from <project> (pid 4242 verified, session abc12345, task 9f3e...)`, `repld tasks` shows `from=peer:<project>/<session>`, and `get_task` returns it as `caller`. A call made from inside a peer call keeps the original session and extends the chain.

Only the kernel pid is verified, against the connecting process. Everything else is a claim by a same-user process: it is for attribution, not authorization.

## Cycles

A call whose chain already contains the target kernel is refused with `PeerRefused` (`reason="cycle"`), as is a call to your own kernel (`"self"`) and a chain eight kernels deep (`"depth"`).

## A peer that is not running

A stopped peer is a `PeerUnavailable` whose message says `repld --project DIR start`. Starting someone else's kernel is a decision, so a call does not do it unasked. To opt in:

```python
peer = peers.get("/home/me/voice-api", start=True)
```

The first call then starts that project's kernel with `python -m repld --project DIR start`, so it binds to its own `./.venv`. The environment is cleaned first: `REPLD_BOUND`, `REPLD_SOCKET`, `VIRTUAL_ENV`, `PYTHONPATH` and every variable this kernel loaded from its own `.env` are dropped, so the new kernel does not run in the wrong venv or inherit this project's secrets.

## Typed shims

Calling by string works, but gives no signatures. Generate a local gist whose functions forward to the peer:

```bash
repld gist stub voice-api voice                    # all public functions of its voice gist
repld gist stub voice-api voice.transcribe,detect  # just these
repld gist stub voice-api voice --as vo --start    # name it; its calls start a stopped peer
```

```python
import voice

text = await voice.transcribe("/tmp/a.wav", lang="en")
```

The stub is read from the peer's AST signatures and keeps parameter kinds (positional-only, keyword-only, `*args`, `**kw`), annotations and literal defaults. A default that is not a literal cannot be copied, so the stub leaves that argument out of the call and the peer applies its own. Only public functions are stubbed; classes cannot cross as JSON.

Stubs are generated: regenerate, do not edit. To see which have drifted:

```bash
repld gist stub --check
```

It names the functions whose signature changed or vanished, and exits 1 if any stub is stale. A peer that is not running is reported as `unchecked`, never `ok`. Regenerate with `repld gist stub ... --force`, which only ever overwrites an existing stub, never a hand-written gist.

## Security

Any process of the same user can call any dotted name in a kernel. That is exactly `exec`'s reach: the kernel's unix socket was already that door, and peers are a typed way through it. Caller identity is a claim. There is no TCP surface and no remote access.

## Not covered

There is no cross-kernel event subscription; a peer cannot be told when something happens in another kernel. Poll with `@every`, or have the callee `notify()` its own sessions. Live objects do not cross either: return the data, or a path to it.

See the [reference](/repld/docs/reference/peers/) for every signature.
