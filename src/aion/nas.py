"""nas.py — Mesh NAS backend for aion.

Thin, read-only bridge between aion's HUD and the RandoMesh storage node
(pansa). Two jobs, both respecting aion's "render only, emit Intents" rule:

  * snapshot()  — poll pansa for share health/free-space, return a dict.
                  Pure parse helpers are split out so they unit-test cleanly
                  without any network.
  * mount_nas() — locally run an sshfs mount of a pansa share (triggered by a
                  MountNas Intent). aion never owns the filesystem; it just
                  launches the mount and reports status.

No file browsing, no chat-about-files — see SOVEREIGN_PLAN.md "Mesh NAS".
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field, asdict
from typing import Any

PANSA = "pansa-ts"
SHARES = {
    "bigstore": "/mnt/bigstore",
    "data-hgst": "/mnt/data-hgst",
}


@dataclass
class NasShare:
    name: str = ""
    mount: str = ""
    mounted: bool = False
    total_gb: float = 0.0
    used_gb: float = 0.0
    avail_gb: float = 0.0
    used_pct: int = 0
    health: str = "unknown"  # ok | warn | fail | unreachable
    note: str = ""


@dataclass
class NasSnapshot:
    reachable: bool = False
    shares: list[NasShare] = field(default_factory=list)
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _parse_df_line(line: str, mount: str, name: str) -> NasShare:
    """Parse one `df -h` line into a NasShare. Pure / testable."""
    parts = line.split()
    # expected: Filesystem  Size  Used  Avail  Use%  Mounted
    if len(parts) < 6:
        return NasShare(name=name, mount=mount, health="unreachable", note="no df output")
    size, used, avail, use_pct = parts[1], parts[2], parts[3], parts[4]

    def _to_gb(v: str) -> float:
        v = v.strip()
        try:
            if v.endswith("T"):
                return float(v[:-1]) * 1024
            if v.endswith("G"):
                return float(v[:-1])
            if v.endswith("M"):
                return float(v[:-1]) / 1024
            return float(v)
        except ValueError:
            return 0.0

    pct = int(use_pct.rstrip("%")) if use_pct.rstrip("%").isdigit() else 0
    health = "ok" if pct < 85 else ("warn" if pct < 95 else "fail")
    return NasShare(
        name=name,
        mount=mount,
        mounted=True,
        total_gb=round(_to_gb(size), 1),
        used_gb=round(_to_gb(used), 1),
        avail_gb=round(_to_gb(avail), 1),
        used_pct=pct,
        health=health,
    )


def _probe_pansa() -> NasSnapshot:
    """SSH to pansa, gather df for both shares + disk-watch health."""
    try:
        out = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes", PANSA,
             "df -h /mnt/bigstore /mnt/data-hgst 2>/dev/null; "
             "echo __HEALTH__; tail -n 6 /var/log/disk-watch.log 2>/dev/null"],
            capture_output=True, text=True, timeout=20,
        )
    except Exception as e:
        return NasSnapshot(reachable=False, note=f"probe error: {e}")

    if out.returncode != 0 or not out.stdout.strip():
        return NasSnapshot(reachable=False, note="pansa unreachable via ssh")

    body = out.stdout
    health_blob = ""
    if "__HEALTH__" in body:
        body, health_blob = body.split("__HEALTH__", 1)

    snap = NasSnapshot(reachable=True)
    for line in body.splitlines():
        if "/mnt/bigstore" in line:
            snap.shares.append(_parse_df_line(line, SHARES["bigstore"], "bigstore"))
        elif "/mnt/data-hgst" in line:
            snap.shares.append(_parse_df_line(line, SHARES["data-hgst"], "data-hgst"))

    if "PASSED" not in health_blob and health_blob.strip():
        for s in snap.shares:
            if s.health == "ok":
                s.health = "warn"
                s.note = "SMART check not PASSED"
    return snap


def snapshot() -> dict[str, Any]:
    """Public: current Mesh NAS state for the HUD. Never raises."""
    try:
        return _probe_pansa().as_dict()
    except Exception as e:  # defensive: HUD must never crash on storage blip
        return NasSnapshot(reachable=False, note=str(e)).as_dict()


def mount_nas(share: str, mountpoint: str | None = None) -> dict[str, Any]:
    """Locally mount a pansa share via sshfs (triggered by MountNas Intent).

    Uses nas-mount.sh if present, else inline sshfs. Returns a status dict.
    aion only *launches* the mount; the OS owns the filesystem.
    """
    if share not in SHARES:
        return {"ok": False, "error": f"unknown share {share}"}
    helper = shutil.which("nas-mount.sh") or "/home/gio/dev/randomesh/scripts/nas-mount.sh"
    try:
        if shutil.which("sshfs"):
            cmd = ["sshfs", f"{PANSA}:{SHARES[share]}",
                   mountpoint or f"/home/gio/nas-{share}",
                   "-o", "reconnect,ServerAliveInterval=15,ServerAliveCountMax=3,allow_other"]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            return {"ok": r.returncode == 0, "error": r.stderr.strip() or None}
        return {"ok": False, "error": "sshfs not installed"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
