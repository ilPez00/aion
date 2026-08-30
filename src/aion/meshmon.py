"""meshmon.py — RandoMesh node monitor (read-only).

Part of aion-as-mesh-suite. Probes each physical mesh node (air / pi /
feather / omo / pansa) over SSH and returns a snapshot the HUD renders.

Design (per aion-extend-backend): collectors are PURE. All I/O lives behind an
injectable `transport(method, target, cmd) -> (rc, out)` callable so the parse
helpers unit-test with a fake transport and never touch the network. Every
node soft-fails: one dead node must never blank the mesh panel.

This module is the *monitor* half. Node/service control (restart, deploy) is
a later phase — Phase 1 is read-only visibility, nothing mutates a node.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Callable

# Tailscale aliases for the physical mesh. pansa also surfaces storage.
NODES: dict[str, str] = {
    "air": "air-ts",
    "pi": "pi-ts",
    "feather": "feather-ts",
    "omo": "omo-ts",
    "pansa": "pansa-ts",
}

# What each node is primarily for (from CONFIG.md / SOVEREIGN_PLAN).
ROLE = {
    "air": "primary-compute",
    "pi": "edge-inference",
    "feather": "client-hud",
    "omo": "source-storage",
    "pansa": "storage-node",
}


@dataclass
class NodeStat:
    name: str = ""
    role: str = ""
    reachable: bool = False
    load1: float = 0.0
    ram_pct: int = 0
    disk_pct: int = 0
    uptime: str = ""
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _default_transport(method: str, target: str, cmd: str) -> tuple[int, str]:
    """Real transport: SSH exec. Imported lazily so tests never shell out."""
    import subprocess

    if method != "ssh":
        return 1, ""
    try:
        r = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=6", "-o", "BatchMode=yes", target, cmd],
            capture_output=True, text=True, timeout=15,
        )
        return r.returncode, (r.stdout + r.stderr)
    except Exception as e:  # network/timeout — node counts as unreachable
        return 1, str(e)


def _parse_stat(block: str, name: str, role: str) -> NodeStat:
    """Parse a node's probe block into a NodeStat. Pure / testable."""
    s = NodeStat(name=name, role=role)
    parts = [p for p in block.split("__S__")]

    # uptime (first chunk): " 12:34:56 up 3 days,  4:00,  2 users,  load average: 0.10, 0.05, 0.01"
    if parts and parts[0].strip():
        up = parts[0]
        s.uptime = up.split("up", 1)[-1].split(",")[0].strip() if "up" in up else ""
        if "load average:" in up:
            try:
                s.load1 = float(up.split("load average:")[-1].split(",")[0].strip())
            except ValueError:
                pass

    # loadavg (second chunk): "0.10 0.05 0.01 1/234 5678"
    if len(parts) > 1 and parts[1].strip():
        try:
            s.load1 = float(parts[1].split()[0])
        except (ValueError, IndexError):
            pass

    # free -b (third chunk): "Mem: 12345678 2345678 ..."
    if len(parts) > 2 and parts[2].strip():
        lines = parts[2].strip().splitlines()
        for ln in lines:
            if ln.lower().startswith("mem:"):
                nums = [int(x) for x in ln.split()[1:] if x.isdigit()]
                if len(nums) >= 2 and nums[0] > 0:
                    s.ram_pct = round(100 * nums[1] / nums[0])

    # df -h / (fourth chunk): "Filesystem  Size  Used  Avail  Use%  Mounted"
    if len(parts) > 3 and parts[3].strip():
        for ln in parts[3].strip().splitlines():
            if ln.endswith("/") or " /" in ln:
                cols = ln.split()
                if len(cols) >= 5:
                    pct = cols[4].rstrip("%")
                    if pct.isdigit():
                        s.disk_pct = int(pct)
    return s


def probe_node(name: str, transport: Callable = _default_transport) -> NodeStat:
    alias = NODES.get(name, name)
    role = ROLE.get(name, "")
    cmd = "uptime; echo __S__; cat /proc/loadavg; echo __S__; free -b | head -2; echo __S__; df -h / | tail -1"
    rc, out = transport("ssh", alias, cmd)
    if rc != 0 or not out.strip():
        return NodeStat(name=name, role=role, reachable=False, note="unreachable")
    s = _parse_stat(out, name, role)
    s.reachable = True
    return s


def snapshot(transport: Callable = _default_transport) -> dict[str, Any]:
    """Public: probe all mesh nodes. Never raises; soft-fails per node."""
    nodes: list[NodeStat] = []
    for name in NODES:
        try:
            nodes.append(probe_node(name, transport))
        except Exception as e:
            nodes.append(NodeStat(name=name, role=ROLE.get(name, ""),
                                  reachable=False, note=f"err: {e}"))
    reachable = sum(1 for n in nodes if n.reachable)
    # Fold in pansa storage health from the NAS backend if available.
    storage = {}
    try:
        from .nas import snapshot as nas_snap
        storage = nas_snap()
    except Exception:
        pass
    return {
        "nodes": [n.as_dict() for n in nodes],
        "total": len(nodes),
        "reachable": reachable,
        "storage": storage,
    }
