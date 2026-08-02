"""sshlink.py — reach aion instances on other machines over SSH.

Why a tunnel and not RPC-over-ssh
---------------------------------
The fleet already speaks HTTP/JSON (`remotes.RemoteClient` -> `RemoteServer`)
with a shared bearer token. That is fine on one machine, where the listener is
bound to loopback and the token is a formality. It is NOT fine across a
network: the body is cleartext, there is no server authentication at all, and
one secret is shared by every node -- compromise any node and you own the fleet.

Rather than reimplement TLS, PKI and certificate rotation inside aion, we let
SSH do the part SSH is good at. `ssh -N -L 127.0.0.1:LOCAL:127.0.0.1:REMOTE`
opens an encrypted, host-authenticated pipe, and then the existing client talks
plain HTTP to 127.0.0.1:LOCAL exactly as it always has.

That buys, for no new protocol code:
  - encryption and integrity
  - *server* authentication via known_hosts (the token authenticates the
    client; nothing before now authenticated the server)
  - per-peer identity: each controller gets its own key in the remote's
    authorized_keys, so one peer can be revoked without re-keying the fleet
  - capability restriction: `restrict,permitopen="127.0.0.1:8765"` in
    authorized_keys means the key can open exactly one forward and nothing
    else -- no shell, no other ports, no agent forwarding
  - the remote listener stays bound to 127.0.0.1. Nothing new is exposed.

The token stays. It is now a second factor rather than the only one: SSH says
"this machine is who it claims", the token says "this caller is a fleet member".

Threat model, stated plainly
----------------------------
  - Anyone with the peer's SSH private key can open the forward. Protect it
    like any other key; use a per-peer key, not your login key.
  - First-contact host key trust is TOFU by default (`accept-new`). That is a
    real, if narrow, MITM window. `AION_SSH_STRICT=yes` requires the host key
    to already be in known_hosts and fails otherwise -- use it if you can seed
    keys out of band.
  - A peer you connect to can see the token you send. Peers are trusted with
    fleet membership by definition; they are not trusted with your shell.

Everything here is stdlib + the system `ssh` binary. argv is always a list --
never a shell string.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .fleet import AION_HOME, BASE_PORT

PEERS_FILE = "peers.json"

# Local ports for tunnels are picked from here upward, so a `ss -ltn` shows at
# a glance which loopback ports belong to aion tunnels rather than to aion
# instances (which start at BASE_PORT).
TUNNEL_PORT_BASE = 8900


# ── Peer ─────────────────────────────────────────────────────────────────────
@dataclass
class SSHPeer:
    """An aion instance on another machine, reached over SSH.

    `remote_port` is the port `RemoteServer` listens on over there -- it is
    forwarded from that machine's *loopback*, so the remote never needs to bind
    a public interface.
    """
    id: str
    host: str
    user: str = ""
    ssh_port: int = 22
    remote_port: int = BASE_PORT
    key: str = ""                 # path to the private key for this peer
    label: str = ""
    enabled: bool = True

    def target(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host

    def as_dict(self) -> dict:
        return {
            "id": self.id, "host": self.host, "user": self.user,
            "ssh_port": self.ssh_port, "remote_port": self.remote_port,
            "key": self.key, "label": self.label, "enabled": self.enabled,
        }

    @staticmethod
    def from_dict(raw: dict) -> "SSHPeer":
        return SSHPeer(
            id=str(raw.get("id", "")).strip(),
            host=str(raw.get("host", "")).strip(),
            user=str(raw.get("user", "")).strip(),
            ssh_port=int(raw.get("ssh_port", 22) or 22),
            remote_port=int(raw.get("remote_port", BASE_PORT) or BASE_PORT),
            key=str(raw.get("key", "")).strip(),
            label=str(raw.get("label", "")).strip(),
            enabled=bool(raw.get("enabled", True)),
        )


# ── Validation ───────────────────────────────────────────────────────────────
# ssh parses argv positionally: ANY element that begins with "-" is read as an
# option, wherever it appears. A peer whose host is "-oProxyCommand=sh -c ..."
# therefore executes a command on THIS machine, even though we never touch a
# shell and always pass a list. A list protects against shell metacharacters;
# it does not protect against argv injection. This is the check that does.
_ID_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")
_HOST_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.:[]")
_USER_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")


def validate_peer(peer: SSHPeer) -> list[str]:
    """Every problem with this peer, empty list if it is safe to launch.

    Returns all problems rather than the first, so a misconfigured peer can be
    fixed in one pass instead of one round trip per typo.
    """
    problems: list[str] = []

    if not peer.id:
        problems.append("id is empty")
    elif set(peer.id) - _ID_OK:
        problems.append(f"id has characters outside [A-Za-z0-9-_.]: {peer.id!r}")

    if not peer.host:
        problems.append("host is empty")
    else:
        if peer.host.startswith("-"):
            problems.append(f"host starts with '-', which ssh reads as an option: {peer.host!r}")
        if set(peer.host) - _HOST_OK:
            problems.append(f"host has characters outside [A-Za-z0-9-_.:[]]: {peer.host!r}")

    if peer.user:
        if peer.user.startswith("-"):
            problems.append(f"user starts with '-', which ssh reads as an option: {peer.user!r}")
        if set(peer.user) - _USER_OK:
            problems.append(f"user has characters outside [A-Za-z0-9-_.]: {peer.user!r}")

    if peer.key:
        if peer.key.startswith("-"):
            problems.append(f"key path starts with '-': {peer.key!r}")
        elif not Path(peer.key).expanduser().exists():
            problems.append(f"key file not found: {peer.key}")

    for name, port in (("ssh_port", peer.ssh_port), ("remote_port", peer.remote_port)):
        if not (1 <= port <= 65535):
            problems.append(f"{name} out of range: {port}")

    return problems


# ── Store ────────────────────────────────────────────────────────────────────
def aion_home() -> Path:
    """`$AION_HOME` if set, else the module default.

    `fleet.AION_HOME` is a constant fixed at import, so it ignores the env var
    that `procgraph` honours. Reading the env here means `AION_HOME=/tmp/x
    aion.sh peers add ...` writes where it says it will -- it was silently
    editing the real config before, which is a bad way to find out.
    """
    env = os.environ.get("AION_HOME", "").strip()
    return Path(env).expanduser() if env else AION_HOME


def peers_path() -> Path:
    return aion_home() / PEERS_FILE


def load_peers() -> list[SSHPeer]:
    """Configured SSH peers. Invalid entries are dropped, not raised: one bad
    line in a config file must not take the whole cockpit down."""
    path = peers_path()
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    items = raw.get("peers", raw) if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    out: list[SSHPeer] = []
    seen: set[str] = set()
    for entry in items:
        if not isinstance(entry, dict):
            continue
        peer = SSHPeer.from_dict(entry)
        if validate_peer(peer) or peer.id in seen:
            continue
        seen.add(peer.id)
        out.append(peer)
    return out


def save_peers(peers: list[SSHPeer]) -> None:
    from .fleet import write_json_atomic
    write_json_atomic(peers_path(), {"peers": [p.as_dict() for p in peers]})


# ── argv ─────────────────────────────────────────────────────────────────────
def strict_host_key_checking() -> str:
    """`yes` requires the host key to be known already; `accept-new` trusts on
    first use but still refuses a CHANGED key. Never `no` -- that would accept
    a swapped key silently, which is the whole attack."""
    if os.environ.get("AION_SSH_STRICT", "").strip().lower() in {"1", "yes", "true"}:
        return "yes"
    return "accept-new"


def build_argv(peer: SSHPeer, local_port: int) -> list[str]:
    """The exact ssh command for this tunnel. Pure -- spawns nothing.

    Every option here is load-bearing:

      -N                     no remote command; this is a pipe, not a login
      -T                     no pty
      ExitOnForwardFailure   without it ssh stays up having bound NOTHING, and
                             the client's connection-refused looks like a dead
                             peer instead of a dead forward
      BatchMode              never prompt. A daemon cannot answer a password
                             prompt; it can only hang forever waiting to
      IdentitiesOnly         offer only this peer's key. Otherwise ssh walks
                             the agent and shows every identity you hold to a
                             machine that only needs one
      ServerAlive*           notice a dead peer in ~30s instead of never
      -L 127.0.0.1:...       bind the LOCAL end to loopback explicitly. A bare
                             `-L port:` honours GatewayPorts, and re-exposing
                             the fleet on the LAN is exactly what the tunnel
                             exists to avoid
      ...:127.0.0.1:remote   the REMOTE end resolves on the remote host, so the
                             remote listener stays loopback-bound too

    There is deliberately NO `ClearAllForwardings=yes`, despite it looking like
    the obvious hygiene against forwardings inherited from ~/.ssh/config:
    `ssh -G` shows it deletes the `-L` on our own command line too, so the
    tunnel would come up forwarding nothing and fail only on timeout. The
    user's ssh config is inherited on purpose anyway -- ProxyJump through a
    bastion is a legitimate way to reach a peer.
    """
    argv = [
        "ssh", "-N", "-T",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "BatchMode=yes",
        "-o", f"StrictHostKeyChecking={strict_host_key_checking()}",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=2",
        "-o", "ConnectTimeout=10",
        "-o", "ForwardAgent=no",
        "-o", "ForwardX11=no",
    ]
    if peer.key:
        argv += ["-o", "IdentitiesOnly=yes", "-i", str(Path(peer.key).expanduser())]
    if peer.ssh_port != 22:
        argv += ["-p", str(peer.ssh_port)]
    argv += ["-L", f"127.0.0.1:{local_port}:127.0.0.1:{peer.remote_port}"]
    # "--" ends option parsing. Belt and braces: validate_peer already refuses
    # a leading dash, and this makes the argv safe even if that check is ever
    # bypassed by a caller constructing SSHPeer directly.
    argv += ["--", peer.target()]
    return argv


def authorized_keys_line(pubkey: str, remote_port: int = BASE_PORT,
                         source: str = "") -> str:
    """The line to append to the REMOTE machine's ~/.ssh/authorized_keys.

    `restrict` disables everything (pty, agent/X11/port forwarding, user rc)
    and then `permitopen` re-enables exactly one thing: a forward to the aion
    listener on that machine's loopback. A key installed this way cannot get a
    shell, cannot reach any other port, and cannot be used to pivot into the
    network behind the peer -- it can talk to aion or nothing.

    `from=` pins the source address when the controller has a stable one.
    """
    opts = ["restrict", f'permitopen="127.0.0.1:{remote_port}"']
    if source:
        opts.insert(0, f'from="{source}"')
    return f"{','.join(opts)} {pubkey.strip()}"


# ── Ports ────────────────────────────────────────────────────────────────────
def free_local_port(start: int = TUNNEL_PORT_BASE, span: int = 200) -> int:
    """A loopback port nothing is listening on.

    Probes rather than binding-and-holding, so there is a race between the
    probe and ssh binding. ExitOnForwardFailure turns that race into a clean
    startup failure we can retry, instead of a tunnel that silently isn't one.
    """
    for port in range(start, start + span):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            # Deliberately NOT SO_REUSEADDR: that option makes the probe accept
            # a port still in TIME_WAIT, which ssh — binding without it — then
            # refuses. "Free" has to mean free for the process that will
            # actually bind it, not for the one asking.
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise OSError(f"no free loopback port in {start}..{start + span}")


def tunnels_dir() -> Path:
    return aion_home() / "tunnels"


def _record(peer_id: str, pid: int, local_port: int, forward: str = "") -> None:
    try:
        tunnels_dir().mkdir(parents=True, exist_ok=True)
        (tunnels_dir() / f"{peer_id}.json").write_text(json.dumps(
            {"pid": pid, "local_port": local_port, "forward": forward}))
    except OSError:
        pass          # bookkeeping, never worth failing a tunnel over


def _forget(peer_id: str) -> None:
    try:
        (tunnels_dir() / f"{peer_id}.json").unlink()
    except OSError:
        pass


def _cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().decode(
            errors="replace").replace("\0", " ")
    except OSError:
        return ""


def reap_orphans() -> list[str]:
    """Kill tunnels left behind by a cockpit that died without cleaning up.

    `start_new_session=True` detaches ssh so a Ctrl-C in the cockpit does not
    sever a request mid-flight -- but it also means a SIGKILLed daemon leaks
    live SSH sessions to remote machines, holding loopback ports, forever.
    Nothing ever collects them, because nothing knows they exist.

    A recorded pid is only killed if /proc still shows a process carrying the
    exact `-L` forward we recorded. A pid alone is not evidence: pids are
    reused, and killing the wrong process is a far worse bug than a leaked
    tunnel. The forward spec is the discriminator rather than argv[0], which
    varies (`ssh`, `/usr/bin/ssh`, a wrapper) and is empty for a zombie.
    """
    killed: list[str] = []
    directory = tunnels_dir()
    if not directory.exists():
        return killed
    for path in sorted(directory.glob("*.json")):
        try:
            raw = json.loads(path.read_text())
            pid, port = int(raw["pid"]), int(raw["local_port"])
            forward = str(raw.get("forward") or f"127.0.0.1:{port}:")
        except (OSError, ValueError, KeyError, TypeError):
            path.unlink(missing_ok=True)
            continue
        cmd = _cmdline(pid)
        if f"-L {forward}" in cmd and " -N " in f" {cmd} ":
            try:
                os.kill(pid, 15)
                killed.append(f"{path.stem} (pid {pid}, port {port})")
            except OSError:
                pass
        path.unlink(missing_ok=True)
    return killed


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    """Is something accepting on this port right now?"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ── Tunnel ───────────────────────────────────────────────────────────────────
@dataclass
class TunnelState:
    """What the HUD needs to draw one tunnel."""
    peer_id: str
    local_port: int = 0
    up: bool = False
    since: float = 0.0
    attempts: int = 0
    last_error: str = ""

    def age_s(self) -> float:
        return time.time() - self.since if self.since else 0.0


