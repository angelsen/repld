---
title: Peers API
description: Every signature of the peers builtin, the Peer handle, its errors, and the gist stub commands.
---

## Finding peers

```python
peers.list()                          # [{name, cwd, pid, socket_path, started_at, self}]
peer = peers.get(target)              # a path, or the basename of one live kernel
peer = peers.get(target, start=True)  # a call may start it if it is not running
```

`peers.get` is synchronous. `target` is a path (it contains a `/` or starts with `.` or `~`) or a bare name that matches exactly one live kernel's directory. The handle is cached per project directory.

## The handle

```python
peer.cwd    # the resolved project directory
peer.name   # its basename
peer.start  # whether a call may start it

await peer.call(target, /, *args, **kwargs)  # the JSON value the target returned
await peer.info()                            # identity and callable surface
await peer.inspect(target)                   # what a name resolves to; runs nothing
```

`target` is positional-only, so a remote parameter may itself be called `target`. There is no `timeout=`: use `asyncio.timeout()`.

`info()` returns `{peer_protocol, kernel, gists, main}`: `kernel` has `cwd`, `pid`, `socket`, `started_at`, `python` and `venv`; `gists` lists each public gist with its functions' signatures; `main` lists callables defined in its `__main__`.

`inspect()` returns `{target, kind, signature, async, doc, module, file}`.

## Errors

All derive from `PeerError` and are also attributes of `peers`.

| Error | Meaning | Ran? |
|---|---|---|
| `PeerNotFound(candidates)` | A name that matches no live kernel, or several | no |
| `PeerUnavailable` | No kernel there, or it predates peers | no |
| `PeerRefused(reason, code, data)` | Declined before running; `reason` is `unresolved`, `bad_args`, `cycle`, `depth` or `self` | no |
| `PeerCallError` | The target raised, or its result cannot travel; has `remote_type`, `remote_message`, `remote_traceback`, `output`, `spill_path`, `task_id` | yes |
| `PeerDisconnected(clean)` | The connection ended before an answer; `clean` means the peer said goodbye first | unknown |

## Inside a called function

```python
import repld

repld.peer_caller()     # dict | None: kernel, claude_session_id, project_dir, session_kind, task_id, chain, pid_verified
```

## Commands

```bash
repld gist stub <peer> <gist>[.fn,...] [--as NAME] [--global] [--start] [--force]
repld gist stub --check [NAME...]
```

`--check` exits 1 if any stub is stale or unchecked. See the [guide](/repld/docs/guides/peers/) for behavior.
