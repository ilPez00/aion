#!/usr/bin/env python3
"""aion_peers.py — manage the SSH peers this cockpit can reach.

    aion.sh peers                       list peers and tunnel state
    aion.sh peers add pi5 gio@10.0.0.5 [--key ~/.ssh/aion_pi5] [--port 22]
                                        [--remote-port 8765] [--label "The Pi"]
    aion.sh peers rm pi5
    aion.sh peers on|off pi5
    aion.sh peers test [pi5]            open the tunnel and poll /status
    aion.sh peers authorize [--remote-port 8765] [--from IP]
                                        print the authorized_keys line to
                                        install on the REMOTE machine

Nothing here dials anywhere on its own; `test` is the only command that opens
a connection, and only to peers already in the config.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aion import sshlink  # noqa: E402
from aion.fleet import BASE_PORT  # noqa: E402


def cmd_list(args) -> int:
    peers = sshlink.load_peers()
    if not peers:
        print("no peers configured.")
        print(f"  add one:  aion.sh peers add <id> <user@host>")
        print(f"  config:   {sshlink.peers_path()}")
        return 0
    print(f"{'ID':<14} {'TARGET':<28} {'REMOTE':<8} STATE")
    for p in peers:
        state = "enabled" if p.enabled else "disabled"
        print(f"{p.id:<14} {p.target():<28} {p.remote_port:<8} {state}"
              f"{'  ' + p.label if p.label else ''}")
    return 0


def cmd_add(args) -> int:
    user, _, host = args.target.rpartition("@")
    peer = sshlink.SSHPeer(
        id=args.id, host=host, user=user, ssh_port=args.port,
        remote_port=args.remote_port, key=args.key or "", label=args.label or "")
    problems = sshlink.validate_peer(peer)
    if problems:
        for p in problems:
            print(f"refused: {p}", file=sys.stderr)
        return 2
    peers = [p for p in sshlink.load_peers() if p.id != peer.id]
    peers.append(peer)
    sshlink.save_peers(peers)
    print(f"added {peer.id} -> {peer.target()} (remote aion on port {peer.remote_port})")
    print(f"\nnow install this on {host}, in ~/.ssh/authorized_keys:\n")
    print("  " + sshlink.authorized_keys_line(
        "<paste the PUBLIC key matching " + (args.key or "your key") + ">",
        peer.remote_port))
    print("\nthen:  aion.sh peers test " + peer.id)
    return 0


def cmd_rm(args) -> int:
    peers = sshlink.load_peers()
    kept = [p for p in peers if p.id != args.id]
    if len(kept) == len(peers):
        print(f"no peer named {args.id!r}", file=sys.stderr)
        return 1
    sshlink.save_peers(kept)
    print(f"removed {args.id}")
    return 0


def _set_enabled(peer_id: str, enabled: bool) -> int:
    peers = sshlink.load_peers()
    for p in peers:
        if p.id == peer_id:
            p.enabled = enabled
            sshlink.save_peers(peers)
            print(f"{peer_id} {'enabled' if enabled else 'disabled'}")
            return 0
    print(f"no peer named {peer_id!r}", file=sys.stderr)
    return 1


def cmd_test(args) -> int:
    """Open the tunnel for real and ask the far end who it is.

    Reports the two failures separately, because they need different fixes: a
    tunnel that will not open is an ssh problem (key, host key, firewall), and
    a tunnel that opens onto silence is an aion problem (not running, wrong
    remote port, token mismatch).
    """
    from aion.remotes import RemoteClient, RemoteNode

    peers = [p for p in sshlink.load_peers()
             if (not args.id or p.id == args.id) and p.enabled]
    if not peers:
        print("nothing to test", file=sys.stderr)
        return 1

    pool = sshlink.TunnelPool()
    rc = 0
    try:
        for peer in peers:
            state = pool.ensure(peer, wait=12.0)
            if not state.up:
                print(f"{peer.id}: TUNNEL FAILED — {state.last_error or 'unknown'}")
                rc = 1
                continue
            node = RemoteNode(id=peer.id, host="127.0.0.1", port=state.local_port)
            status = asyncio.run(RemoteClient(timeout=5.0).fetch_status(node))
            if not status:
                print(f"{peer.id}: tunnel up on 127.0.0.1:{state.local_port}, "
                      f"but no aion answered on {peer.host}:{peer.remote_port} "
                      f"(not running, wrong --remote-port, or token mismatch)")
                rc = 1
                continue
            print(f"{peer.id}: OK — {status.get('hostname', '?')} · "
                  f"{status.get('running_count', 0)} running · "
                  f"harness {status.get('active_harness', '—')} "
                  f"(via 127.0.0.1:{state.local_port})")
    finally:
        pool.close_all()
    return rc


def cmd_authorize(args) -> int:
    print(sshlink.authorized_keys_line(
        args.pubkey or "<your-public-key>", args.remote_port, args.source or ""))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="aion.sh peers", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("list")

    a = sub.add_parser("add")
    a.add_argument("id")
    a.add_argument("target", help="user@host or host")
    a.add_argument("--key", default="")
    a.add_argument("--port", type=int, default=22, help="ssh port")
    a.add_argument("--remote-port", type=int, default=BASE_PORT,
                   help="port aion listens on over there")
    a.add_argument("--label", default="")

    r = sub.add_parser("rm")
    r.add_argument("id")
    for name in ("on", "off"):
        s = sub.add_parser(name)
        s.add_argument("id")

    t = sub.add_parser("test")
    t.add_argument("id", nargs="?", default="")

    z = sub.add_parser("authorize")
    z.add_argument("pubkey", nargs="?", default="")
    z.add_argument("--remote-port", type=int, default=BASE_PORT)
    z.add_argument("--from", dest="source", default="")

    args = ap.parse_args(argv)
    if args.cmd in (None, "list"):
        return cmd_list(args)
    if args.cmd == "add":
        return cmd_add(args)
    if args.cmd == "rm":
        return cmd_rm(args)
    if args.cmd in ("on", "off"):
        return _set_enabled(args.id, args.cmd == "on")
    if args.cmd == "test":
        return cmd_test(args)
    if args.cmd == "authorize":
        return cmd_authorize(args)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
