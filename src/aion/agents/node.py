#!/usr/bin/env python3
"""Mesh node discovery + peer exchange + SSH key distribution.
Each node advertises itself (hostname, IP, SSH user/port/pubkey) and
collects peer info from other mesh nodes to propagate across the LAN."""

import hmac, json, os, socket, subprocess, sys, threading, time
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

# Shared secret gating the peer endpoint. If unset, the server binds localhost
# only (fail-closed: no token => no LAN exposure). Set the SAME value on every
# mesh node so they can authenticate each other.
MESH_TOKEN = os.getenv("MESH_TOKEN", "")

# The only fields ever trusted from a remote peer's /nodes response. SSH
# material and hostname are never accepted from peers (poisoning / key-injection
# vector); each node reports its own facts locally via _own_node().
_SAFE_PEER_FIELDS = ("host", "agent_type", "port", "status", "last_seen")

_mesh_state = {"timestamp": "", "local_ip": "", "nodes": [], "peers_queried": []}


def _bind_host():
    """0.0.0.0 only when a token is configured; else localhost (fail-closed)."""
    return "0.0.0.0" if MESH_TOKEN else "127.0.0.1"


def _sanitize_peer_node(n, my_ip):
    """Whitelist a peer-reported node to safe fields, or None to drop it.

    A remote peer must never be able to inject ssh_user / ssh_pub_key / hostname
    into our state — those become an authorized_keys / recon vector downstream.
    """
    if not isinstance(n, dict) or n.get("host") in (None, "", my_ip):
        return None
    safe = {f: n[f] for f in _SAFE_PEER_FIELDS if f in n}
    if "host" not in safe:
        return None
    safe["source"] = "peer"
    return safe

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

def _own_node():
    # Discovery only: host + which agent + port. No SSH user/key/hostname is
    # collected or advertised — key distribution must be explicit, not harvested.
    return {
        "host": local_ip(),
        "agent_type": "mesh-node",
        "port": PEER_PORT,
        "status": "online",
        "source": "self",
        "last_seen": datetime.now().isoformat(),
    }

# ── scanning ─────────────────────────────────────────────────────────

def scan():
    my_ip = local_ip()
    own = _own_node()
    found = [own]

    # 1) nmap sweep for agent ports
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
        # note SSH availability (presence only — no user/key material)
        if port_open(ip, 22):
            found.append({"host": ip, "agent_type": "ssh-host", "port": 22,
                "status": "online", "source": "direct",
                "last_seen": datetime.now().isoformat()})

    # 2) known peers (from known_peers.json)
    for ip in _load_known():
        if ip == my_ip: continue
        for agent, port in [("hermes", 8732), ("opencode", 9876), ("aion", 8765)]:
            k = f"{ip}:{agent}"
            if k not in {f"{n['host']}:{n.get('agent_type','')}" for n in found} and port_open(ip, port):
                found.append({"host": ip, "port": port, "agent_type": agent,
                    "status": "online", "source": "known",
                    "last_seen": datetime.now().isoformat()})

    # 3) discover mesh peers (port 18765)
    peers = [ip for ip in live if port_open(ip, PEER_PORT)]
    for ip in _load_known():
        if ip != my_ip and ip not in peers and port_open(ip, PEER_PORT):
            peers.append(ip)

    # 4) peer exchange — fetch /nodes from each peer (authenticated)
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
                    safe = _sanitize_peer_node(n, my_ip)  # drops ssh_*/hostname
                    if safe is None:
                        continue
                    k = f"{safe['host']}:{safe.get('agent_type','unknown')}"
                    if k not in {f"{x['host']}:{x.get('agent_type','unknown')}" for x in found}:
                        found.append(safe)
        except:
            pass

    # dedup
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
        # No token configured => server is localhost-only, so local reads pass.
        if not MESH_TOKEN:
            return True
        got = self.headers.get("Authorization", "")
        want = f"Bearer {MESH_TOKEN}"
        return hmac.compare_digest(got, want)

    def do_GET(self):
        if self.path == "/nodes":
            if not self._authed():
                self.send_response(401); self.end_headers(); return
            body = json.dumps(_mesh_state).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)
        else: self.send_response(404); self.end_headers()
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
