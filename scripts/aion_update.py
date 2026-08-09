#!/usr/bin/env python3
"""aion_update.py — what is every machine in this fleet running?

    ./aion.sh update           # this machine vs origin vs every peer
    ./aion.sh update --pull    # and fast-forward this machine if behind

Peer revisions come from the `/status` each node already serves, so this needs
no extra endpoint and no ssh beyond the tunnel that already exists.

It never pulls code from a peer, only from origin. A peer is trusted with
fleet membership, not with supplying the software the fleet runs.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aion.selfupdate import (  # noqa: E402
    Revision, UpdatePolicy, compare, describe, local_revision, pull,
    upstream_revision)


async def peer_revisions(timeout: float = 8.0) -> dict[str, Revision]:
    """Ask every configured peer what it is running. Silence is not a crash.

    Returns the whole Revision, not just the sha. `/status` has always carried
    `dirty`, and this function threw it away — so a peer with uncommitted work
    reported "differs from origin" and never the reason it could not act on
    that. pansa sat in exactly that state for weeks.
    """
    from aion.remotes import RemoteClient, RemoteNode
    from aion.sshlink import TunnelPool, load_peers

    out: dict[str, Revision] = {}
    peers = [p for p in load_peers() if p.enabled]
    if not peers:
        return out
    pool, client = TunnelPool(), RemoteClient(timeout=timeout)
    for peer in peers:
        try:
            state = pool.ensure(peer)
            if not state or not state.local_port:
                out[peer.id] = Revision()
                continue
            node = RemoteNode(id=peer.id, host="127.0.0.1",
                              port=state.local_port)
            status = await client.fetch_status(node)
        except Exception:                      # noqa: BLE001
            status = None
        rev = (status or {}).get("revision") or {}
        out[peer.id] = Revision(sha=str(rev.get("sha") or ""),
                                branch=str(rev.get("branch") or ""),
                                dirty=bool(rev.get("dirty")))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fleet revision drift.")
    ap.add_argument("--pull", action="store_true",
                    help="fast-forward this machine if it is behind origin")
    ap.add_argument("--no-peers", action="store_true",
                    help="skip peers; check this machine against origin only")
    args = ap.parse_args(argv)

    local = local_revision(ROOT)
    upstream = upstream_revision(ROOT)
    peers = {} if args.no_peers else asyncio.run(peer_revisions())
    drift = compare(local, upstream, peers)

    print(f"local    {local.short or '?':>8}  {local.branch or '?'}"
          f"{'  (dirty)' if local.dirty else ''}")
    print(f"origin   {upstream[:7] or '?':>8}")
    for pid, rev in sorted(peers.items()):
        mark = "=" if local.same_as(rev) else ("?" if not rev.sha else "≠")
        note = "  (dirty — will refuse to update)" if rev.dirty else ""
        print(f"{pid:<8} {rev.short or '?':>8}  {mark}{note}")
    print(f"\n{describe(drift)}")

    if args.pull and drift.behind_origin:
        moved, msg = pull(ROOT, UpdatePolicy(check_every=1, auto_pull=True))
        print(f"pull: {msg}")
        return 0 if moved else 1
    # Exit non-zero when something is out of step, so this is usable as a
    # check in a script rather than only by eye.
    return 0 if drift.in_sync else 1


if __name__ == "__main__":
    raise SystemExit(main())
