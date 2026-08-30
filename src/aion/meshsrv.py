"""meshsrv.py — RandoMesh service lifecycle (Phase 2).

Lets aion SEE and CONTROL mesh services (Physis, Praxis webapp, llama-server,
colibri) across the fleet. Read-only probe + SSH control, both with an
injectable transport so the pure logic is unit-testable (aion-extend-backend).

Service model:
  - each service has a host (Tailscale alias), a probe (tcp port OR systemd unit),
    and an optional start/stop cmd.
  - snapshot() probes every service; unreachable host -> service marked down,
    never raises.
  - control_service(name, action) runs start/stop/restart over SSH.

aion identity: this is a HUD/control surface, not an orchestrator that owns
the services. The deck emits MeshService Intents; meshsrv executes them.
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Callable, Optional

Transport = Callable[[str, str, str], tuple[int, str]]  # (method, target, cmd) -> (rc, out)


# name -> (host alias, probe kind, probe value, start-cmd, stop-cmd)
# probe kind: "tcp" (port) or "unit" (systemd unit name)
SERVICES: dict[str, dict] = {
    "physis": {
        "host": "omo-ts",
        "probe": ("tcp", 8090),
        "start": "cd /home/gio/physis_pro && PHYSIS_DEV=1 cargo run --release --bin physis-web 2>&1 | tail -3",
        "stop": "pkill -f physis-web",
    },
    "praxis_webapp": {
        "host": "omo-ts",
        "probe": ("tcp", 8070),
        "start": "cd /home/gio/praxis_webapp && npm run start 2>&1 | tail -3",
        "stop": "pkill -f 'npm run start'",
    },
    # RandoMesh model-serving stack (llama.cpp Vulkan/ROCm, Caddy LB on omo:8088).
    # Each node serves gemma4-e2b on :8081; the LB is the unified OpenAI endpoint.
    # Host check = ssh to that host and probe its localhost port.
    "mesh-lm-orch": {   # Caddy orchestrator on omo — unified /v1 endpoint
        "host": "omo-ts",
        "probe": ("tcp", 8088),
        "start": "cd /home/gio/scripts/freetoken-cluster && caddy run --config Caddyfile.llama --adapter caddyfile 2>&1 | tail -3",
        "stop": "pkill -f 'caddy run'",
    },
    "omo-llm": {        # llama-server node on RX 6650 XT (8GB, Vulkan/ROCm)
        "host": "omo-ts", "probe": ("tcp", 8081),
        "start": "nohup /home/gio/dev/scripts/llama-b8831/llama-server -m /home/gio/models/gemma4-e2b-heretic-Q4_K_M.gguf -ngl 99 --host 0.0.0.0 --port 8081 --alias e2b --jinja -c 4096 >/tmp/omo-llama.log 2>&1 &",
        "stop": "pkill -f 'llama-server.*8081'",
    },
    "pansa-llm": {      # llama-server node on RX 550 (2GB, partial + CPU)
        "host": "pansa-ts", "probe": ("tcp", 8081),
        "start": "bash /home/gio/models/pansa_node.sh",
        "stop": "pkill -f 'llama-server.*8081'",
    },
    "air-llm": {        # llama-server CPU node (4-core, slow/conditional)
        "host": "air-ts", "probe": ("tcp", 8081),
        "start": "bash /home/gio/air_node.sh",
        "stop": "pkill -f 'llama-server.*8081'",
    },
    "colibri": {
        "host": "omo-ts",
        "probe": ("tcp", 11435),
        "start": "cd /home/gio/colibri && uv run colibri serve 2>&1 | tail -3",
        "stop": "pkill -f colibri",
    },
}


@dataclass
class ServiceState:
    name: str
    host: str
    probe_kind: str
    probe_value: str
    running: bool = False
    reachable: bool = False
    detail: str = ""
    start_cmd: str = ""
    stop_cmd: str = ""

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "host": self.host,
            "probe_kind": self.probe_kind,
            "probe_value": self.probe_value,
            "running": self.running,
            "reachable": self.reachable,
            "detail": self.detail,
            "start_cmd": self.start_cmd,
            "stop_cmd": self.stop_cmd,
        }


def _probe_tcp(host: str, port: int, transport: Transport) -> tuple[bool, str]:
    """Probe a TCP port on the target host. We ssh TO `host` and check its
    OWN localhost port (the service binds there), so use 127.0.0.1 — /dev/tcp
    needs an IP, not the ssh alias."""
    cmd = f"timeout 4 bash -c 'cat < /dev/null > /dev/tcp/127.0.0.1/{port}' && echo OPEN || echo CLOSED"
    rc, out = transport("ssh", host, cmd)
    if "OPEN" in out:
        return True, "open"
    return False, (out.strip() or f"rc={rc}")


def _probe_unit(host: str, unit: str, transport: Transport) -> tuple[bool, str]:
    cmd = f"systemctl is-active {shlex.quote(unit)} 2>/dev/null || echo INACTIVE"
    rc, out = transport("ssh", host, cmd)
    active = "active" in out.lower()
    return active, out.strip() or f"rc={rc}"


def probe_service(name: str, transport: Optional[Transport] = None) -> ServiceState:
    if transport is None:
        transport = _ssh_transport
    spec = SERVICES.get(name)
    if not spec:
        return ServiceState(name=name, host="?", probe_kind="?", probe_value="?",
                             detail="unknown service")
    kind, val = spec["probe"]
    try:
        if kind == "tcp":
            running, detail = _probe_tcp(spec["host"], int(val), transport)
        else:
            running, detail = _probe_unit(spec["host"], val, transport)
    except Exception as e:  # dead host / timeout -> soft-fail, never crash the HUD
        return ServiceState(name=name, host=spec["host"], probe_kind=kind,
                             probe_value=str(val), running=False, reachable=False,
                             detail=f"unreachable: {type(e).__name__}",
                             start_cmd=spec.get("start", ""), stop_cmd=spec.get("stop", ""))
    if "CLOSED" in detail or "INACTIVE" in detail:
        running = False
    return ServiceState(
        name=name, host=spec["host"], probe_kind=kind, probe_value=str(val),
        running=running, reachable=True, detail=detail,
        start_cmd=spec.get("start", ""), stop_cmd=spec.get("stop", ""),
    )


def snapshot(transport: Optional[Transport] = None) -> dict:
    """Probe all known mesh services. transport defaults to real SSH.
    Never raises — a dead host yields running=False, not a crash."""
    if transport is None:
        transport = _ssh_transport
    states = [probe_service(n, transport).as_dict() for n in SERVICES]
    up = sum(1 for s in states if s["running"])
    return {"total": len(states), "up": up, "services": states}


def control_service(name: str, action: str, transport: Optional[Transport] = None) -> dict:
    """start | stop | restart a mesh service over SSH. Returns a result dict.
    action 'restart' = stop then start."""
    if transport is None:
        transport = _ssh_transport
    spec = SERVICES.get(name)
    if not spec:
        return {"ok": False, "name": name, "error": "unknown service"}
    if action == "restart":
        control_service(name, "stop", transport)
        action = "start"
    cmd = spec.get("start" if action == "start" else "stop")
    if not cmd:
        return {"ok": False, "name": name, "error": f"no {action} cmd"}
    rc, out = transport("ssh", spec["host"], cmd)
    return {"ok": rc == 0, "name": name, "action": action, "host": spec["host"],
            "rc": rc, "out": out.strip()[-200:]}


# --- real transport (used at runtime) ---
def _ssh_transport(method: str, target: str, cmd: str) -> tuple[int, str]:
    import subprocess

    if method != "ssh":
        raise ValueError(f"unsupported transport method {method}")
    full = ["ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes",
            "-o", "ServerAliveInterval=15", target, cmd]
    p = subprocess.run(full, capture_output=True, text=True, timeout=30)
    return p.returncode, p.stdout + p.stderr
