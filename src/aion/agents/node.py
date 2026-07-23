#!/usr/bin/env python3
"""Mesh node: discovery + peer exchange + SSH keys + inventory.
Each node advertises identity, hardware, stored data, and services."""

import hmac, json, os, platform, re, socket, subprocess, sys, threading, time
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

PEER_PORT = 18765
SCAN_RANGE = os.getenv("NODE_SCAN_RANGE", "192.168.1.0/24")
TIMEOUT = int(os.getenv("NODE_SCAN_TIMEOUT", "3"))
SCAN_INTERVAL = int(os.getenv("NODE_SCAN_INTERVAL", "300"))
STATE_DIR = Path(os.getenv("MESH_STATE_DIR", str(Path.home() / ".cache" / "aion-mesh")))
STATE_FILE = STATE_DIR / "nodes.json"
_KNOWN_FILE = STATE_DIR / "known_peers.json"
_CAP_FILE = STATE_DIR / "capabilities.json"

# Shared secret gating the peer endpoint. Unset => bind localhost only
# (fail-closed: no token, no LAN exposure). Set the SAME value on every node.
MESH_TOKEN = os.getenv("MESH_TOKEN", "")


def _bind_host():
    """0.0.0.0 only when a token is configured; else localhost (fail-closed)."""
    return "0.0.0.0" if MESH_TOKEN else "127.0.0.1"


def _sanitize_peer_node(n, my_ip):
    """Whitelist a peer-reported node: keep inventory, drop all SSH material.

    Inventory sharing (hardware/data/services/hostname/description) is the
    point of the mesh, so those pass. But ssh_user / ssh_pub_key / ssh_port are
    never trusted from a peer — that's an authorized_keys / recon injection
    vector. Returns the sanitized dict, or None to drop the entry.
    """
    if not isinstance(n, dict) or n.get("host") in (None, "", my_ip):
        return None
    safe = {k: v for k, v in n.items() if not k.startswith("ssh")}
    safe["source"] = "peer"
    return safe

_mesh_state = {"timestamp": "", "local_ip": "", "nodes": [], "peers_queried": []}
# Load last known state from disk so HTTP server always has data
try: _mesh_state.update(json.loads(STATE_FILE.read_text()))
except: pass

# ── helpers ──────────────────────────────────────────────────────────

def _load_known():
    try: return json.loads(_KNOWN_FILE.read_text()).get("peers", [])
    except: return []

def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try: s.connect(("8.8.8.8", 80)); return s.getsockname()[0]
    except: return "127.0.0.1"
    finally: s.close()

def port_open(ip, port, timeout=None):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout or TIMEOUT)
    r = s.connect_ex((ip, port)) == 0; s.close(); return r

def _read_capabilities():
    try:
        if _CAP_FILE.exists():
            return json.loads(_CAP_FILE.read_text())
    except: pass
    return {}

# ── hardware collector ──────────────────────────────────────────────

def _collect_hardware():
    info = {}
    info["os"] = f"{platform.system()} {platform.release()}"
    info["arch"] = platform.machine()
    info["hostname"] = socket.gethostname()

    # CPU
    cpu = {"cores": os.cpu_count() or 0}
    try:
        with open("/proc/cpuinfo") as f:
            m = re.search(r"model name\s+:\s+(.+)", f.read())
            if m: cpu["model"] = m.group(1).strip()
    except: pass
    try:
        out = subprocess.run(["nproc"], capture_output=True, text=True, timeout=5)
        if out.returncode == 0: cpu["cores"] = int(out.stdout.strip())
    except: pass
    try:
        out = subprocess.run(["lscpu"], capture_output=True, text=True, timeout=5)
        for line in out.stdout.splitlines():
            m = re.match(r"Model name:\s+(.+)", line)
            if m: cpu["model"] = m.group(1).strip(); break
    except: pass
    info["cpu"] = cpu

    # RAM
    try:
        with open("/proc/meminfo") as f:
            d = f.read()
            t = re.search(r"MemTotal:\s+(\d+)", d)
            a = re.search(r"MemAvailable:\s+(\d+)", d)
            if t: info["ram"] = {"total_kb": int(t.group(1)),
                                 "available_kb": int(a.group(1)) if a else 0}
    except: pass

    # Disk
    disks = []
    for p in ["/", "/home", "/usr/data"]:
        try:
            s = os.statvfs(p)
            total = s.f_blocks * s.f_frsize
            free = s.f_bfree * s.f_frsize
            disks.append({"mount": p, "total_gb": round(total / 1e9, 1),
                          "free_gb": round(free / 1e9, 1)})
        except: pass
    if disks: info["disks"] = disks

    # Camera
    cams = []
    try:
        for d in os.listdir("/dev"):
            if re.match(r"video\d+", d): cams.append(f"/dev/{d}")
    except: pass
    if cams: info["cameras"] = cams

    # GPU
    try:
        out = subprocess.run(["lspci"], capture_output=True, text=True, timeout=5)
        gpus = [line for line in out.stdout.splitlines() if "VGA" in line or "3D" in line or "Graphics" in line]
        if gpus: info["gpu"] = gpus
    except: pass

    return info