class SSHTunnel:
    """One `ssh -L` child process, owned and supervised.

    The process is the tunnel: if it dies the forward dies with it, so liveness
    is `poll() is None` plus an actual connect to the local end. Checking only
    the pid would call a half-open tunnel healthy.
    """

    def __init__(self, peer: SSHPeer, local_port: int = 0,
                 ssh_bin: str = "ssh") -> None:
        self.peer = peer
        self.local_port = local_port
        self.ssh_bin = ssh_bin
        self.proc: subprocess.Popen | None = None
        self.state = TunnelState(peer_id=peer.id, local_port=local_port)

    def argv(self) -> list[str]:
        argv = build_argv(self.peer, self.local_port)
        argv[0] = self.ssh_bin
        return argv

    def start(self) -> bool:
        """Spawn ssh. Returns False with `state.last_error` set on refusal."""
        problems = validate_peer(self.peer)
        if problems:
            self.state.last_error = "; ".join(problems)
            return False
        if shutil.which(self.ssh_bin) is None:
            self.state.last_error = f"{self.ssh_bin} not found on PATH"
            return False
        if not self.local_port:
            try:
                self.local_port = free_local_port()
            except OSError as exc:
                self.state.last_error = str(exc)
                return False
        self.state.local_port = self.local_port
        self.state.attempts += 1
        try:
            self.proc = subprocess.Popen(
                self.argv(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,   # Ctrl-C in the cockpit is not ssh's business
            )
        except OSError as exc:
            self.state.last_error = str(exc)
            return False
        self.state.since = time.time()
        self.state.last_error = ""
        _record(self.peer.id, self.proc.pid, self.local_port,
                f"127.0.0.1:{self.local_port}:127.0.0.1:{self.peer.remote_port}")
        return True

    def alive(self) -> bool:
        """Process running AND the local end actually accepting."""
        if self.proc is None or self.proc.poll() is not None:
            self.state.up = False
            return False
        self.state.up = port_open(self.local_port)
        return self.state.up

    def wait_ready(self, timeout: float = 8.0, interval: float = 0.1) -> bool:
        """Block until the forward answers, ssh exits, or we give up.

        ssh binds the local port before it reports anything, and reports
        nothing at all with -N, so polling the socket is the only honest
        readiness signal available.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                self.state.last_error = self._drain_stderr() or "ssh exited"
                self.state.up = False
                return False
            if port_open(self.local_port):
                self.state.up = True
                return True
            time.sleep(interval)
        self.state.last_error = "timed out waiting for the forward"
        self.state.up = False
        return False

    def stop(self) -> None:
        if self.proc is None:
            return
        try:
            self.proc.terminate()
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        except OSError:
            pass
        self.proc = None
        self.state.up = False
        _forget(self.peer.id)

    def _drain_stderr(self) -> str:
        """ssh's own diagnosis, first line only.

        The stderr of a failed ssh is the single most useful thing here
        ("Permission denied (publickey)", "Host key verification failed") and
        the reason peers were previously undebuggable from inside aion.
        """
        if self.proc is None or self.proc.stderr is None:
            return ""
        try:
            raw = self.proc.stderr.read() or b""
        except (OSError, ValueError):
            return ""
        for line in raw.decode(errors="replace").splitlines():
            line = line.strip()
            if line and not line.lower().startswith("warning: permanently added"):
                return line
        return ""


# ── Pool ─────────────────────────────────────────────────────────────────────
def retry_delay(attempts: int) -> float:
    """Seconds to wait before retrying a peer that failed `attempts` times.

    POLICY, deliberately isolated so it can be changed without touching the
    supervisor. The default is exponential backoff capped at a minute: a peer
    that is off (a laptop, a shut-down workstation) must not generate a
    connection per second forever, but one that reboots must come back without
    anybody restarting the cockpit -- so it never stops retrying, it just slows.
    """
    return min(60.0, 2.0 ** min(attempts, 6))


class TunnelPool:
    """Keeps one tunnel per enabled peer, and heals them.

    Deliberately synchronous and poll-driven: the cockpit already has a tick,
    and a supervisor with its own thread would need locking around the same
    process table for no gain.
    """

    def __init__(self, ssh_bin: str = "ssh", reap: bool = True) -> None:
        self.ssh_bin = ssh_bin
        self.tunnels: dict[str, SSHTunnel] = {}
        self._next_try: dict[str, float] = {}
        # Whatever the previous cockpit left running is not ours to inherit,
        # and it is holding a port we are about to want.
        self.reaped = reap_orphans() if reap else []

    def ensure(self, peer: SSHPeer, wait: float = 8.0) -> TunnelState:
        """Bring this peer's tunnel up if it is not, and report the state."""
        tunnel = self.tunnels.get(peer.id)
        if tunnel is not None and tunnel.alive():
            return tunnel.state

        now = time.time()
        if now < self._next_try.get(peer.id, 0.0):
            # Still in backoff. Report the last failure rather than hammering.
            return tunnel.state if tunnel else TunnelState(
                peer_id=peer.id, last_error="backing off")

        if tunnel is not None:
            tunnel.stop()
        tunnel = SSHTunnel(peer, ssh_bin=self.ssh_bin)
        tunnel.state.attempts = self._attempts(peer.id)
        self.tunnels[peer.id] = tunnel

        if tunnel.start() and tunnel.wait_ready(timeout=wait):
            self._next_try.pop(peer.id, None)
            tunnel.state.attempts = 0
            return tunnel.state
        self._next_try[peer.id] = now + retry_delay(tunnel.state.attempts)
        return tunnel.state

    def ensure_all(self, peers: list[SSHPeer] | None = None,
                   wait: float = 8.0) -> list[TunnelState]:
        peers = load_peers() if peers is None else peers
        states = [self.ensure(p, wait=wait) for p in peers if p.enabled]
        # A peer removed from the config must not keep a forward open.
        live = {p.id for p in peers if p.enabled}
        for pid in [k for k in self.tunnels if k not in live]:
            self.tunnels.pop(pid).stop()
            self._next_try.pop(pid, None)
        return states

    def local_port(self, peer_id: str) -> int:
        """Where to send HTTP for this peer, 0 if the tunnel is not up."""
        tunnel = self.tunnels.get(peer_id)
        return tunnel.local_port if tunnel and tunnel.alive() else 0

    def states(self) -> list[TunnelState]:
        for tunnel in self.tunnels.values():
            tunnel.alive()
        return [t.state for t in self.tunnels.values()]

    def close_all(self) -> None:
        for tunnel in self.tunnels.values():
            tunnel.stop()
        self.tunnels.clear()
        self._next_try.clear()

    def _attempts(self, peer_id: str) -> int:
        old = self.tunnels.get(peer_id)
        return old.state.attempts if old else 0


# ── Bridge to the existing fleet ─────────────────────────────────────────────
def to_remote_node(peer: SSHPeer, local_port: int):
    """An `remotes.RemoteNode` aimed at the local end of this peer's tunnel.

    This is the whole point of the design: past here, nothing in aion knows or
    cares that the instance is on another machine. Routing, gates, status polls
    and cancellation all use the transport they already used.
    """
    from .remotes import RemoteNode
    return RemoteNode(
        id=peer.id,
        host="127.0.0.1",
        port=local_port,
        label=peer.label or peer.host,
    )


def describe(peers: list[SSHPeer] | None = None,
             pool: TunnelPool | None = None) -> list[dict]:
    """JSON-ready rows for the HUD: one per configured peer, tunnel or not."""
    peers = load_peers() if peers is None else peers
    by_id = {s.peer_id: s for s in (pool.states() if pool else [])}
    rows = []
    for peer in peers:
        state = by_id.get(peer.id)
        rows.append({
            "id": peer.id,
            "label": peer.label or peer.host,
            "target": peer.target(),
            "ssh_port": peer.ssh_port,
            "remote_port": peer.remote_port,
            "enabled": peer.enabled,
            "up": bool(state and state.up),
            "local_port": state.local_port if state else 0,
            "attempts": state.attempts if state else 0,
            "error": state.last_error if state else "",
            "uptime_s": round(state.age_s(), 1) if state and state.up else 0.0,
        })
    return rows
