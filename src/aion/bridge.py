"""bridge.py — instance-to-instance WebSocket relay for aion.

Connects two (or more) running aion instances so they share bus events:
task lifecycle, stats, logs, and optionally intents. Symmetric design:
each instance listens for incoming peers AND connects to configured outbound
peers at the same time. Messages carry an origin + sequence number for
dedup — no echo loops.

Transport is WebSocket (the `websockets` library from the `web` optional dep).
No custom daemon, no ssh: just a lightweight relay that bridges the in-process
Bus to a multiprocess fabric.

Config: config/bridge.json
```json
{
  "listen_host": "0.0.0.0",
  "listen_port": 9876,
  "instance_name": "air",
  "peers": [
    {"host": "laptop-b.tail-scale-net.ts.net", "port": 9876}
  ],
  "relay_topics": ["task", "stats", "log", "intent"]
}
```
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("aion.bridge")

# Default config
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 9876
DEFAULT_NAME = "aion"

# Max messages queued per peer before we start dropping (oldest-first)
PEER_QUEUE_MAX = 512

# Reconnect backoff: 1s, 2s, 4s, 8s, 16s, 30s, 60s (capped)
RECONNECT_BASE_S = 1.0
RECONNECT_MAX_S = 60.0

# Bus topic for bridge health / peer status
TOPIC_BRIDGE = "bridge"

# ---------------------------------------------------------------------------
# wire protocol: JSON frame
# ---------------------------------------------------------------------------
@dataclass
class BridgeMessage:
    """One relayed bus event, serialised over the wire."""

    topic: str
    payload: Any
    origin: str            # instance_name of the sender
    seq: int               # monotonic counter, per-instance
    ts: float              # unix timestamp

    def to_json(self) -> str:
        return json.dumps({
            "topic": self.topic,
            "payload": self.payload,
            "origin": self.origin,
            "seq": self.seq,
            "ts": self.ts,
        }, default=str)

    @classmethod
    def from_json(cls, raw: str) -> "BridgeMessage":
        d = json.loads(raw)
        return cls(
            topic=d["topic"],
            payload=d.get("payload"),
            origin=d.get("origin", "?"),
            seq=d.get("seq", 0),
            ts=d.get("ts", 0.0),
        )


# ---------------------------------------------------------------------------
# peer connection (outbound)
# ---------------------------------------------------------------------------
class _PeerConnection:
    """Manages one outbound WebSocket connection to a peer, with reconnection."""

    def __init__(self, host: str, port: int, *, loop: asyncio.AbstractEventLoop,
                 send_queue: asyncio.Queue) -> None:
        self.host = host
        self.port = port
        self._loop = loop
        self._send_queue = send_queue
        self._ws = None
        self._closed = False
        self._task: asyncio.Task | None = None

    @property
    def label(self) -> str:
        return f"{self.host}:{self.port}"

    async def run(self) -> None:
        """Connect + pump loop. Never raises: reconnects forever."""
        delay = RECONNECT_BASE_S
        while not self._closed:
            try:
                async with await self._connect() as ws:
                    self._ws = ws
                    delay = RECONNECT_BASE_S  # reset on success
                    logger.info("[bridge] connected to peer %s", self.label)
                    await self._pump(ws)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                level = logging.WARNING if delay <= RECONNECT_BASE_S else logging.DEBUG
                logger.log(level, "[bridge] peer %s error: %s; reconnecting in %.1fs",
                           self.label, exc, delay)
            self._ws = None
            if not self._closed:
                await asyncio.sleep(delay)
                delay = min(delay * 2, RECONNECT_MAX_S)
        logger.info("[bridge] peer %s closed", self.label)

    async def _connect(self):
        import websockets
        uri = f"ws://{self.host}:{self.port}"
        return await websockets.connect(uri, ping_interval=30, ping_timeout=10)

    async def _pump(self, ws) -> None:
        """Read from the send queue and write frames."""
        while not self._closed:
            msg = await self._send_queue.get()
            if msg is None:  # sentinel — shutdown
                return
            try:
                await ws.send(msg)
            except Exception:
                # push back for reconnection retry
                await asyncio.sleep(0)
                self._send_queue.put_nowait(msg)
                raise

    def close(self) -> None:
        self._closed = True
        if self._task:
            self._task.cancel()
        # unblock the pump with a sentinel
        self._send_queue.put_nowait(None)


# ---------------------------------------------------------------------------
# incoming connection handler
# ---------------------------------------------------------------------------
async def _handle_peer(ws, bridge: "AionBridge") -> None:
    """One incoming WebSocket connection from a remote instance."""
    remote_addr = ws.remote_address
    logger.info("[bridge] incoming peer from %s:%s", *remote_addr)
    try:
        async for raw in ws:
            try:
                msg = BridgeMessage.from_json(raw)
            except (json.JSONDecodeError, KeyError) as exc:
                logger.warning("[bridge] bad frame from %s: %s", remote_addr, exc)
                continue
            # Skip messages we originated (echo guard via origin)
            if msg.origin == bridge.instance_name:
                continue
            # Publish onto the local bus
            enriched = {**msg.payload, "_origin": msg.origin, "_seq": msg.seq} if isinstance(msg.payload, dict) else msg.payload
            await bridge.bus.publish(msg.topic, enriched)
    except Exception as exc:
        logger.debug("[bridge] peer %s disconnected: %s", remote_addr, exc)


# ---------------------------------------------------------------------------
# AionBridge — the main class
# ---------------------------------------------------------------------------
class AionBridge:
    """Relays bus events between aion instances over WebSocket.

    Usage (wired in AiOSApp.on_mount):
        cfg = load_bridge_config()
        if cfg:
            bridge = AionBridge(bus, cfg)
            asyncio.create_task(bridge.start())
    """

    def __init__(self, bus: Any, config: dict) -> None:
        self.bus = bus
        self.instance_name = config.get("instance_name", DEFAULT_NAME)
        self.listen_host = config.get("listen_host", DEFAULT_HOST)
        self.listen_port = config.get("listen_port", DEFAULT_PORT)
        self.relay_topics = config.get("relay_topics",
                                       ["task", "stats", "log"])

        self._seq = 0
        self._server: Any | None = None
        self._peers: list[_PeerConnection] = []
        self._peer_queues: list[asyncio.Queue] = []
        self._sub_unsubs: list[Callable] = []
        self._running = False

    # ---- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        """Start the bridge: listen for inbound, connect to outbound peers,
        subscribe to relay topics on the bus."""
        if self._running:
            return
        self._running = True

        import websockets

        # 1) WebSocket server
        try:
            self._server = await websockets.serve(
                lambda ws: _handle_peer(ws, self),
                self.listen_host, self.listen_port,
                ping_interval=30, ping_timeout=10,
            )
            logger.info("[bridge] listening on %s:%s",
                        self.listen_host, self.listen_port)
        except OSError as exc:
            logger.warning("[bridge] cannot listen on %s:%s: %s",
                           self.listen_host, self.listen_port, exc)

        # 2) Outbound peer connections
        for peer in self._get_peers_from_config():
            q: asyncio.Queue = asyncio.Queue(maxsize=PEER_QUEUE_MAX)
            pc = _PeerConnection(peer["host"], peer["port"],
                                 loop=asyncio.get_event_loop(),
                                 send_queue=q)
            pc._task = asyncio.create_task(pc.run())
            self._peers.append(pc)
            self._peer_queues.append(q)

        # 3) Subscribe to bus topics for relay
        for topic in self.relay_topics:
            unsub = self.bus.subscribe(topic, self._make_relay(topic))
            self._sub_unsubs.append(unsub)

        # 4) Initial health status
        await self._publish_health()

    async def stop(self) -> None:
        """Shut down: close peers, server, and unsubscribe."""
        self._running = False
        for pc in self._peers:
            pc.close()
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        for unsub in self._sub_unsubs:
            unsub()
        self._peers.clear()
        self._peer_queues.clear()
        self._sub_unsubs.clear()
        logger.info("[bridge] stopped")

    # ---- internal ---------------------------------------------------------

    def _get_peers_from_config(self) -> list[dict]:
        """Resolve the 'peers' key — allow direct list or key under config."""
        raw = getattr(self, '_raw_config', None) or {}
        return raw.get("peers", [])

    def _make_relay(self, topic: str):
        """Return a bus subscriber that serialises and enqueues events.

        The closure captures the topic so a single publish() fans out to
        every connected peer via their send queue.
        """
        async def _relay(payload: Any) -> None:
            if not self._running or not self._peer_queues:
                return
            self._seq += 1
            msg = BridgeMessage(
                topic=topic,
                payload=payload,
                origin=self.instance_name,
                seq=self._seq,
                ts=time.time(),
            )
            raw = msg.to_json()
            for q in self._peer_queues:
                try:
                    q.put_nowait(raw)
                except asyncio.QueueFull:
                    # Drop oldest to keep the queue bounded
                    try:
                        q.get_nowait()
                        q.put_nowait(raw)
                    except asyncio.QueueEmpty:
                        pass
        return _relay

    async def _publish_health(self) -> None:
        """Emit bridge status on the bus so the UI can show peer panels."""
        peers = []
        for pc in self._peers:
            peers.append({
                "host": pc.host,
                "port": pc.port,
                "connected": pc._ws is not None,
            })
        await self.bus.publish(TOPIC_BRIDGE, {
            "action": "status",
            "instance_name": self.instance_name,
            "listen": f"{self.listen_host}:{self.listen_port}",
            "peers": peers,
            "relay_topics": self.relay_topics,
        })

    def set_config(self, raw_config: dict) -> None:
        """Store the raw config dict for peer resolution."""
        self._raw_config = raw_config


# ---------------------------------------------------------------------------
# config loading
# ---------------------------------------------------------------------------
def default_bridge_config_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "bridge.json"


def load_bridge_config(path: str | Path | None = None) -> dict | None:
    """Read config/bridge.json. Missing config = bridge disabled."""
    p = Path(path) if path else default_bridge_config_path()
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        return data
    except Exception as exc:
        logger.warning("[bridge] failed to load %s: %s", p, exc)
        return None


# ---------------------------------------------------------------------------
# convenience: wire into AiOSApp
# ---------------------------------------------------------------------------
async def start_bridge_from_config(bus: Any) -> AionBridge | None:
    """Load bridge.json and start the bridge. Returns None if no config."""
    cfg = load_bridge_config()
    if not cfg or not cfg.get("enabled", True):
        logger.debug("[bridge] disabled (no config or enabled=false)")
        return None
    bridge = AionBridge(bus, cfg)
    bridge.set_config(cfg)
    await bridge.start()
    return bridge