"""sysinfo.py — COMPUTER statistics reader (Iron Man HUD backend).

Collects CPU / RAM / disk / network / GPU from the host via psutil, plus the
GPU probe reused by TelemetryHarness. Returns a plain dict (no I/O surprises)
so SystemHarness can publish it on the bus and the `sys` workspace render it.

Degrades gracefully: if psutil is unavailable, snapshot() returns
{"ok": False, "error": "psutil missing"} and the HUD shows "(stats unavailable)".
"""
from __future__ import annotations

import shutil
from typing import Any

try:
    import psutil
except Exception:  # noqa: BLE001
    psutil = None  # type: ignore[assignment]


def _gpu_probe() -> dict[str, Any]:
    """Best-effort GPU stats (shared with TelemetryHarness logic)."""
    import subprocess
    out: dict[str, Any] = {}
    # nvidia-smi first
    try:
        proc = subprocess.run(
            "nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total "
            "--format=csv,noheader,nounits",
            shell=True, capture_output=True, text=True, timeout=2)
        lines = (proc.stdout or "").strip().splitlines()[:1]
        if lines:
            parts = [p.strip() for p in lines[0].split(",")]
            if len(parts) == 3:
                out["gpu_util_pct"] = int(parts[0])
                out["gpu_mem_mb"] = int(parts[1])
                out["gpu_mem_total_mb"] = int(parts[2])
    except Exception:  # noqa: BLE001
        pass
    if "gpu_util_pct" not in out:
        # fall back to counting loaded ollama models as a rough GPU proxy
        try:
            proc = subprocess.run("ollama ps --format json", shell=True,
                                  capture_output=True, text=True, timeout=2)
            import json as _json
            data = _json.loads(proc.stdout or "{}")
            models = data.get("models", [])
            if models:
                out["gpu_models"] = len(models)
                out["gpu_vram_mb"] = sum(
                    m.get("size_vram", 0) for m in models) // (1024 * 1024)
        except Exception:  # noqa: BLE001
            pass
    return out


def _thermal_probe() -> dict[str, Any]:
    """Best-effort CPU/thermal sensor readings via psutil."""
    out: dict[str, Any] = {}
    if psutil is None:
        return out
    try:
        temps = psutil.sensors_temperatures()
        if temps:
            cpu_temps = []
            for name, entries in temps.items():
                for entry in entries:
                    if entry.current is not None:
                        label = entry.label or name
                        lower = label.lower()
                        # CPU-related: core, package, tctl, tdie, cpu, k10temp, zen
                        if any(k in lower for k in ("core", "package", "cpu", "tctl", "tdie", "k10temp", "zen", "edge")):
                            cpu_temps.append({"label": label, "current": entry.current, "high": entry.high, "critical": entry.critical})
            if cpu_temps:
                out["cpu"] = cpu_temps
                out["max_cpu_c"] = max(t["current"] for t in cpu_temps)
            # Also include other sensors (gpu, nvme, etc.) if present
            other = []
            for name, entries in temps.items():
                for entry in entries:
                    if entry.current is not None:
                        label = entry.label or name
                        lower = label.lower()
                        if not any(k in lower for k in ("core", "package", "cpu", "tctl", "tdie", "k10temp", "zen", "edge")):
                            other.append({"label": label, "current": entry.current, "high": entry.high, "critical": entry.critical})
            if other:
                out["other"] = other
    except Exception:
        pass
    return out


class SystemReader:
    def __init__(self, disk_paths: list[str] | None = None) -> None:
        self.disk_paths = disk_paths or self._default_disks()
        self._net_prev = None
        self._net_prev_ts = 0.0
        self._boot_ts = None

    @staticmethod
    def _default_disks() -> list[str]:
        if psutil is None:
            return []
        mounts = []
        for p in psutil.disk_partitions(all=False):
            # skip pseudo / network mounts for a cleaner readout
            if p.fstype and ("squashfs" not in p.fstype.lower()
                             and "tmpfs" not in p.fstype.lower()):
                mounts.append(p.mountpoint)
        # dedupe + always include / if present
        mounts = list(dict.fromkeys(mounts))
        if "/" not in mounts and psutil is not None:
            mounts.insert(0, "/")
        return mounts[:6]  # cap to keep the HUD tidy

    def snapshot(self) -> dict[str, Any]:
        if psutil is None:
            return {"ok": False, "error": "psutil missing",
                    "cpu": None, "mem": None, "disks": [], "net": {}, "gpu": {}}
        snap: dict[str, Any] = {"ok": True}
        # ---- CPU ----
        per = psutil.cpu_percent(interval=None, percpu=True)
        snap["cpu"] = {
            "total_pct": round(psutil.cpu_percent(interval=None), 1),
            "per_core_pct": [round(x, 1) for x in per],
            "cores": len(per),
            "load1": round(psutil.getloadavg()[0], 2),
        }
        try:
            snap["cpu"]["freq_mhz"] = int(psutil.cpu_freq().current or 0)
        except Exception:  # noqa: BLE001
            snap["cpu"]["freq_mhz"] = 0
        # ---- RAM ----
        vm = psutil.virtual_memory()
        swap = psutil.swap_memory()
        snap["mem"] = {
            "total": vm.total, "used": vm.used, "free": vm.available,
            "pct": round(vm.percent, 1),
            "swap_total": swap.total, "swap_used": swap.used,
            "swap_pct": round(swap.percent, 1),
        }
        # ---- disk ----
        disks = []
        for mp in self.disk_paths:
            try:
                du = shutil.disk_usage(mp)
                disks.append({
                    "mount": mp, "total": du.total, "used": du.used,
                    "free": du.free, "pct": round(du.used / du.total * 100, 1),
                })
            except Exception:  # noqa: BLE001
                continue
        snap["disks"] = disks
        # ---- network ----
        net = psutil.net_io_counters()
        now = __import__("time").time()
        rates = {"up_bps": 0.0, "down_bps": 0.0}
        if self._net_prev is not None:
            dt = max(0.1, now - self._net_prev_ts)
            rates["up_bps"] = max(0.0, (net.bytes_sent - self._net_prev[0]) / dt)
            rates["down_bps"] = max(0.0, (net.bytes_recv - self._net_prev[1]) / dt)
        self._net_prev = (net.bytes_sent, net.bytes_recv)
        self._net_prev_ts = now
        snap["net"] = {
            "up_bps": round(rates["up_bps"], 1),
            "down_bps": round(rates["down_bps"], 1),
            "total_sent": net.bytes_sent, "total_recv": net.bytes_recv,
            "conns": len(psutil.net_connections(kind="inet")),
        }
        # ---- GPU ----
        snap["gpu"] = _gpu_probe()
        # ---- Thermal ----
        snap["thermal"] = _thermal_probe()
        # ---- processes ----
        snap["processes"] = self.processes()
        return snap

    def processes(self, top_n: int = 10) -> list[dict[str, Any]]:
        if psutil is None:
            return []
        out: list[dict[str, Any]] = []
        try:
            procs = []
            for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info", "status"]):
                try:
                    info = p.info
                    procs.append({
                        "pid": info["pid"],
                        "name": info["name"] or "?",
                        "cpu_pct": round(info["cpu_percent"] or 0.0, 1),
                        "mem_mb": round((info["memory_info"].rss if info["memory_info"] else 0) / (1024 * 1024), 1),
                        "status": info["status"] or "?",
                    })
                except Exception:
                    continue
            procs.sort(key=lambda x: x["cpu_pct"], reverse=True)
            out = procs[:top_n]
        except Exception:
            pass
        return out
