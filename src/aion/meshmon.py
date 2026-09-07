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

import json
import os
import re
import time
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
    "air": "cpu-inference",  # CPU-only laptop (4-core, 7.7GB, no GPU); slow, conditional
    "pi": "edge-inference",
    "feather": "client-hud",
    "omo": "source-storage",
    "pansa": "storage-node",
}

# ── Fleet config (single source of truth = randomesh CONFIG.md → fleet.json) ──
# aion should NOT hardcode the node map here (that drifted — air was "primary-compute"
# while reality is CPU-only). Load nodes/roles from randomesh's exported fleet.json
# when present; these built-ins are only the fallback. Set AION_FLEET_CONFIG to point
# elsewhere, e.g. a copy deployed to the HUD host.
def _load_fleet_config() -> tuple[dict[str, str], dict[str, str]]:
    import json
    import os

    path = os.environ.get("AION_FLEET_CONFIG", "")
    if not path:
        path = os.path.expanduser("~/dev/randomesh/fleet.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        nodes, roles = dict(NODES), dict(ROLE)
        for n in data.get("nodes", []):
            name = n.get("name")
            if not name:
                continue
            nodes[name] = n.get("tailscale") or (name + "-ts")
            if n.get("role"):
                roles[name] = n["role"]
        return nodes, roles
    except Exception:
        return dict(NODES), dict(ROLE)  # fleet.json absent/unreadable — use built-ins

_NODES_FROM_CONFIG, _ROLE_FROM_CONFIG = _load_fleet_config()
NODES, ROLE = _NODES_FROM_CONFIG, _ROLE_FROM_CONFIG


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


# ── Fleet manager snapshot (services / machines / programs / configs /
#    network / agents) ──────────────────────────────────────────────────────
# Source of truth is randomesh `mesh facts` (scripts/fleet/facts.sh), which
# sweeps every node and caches JSON at a path both sides treat as a contract.
# aion reads that cache when fresh (60s); when stale it triggers exactly one
# on-demand resweep; when the whole facts layer is unavailable it degrades to
# the legacy per-node SSH probes above (machines only). All parsers are pure
# and unit-test with fixture dicts — no I/O in logic.

FACTS_CACHE = os.environ.get(
    "AION_FACTS_CACHE", os.path.expanduser("~/.cache/randomesh/facts.json"))
FACTS_MAX_AGE_S = 60.0
FACTS_CMD = ["bash", os.path.expanduser(
    "~/dev/randomesh/scripts/fleet/facts.sh"), "--json"]


def _local_runner(cmd: list[str]) -> tuple[int, str]:
    """Default facts runner: exec `mesh facts --json` and return (rc, stdout)."""
    import subprocess
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        return r.returncode, r.stdout
    except Exception as e:  # noqa: BLE001 — a failed sweep must soft-fail
        return 1, str(e)


def _load_facts(max_age_s: float = FACTS_MAX_AGE_S,
                runner: Callable | None = None) -> dict[str, Any] | None:
    """Fresh cache -> dict; stale/missing -> one resweep then re-read;
    unavailable -> None (callers fall back to legacy probes)."""
    try:
        if time.time() - os.stat(FACTS_CACHE).st_mtime <= max_age_s:
            with open(FACTS_CACHE, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and "nodes" in data:
                return data
    except (OSError, ValueError):
        pass
    rc, out = (runner or _local_runner)(FACTS_CMD)
    if rc != 0:
        return None
    try:
        data = json.loads(out)
    except ValueError:
        return None
    if isinstance(data, dict) and "nodes" in data:
        return data
    return None


def _host_matches(host_alias: str, hostname: str) -> bool:
    """fleet.json hosts are tailscale aliases (omo-ts, feather-1, air); facts
    are keyed by node with hostname inside. Loose either-way prefix match."""
    if not host_alias or not hostname:
        return False
    return (host_alias.startswith(hostname)
            or hostname.startswith(host_alias.split("-")[0]))


def _machine_rows(facts: dict) -> list[dict]:
    rows = []
    for name, node in (facts.get("nodes") or {}).items():
        f = node.get("facts") or {}
        hw = f.get("hw") or {}
        rows.append({
            "name": name, "role": ROLE.get(name, ""),
            "online": bool(node.get("online")),
            "cpu": hw.get("cpu_model") or "", "cores": hw.get("cores"),
            "load1": hw.get("load1"),
            "ram_total_mb": hw.get("ram_total_mb"),
            "ram_avail_mb": hw.get("ram_avail_mb"),
            "gpus": hw.get("gpus") or [],
            "uptime_s": (f.get("host") or {}).get("uptime_s"),
            "disks": f.get("disks") or [],
            "error": node.get("error", "") if not node.get("online") else "",
        })
    return rows


def _service_rows(facts: dict) -> list[dict]:
    """fleet.json service catalog x each node's live systemd state. A service
    with a unit gets its active/sub; a tcp-probe service is active when the
    node's listening-port list contains the port."""
    catalog: dict = {}
    try:
        path = os.environ.get("AION_FLEET_CONFIG") or os.path.expanduser(
            "~/dev/randomesh/fleet.json")
        with open(path, encoding="utf-8") as fh:
            catalog = (json.load(fh).get("services") or {})
    except Exception:
        pass
    nodes = facts.get("nodes") or {}

    def node_facts_for(host_alias: str) -> dict:
        for node in nodes.values():
            if not node.get("online"):
                continue
            f = node.get("facts") or {}
            if _host_matches(host_alias, (f.get("host") or {}).get("hostname", "")):
                return f
        return {}

    rows = []
    for name, spec in catalog.items():
        host = spec.get("host", "")
        unit = spec.get("unit") or ""
        f = node_facts_for(host)
        state, sub = ("unknown", "")
        if not f:
            state = "down"  # host itself unreachable in this sweep
        elif unit:
            for u in (f.get("services") or {}).get("user_units") or []:
                if u.get("unit") == unit:
                    state, sub = u.get("active", "unknown"), u.get("sub", "")
                    break
        else:
            m = re.match(r"(?:tcp|http):(\d+)", str(spec.get("probe") or ""))
            if m:
                # tcp and http probes both reduce to "is the port listening"
                # in a facts snapshot; the actual HTTP check happens live in
                # meshsrv.probe_service when the operator asks for detail.
                state = ("active" if int(m.group(1)) in (f.get("ports") or [])
                         else "inactive")
        rows.append({"name": name, "host": host, "unit": unit,
                     "critical": bool(spec.get("critical")),
                     "state": state, "sub": sub, "note": spec.get("note", "")})
    return rows


def _program_rows(facts: dict) -> list[dict]:
    rows = []
    for name, node in (facts.get("nodes") or {}).items():
        if not node.get("online"):
            continue
        f = node.get("facts") or {}
        rows.append({
            "name": name,
            "programs": f.get("programs") or {},
            "packages": f.get("packages") or {},
            "ollama_models": [m.get("name") for m in (f.get("ollama") or {}).get("models") or []],
            "llama_builds": f.get("llama_builds") or [],
        })
    return rows


def _config_rows(facts: dict) -> list[dict]:
    rows = []
    for name, node in (facts.get("nodes") or {}).items():
        if not node.get("online"):
            continue
        row = {"name": name}
        row.update(node.get("facts", {}).get("configs") or {})
        rows.append(row)
    return rows


def _network_rows(facts: dict) -> list[dict]:
    rows = []
    for name, node in (facts.get("nodes") or {}).items():
        if not node.get("online"):
            continue
        f = node.get("facts") or {}
        ts = (f.get("network") or {}).get("tailscale") or {}
        peers = ts.get("peers") or []
        rows.append({
            "name": name, "ts_up": bool(ts.get("up")),
            "self_ip": ts.get("self_ip"),
            "peers_online": sorted(p["name"] for p in peers if p.get("online")),
            "peers_total": len(peers),
            "listening_ports": f.get("ports") or [],
        })
    return rows


def snapshot_sections(transport: Callable | None = None,
                      facts_runner: Callable | None = None) -> dict[str, Any]:
    """Six-section fleet-manager snapshot. Never raises; degrades honestly."""
    data = _load_facts(runner=facts_runner)
    if data:
        return {
            "source": "facts",
            "generated": data.get("generated"),
            "machines": _machine_rows(data),
            "services": _service_rows(data),
            "programs": _program_rows(data),
            "configs": _config_rows(data),
            "network": _network_rows(data),
            "agents": data.get("agents") or [],
        }
    snap = snapshot(transport or _default_transport)
    return {"source": "legacy", "generated": None,
            "machines": snap["nodes"], "services": [], "programs": [],
            "configs": [], "network": [], "agents": []}