# ── own node ─────────────────────────────────────────────────────────

def _own_node():
    # Inventory (hardware/data/services) is the mesh feature and is served only
    # behind MESH_TOKEN. No SSH user/key is collected or advertised — key
    # distribution must be explicit, never harvested and broadcast.
    cap = _read_capabilities()
    return {
        "host": local_ip(),
        "hostname": socket.gethostname(),
        "agent_type": "mesh-node",
        "port": PEER_PORT,
        "status": "online",
        "source": "self",
        "hardware": _collect_hardware(),
        "data": cap.get("data", {}),
        "services": cap.get("services", {}),
        "description": cap.get("description", ""),
        "last_seen": datetime.now().isoformat(),
    }

# ── scanning ─────────────────────────────────────────────────────────

def scan():
    my_ip = local_ip()
    own = _own_node()
    found = [own]

    try:
        out = subprocess.run(["nmap", "-sn", SCAN_RANGE, "-oG", "-"],
            capture_output=True, text=True, timeout=90).stdout
        live = [line.split()[1] for line in out.splitlines()
                if "Up" in line and "Host:" in line and line.split()[1] != my_ip]
    except:
        live = []

    for ip in live:
        for agent, port in [("hermes", 8732), ("opencode", 9876), ("aion", 8765)]:
            if port_open(ip, port):
                found.append({"host": ip, "port": port, "agent_type": agent,
                    "status": "online", "source": "direct",
                    "last_seen": datetime.now().isoformat()})
        if port_open(ip, 22):
            found.append({"host": ip, "agent_type": "ssh-host", "port": 22,
                "status": "online", "source": "direct",
                "last_seen": datetime.now().isoformat()})

    for ip in _load_known():
        if ip == my_ip: continue
        for agent, port in [("hermes", 8732), ("opencode", 9876), ("aion", 8765)]:
            k = f"{ip}:{agent}"
            if k not in {f"{n['host']}:{n.get('agent_type','')}" for n in found} and port_open(ip, port):
                found.append({"host": ip, "port": port, "agent_type": agent,
                    "status": "online", "source": "known",
                    "last_seen": datetime.now().isoformat()})

    peers = [ip for ip in live if port_open(ip, PEER_PORT)]
    for ip in _load_known():
        if ip != my_ip and ip not in peers and port_open(ip, PEER_PORT):
            peers.append(ip)

    auth = f"Authorization: Bearer {MESH_TOKEN}\r\n" if MESH_TOKEN else ""
    for ip in peers:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(TIMEOUT)
            if s.connect_ex((ip, PEER_PORT)) == 0:
                s.sendall(f"GET /nodes HTTP/1.0\r\nHost: localhost\r\n{auth}\r\n"
                          .encode())
                data = b""
                while True:
                    chunk = s.recv(4096)
                    if not chunk: break
                    data += chunk
                s.close()
                body = data.split(b"\r\n\r\n", 1)[-1] if b"\r\n\r\n" in data else data
                for n in json.loads(body).get("nodes", []):
                    safe = _sanitize_peer_node(n, my_ip)   # strips ssh_*
                    if safe is None: continue
                    k = f"{safe['host']}:{safe.get('agent_type','unknown')}"
                    merged = False
                    for i, x in enumerate(found):
                        if f"{x['host']}:{x.get('agent_type','unknown')}" == k:
                            found[i].update(safe)
                            merged = True
                            break
                    if not merged:
                        found.append(safe)
        except:
            pass

    seen = set(); deduped = []
    for n in found:
        k = f"{n['host']}:{n.get('agent_type','unknown')}"
        if k not in seen: seen.add(k); deduped.append(n)

    _mesh_state.update({
        "timestamp": datetime.now().isoformat(),
        "local_ip": my_ip,
        "nodes": deduped,
        "peers_queried": peers,
    })
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(_mesh_state, indent=2))

# ── HTTP server ──────────────────────────────────────────────────────

class MeshHandler(BaseHTTPRequestHandler):
    def _authed(self):
        if not MESH_TOKEN:            # localhost-only server; local reads pass
            return True
        got = self.headers.get("Authorization", "")
        return hmac.compare_digest(got, f"Bearer {MESH_TOKEN}")

    def do_GET(self):
        if self.path != "/nodes":
            self.send_response(404); self.end_headers(); return
        if not self._authed():
            self.send_response(401); self.end_headers(); return
        body = json.dumps(_mesh_state).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass

def _serve():
    host = _bind_host()
    if host != "0.0.0.0":
        sys.stderr.write("[mesh] MESH_TOKEN unset — peer endpoint bound to "
                         "127.0.0.1 only (set MESH_TOKEN to expose on the LAN)\n")
    return HTTPServer((host, PEER_PORT), MeshHandler)

def daemon():
    threading.Thread(target=_serve().serve_forever, daemon=True).start()
    while True:
        scan(); time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    if "--daemon" in sys.argv: daemon()
    else:
        threading.Thread(target=_serve().serve_forever, daemon=True).start()
        scan()
        sys.exit(0 if _mesh_state["nodes"] else 1)
