#!/usr/bin/env python3
"""aion_node.py — a machine that can be given work, with nobody sitting at it.

`RemoteServer` is constructed inside the Textual cockpit, so a peer only
listens while a human has a terminal open on it. `docs/ssh-peers.md` says the
listener "is running, by default, on loopback port 8765" — which is true only
of a machine somebody is looking at. On a headless box (the pi), or one running
just the web HUD (pansa), nothing binds 8765 and every peer command fails with
a tunnel that connects to a closed port.

That is the whole reason a fleet exists: the machines you dispatch TO are the
ones you are not sitting at.

This is the cockpit's spine without its face — a Store, a task registry, the
harness table, and the same `RemoteServer` bound to the same loopback port,
wired to the same handlers `ui/app.py` wires. No Textual, no rendering, no
input. It answers `/status`, `/run`, `/task`, `/cancel`, `/gate` and `/swarm`
exactly as a cockpit does, because it is the same objects behind the socket.

Loopback only, always. Reaching this from another machine is `ssh -L`'s job:
`/run` executes prompts, and an endpoint that does that has no business on a
network interface. `AION_LISTEN=lan` is deliberately not honoured here.

    ./aion.sh node                 # foreground, port from the instance name
    ./aion.sh node --port 8765     # pin it (what peers expect)
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aion.core import Bus, load_config  # noqa: E402


async def serve(port: int, quiet: bool = False) -> None:
    from aion import fleet
    from aion.harnesses import build_harnesses
    from aion.remotes import RemoteServer
    from aion.store import Store

    cfg = load_config()
    bus = Bus()
    # The Store builds its OWN registry, so harnesses must be wired to that
    # one. Handing them a second registry gives the peer a task table nothing
    # it reports about is actually in.
    store = Store(cfg=cfg, bus=bus)
    registry = store.registry
    store.harnesses = build_harnesses(cfg["harnesses"], bus, registry,
                                      store.store)

    # 127.0.0.1 is not a default here, it is the design. See the docstring.
    server = RemoteServer(host="127.0.0.1", port=port)
    server.on_status = lambda: fleet.status_payload(store, headless=True)
    server.on_run = lambda prompt, harness: _run(store, prompt, harness)
    server.on_task_query = store.task_state
    server.on_task = lambda tid, action: store.control_task(tid, action)
    server.on_cancel = lambda tid: store.control_task(tid, "cancel")
    server.on_gate = lambda gid, approved: store.resolve_gate_by_id(gid, approved)
    server.on_swarm = store.swarm_command

    await server.start()
    inst = fleet.instance_id()
    if not quiet:
        print(f"[node] {inst} listening on 127.0.0.1:{port} "
              f"({len(store.harnesses)} harnesses)")
        print("[node] loopback only — reach it with: ssh -L <local>:127.0.0.1:"
              f"{port} <this host>")

    updates = asyncio.create_task(_update_watch(cfg, quiet))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # A node holds real work. Dropping the socket on a signal is the
        # difference between "the peer went away" and "the peer is a black
        # hole that accepts connections while shutting down".
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:      # pragma: no cover — not POSIX
            pass
    try:
        await stop.wait()
    finally:
        updates.cancel()
        await server.stop()
        if not quiet:
            print("[node] stopped")


async def _update_watch(cfg: dict, quiet: bool) -> None:
    """Say when this node falls behind origin. Off unless configured.

    A node is the machine nobody looks at, which is exactly where a stale
    checkout survives longest — air was three weeks behind while answering
    every request. The report goes to the log the unit already writes, so
    `journalctl --user -u aion-node` or `~/.aion/node.log` has it.

    Reports only. `auto_pull` still has to be asked for, and even then the new
    code needs a restart: swapping the source under a running process gets a
    node that is neither version.
    """
    from aion.selfupdate import (compare, describe, local_revision,
                                 policy_from_config, pull, upstream_revision)

    policy = policy_from_config(cfg)
    if not policy.enabled:
        return
    root = Path(__file__).resolve().parents[1]
    last = ""
    while True:
        try:
            local = await asyncio.to_thread(local_revision, root)
            upstream = await asyncio.to_thread(
                upstream_revision, root, policy.remote, policy.branch)
            drift = compare(local, upstream)
            line = describe(drift)
            # Only on change: a node logging "up to date" every quarter hour
            # is noise that buries the one line that matters.
            if line != last and not drift.in_sync:
                print(f"[node] {line}", flush=True)
            last = line
            if drift.behind_origin and policy.auto_pull:
                moved, msg = await asyncio.to_thread(pull, root, policy)
                print(f"[node] update: {msg}", flush=True)
                if moved:
                    # Leaving a node running code it no longer has on disk is
                    # the worst of both, so it restarts onto what it pulled.
                    #
                    # Via our own SIGTERM rather than SystemExit: raising
                    # inside a task only ends the task, and the point is to go
                    # through the same shutdown path a `systemctl stop` takes
                    # so the socket closes cleanly instead of being dropped
                    # mid-request. systemd (`Restart=always`) brings it back.
                    print("[node] restarting onto the new revision", flush=True)
                    signal.raise_signal(signal.SIGTERM)
                    return
            elif drift.behind_origin and policy.ask:
                # A node has nobody to ask. Saying so beats silently doing
                # nothing under a setting whose name promises a prompt.
                print("[node] behind origin; `ask` needs a cockpit — set "
                      "auto_pull, or update this node from the controller",
                      flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:                       # noqa: BLE001
            print(f"[node] update check failed: {type(e).__name__}: {e}",
                  flush=True)
        await asyncio.sleep(policy.check_every)


def _run(store, prompt: str, harness: str) -> dict:
    """Start work for a peer. Refusals are answers, never exceptions."""
    hid = (harness or getattr(store.state, "active_harness", "") or "").strip()
    if hid and hid not in store.harnesses:
        return {"error": f"unknown harness {hid!r}",
                "have": sorted(store.harnesses)}
    try:
        task_id = store._spawn_now(hid, prompt, label=f"peer/{prompt[:30]}")
    except Exception as e:                       # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}
    return {"task_id": task_id} if task_id else {"error": "spawn refused"}


def main(argv: list[str] | None = None) -> int:
    from aion import fleet

    ap = argparse.ArgumentParser(description="Run aion as a headless peer.")
    ap.add_argument("--port", type=int, default=None,
                    help="listen port (default: derived from instance name)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    port = args.port if args.port is not None else fleet.Heartbeat().port
    try:
        asyncio.run(serve(port, quiet=args.quiet))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
