"""`repld gate` — see and answer the human gates a kernel is blocked on.

`ask()` / `confirm()` / `choose()` park a cell until a human answers. A kernel
started as `repld` in a pane reads that answer off its own stdin, and a pinned
browser tab can answer through the pill — but a kernel spawned *for* you (Claude
Code's bridge, `repld restart`, a systemd unit) has neither: it runs
`--no-display` with stdin on /dev/null. This is the surface for that kernel, and
it is now the common one.

Read-only discovery already worked — `repld log -f` renders the prompt, and
since the gate id travels with it you can see what to type. What was missing was
the verb, so this is deliberately small: two JSON-RPC calls over the same IPC
socket every other command uses. The kernel owns the coercion (`gates/resolve`
consults the gate's own kind), so this file never has to know what a confirm is.
"""

import json
import sys
from pathlib import Path

from . import cli_args, paths
from .exec_cmd import _call, _connect
from .render import BOLD, DIM, RESET, gate_hint

_USAGE = """\
repld gate — human gates this project's kernel is waiting on

  repld gate [--json] [--socket PATH]
  repld gate answer <gate_id> <value> [--socket PATH]

  A kernel you started with `repld` takes answers in its own pane. One that
  was spawned for you has no pane — answer it here.

  <value> is y/n for a confirm, an option name or its 1-based number for a
  choose, and free text for an ask. Find the id in `repld log -f` or in the
  `awaiting human` channel message.
"""

_LABEL = "repld gate"


def _err(msg: str) -> None:
    print(f"{_LABEL}: {msg}", file=sys.stderr, flush=True)


def _report_error(err: dict) -> int:
    """Render a JSON-RPC error, naming version skew when that's what it is.

    A kernel is meant to run for weeks, so upgrading repld leaves the *old* one
    serving this project until someone restarts it — and an old one has no
    `gates/list`. Reporting the raw `method not found` would send you looking
    for a bug in a command that is simply newer than the kernel it's talking to.
    """
    if err.get("code") == -32601:
        _err(
            "this kernel predates `repld gate` — restart it to pick the command "
            "up (`repld restart`), or answer in its pane if it has one"
        )
        return 1
    _err(err.get("message", "unknown error"))
    return 1


def _fmt(gate: dict) -> str:
    kind = gate.get("kind", "?")
    # Via render.py, not rebuilt here: this is a human reading what to type,
    # and it has to name the same options as the pane and `repld log -f`.
    hint = gate_hint(kind, gate.get("options"))
    hint = f" {hint}" if hint else ""
    waited = gate.get("waiting_s", 0)
    return (
        f"{BOLD}{gate.get('gate_id', '?')}{RESET}  {gate.get('prompt', '')}"
        f"{DIM}{hint}  ({kind}, waiting {waited}s){RESET}"
    )


def run_gate(argv: list[str]) -> int:
    # Ahead of the `answer` branch, so `repld gate answer --help` is a request
    # for usage rather than a gate id of '--help' with no value.
    if cli_args.wants_help(argv):
        print(_USAGE)
        return 0

    lock_path, rest = paths.resolve_lock_path(argv)
    as_json = "--json" in rest
    rest = [a for a in rest if a != "--json"]

    if rest and rest[0] == "answer":
        if len(rest) < 3:
            # stdout, matching every other subcommand's usage-on-error. `_err`
            # is for runtime failures, where stderr is right.
            print(f"{_LABEL}: answer needs a gate id and a value\n")
            print(_USAGE)
            return 2
        gate_id = rest[1]
        # Everything after the id, so an unquoted multi-word `ask` answer works.
        value = " ".join(rest[2:])
        return _answer(lock_path, gate_id, value, as_json)

    # Only the listing form reaches here — `answer` returned above, and its
    # trailing words are the answer itself, which no flag-set check can vet.
    bad = cli_args.check_args(_LABEL, rest, _USAGE, positionals=0)
    if bad is not None:
        return bad
    return _list(lock_path, as_json)


def _list(lock_path: Path, as_json: bool) -> int:
    conn = _connect(lock_path, label=_LABEL)
    if conn is None:
        return 1
    sock, rfile, wfile, _lock = conn
    try:
        resp = _call(rfile, wfile, "gates/list")
        if resp is None:
            _err("kernel disconnected")
            return 1
        if "error" in resp:
            return _report_error(resp["error"])
        gates = resp.get("result", {}).get("gates", [])
        if as_json:
            print(json.dumps(gates, indent=2))
            return 0
        if not gates:
            print("no gates awaiting an answer")
            return 0
        for g in gates:
            print(_fmt(g))
        print(f"\n{DIM}answer with: repld gate answer <gate_id> <value>{RESET}")
        return 0
    finally:
        sock.close()


def _answer(lock_path: Path, gate_id: str, value: str, as_json: bool) -> int:
    conn = _connect(lock_path, label=_LABEL)
    if conn is None:
        return 1
    sock, rfile, wfile, _lock = conn
    try:
        resp = _call(
            rfile, wfile, "gates/resolve", {"gate_id": gate_id, "value": value}
        )
        if resp is None:
            _err("kernel disconnected")
            return 1
        if "error" in resp:
            return _report_error(resp["error"])
        result = resp.get("result", {})
        if as_json:
            print(json.dumps(result, indent=2))
        else:
            print(f"gate {result.get('gate_id')} answered: {result.get('value')!r}")
        return 0
    finally:
        sock.close()
