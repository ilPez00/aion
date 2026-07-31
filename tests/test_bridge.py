"""test_bridge.py — unit + integration tests for the aion bridge.

Tests the wire protocol, peer connection lifecycle, bus relay, echo
guard, and bounded queue behaviour. No network: websockets is mocked
via a fake transport so tests are fast and deterministic.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aion.bridge import (  # noqa: E402
    BridgeMessage,
    AionBridge,
    load_bridge_config,
    TOPIC_BRIDGE,
    _handle_peer,
)
from aion.core import Bus  # noqa: E402


# ---------------------------------------------------------------------------
# in-memory fake peer for bridge tests
# ---------------------------------------------------------------------------
class FakePeer:
    """Simulates one WebSocket peer using in-memory queues."""

    _EOF = "__EOF__"

    def __init__(self):
        self.sent: list[str] = []
        self.received: list[str] = []
        self._q: asyncio.Queue = asyncio.Queue()
        self.remote_address = ("127.0.0.1", 9999)

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        raw = await self._q.get()
        if raw == self._EOF:
            raise StopAsyncIteration
        return raw

    async def send(self, raw: str) -> None:
        self.sent.append(raw)

    async def close(self) -> None:
        self._q.put_nowait(self._EOF)

    def push(self, raw: str) -> None:
        self._q.put_nowait(raw)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _make_bridge(bus: Bus, **overrides) -> AionBridge:
    cfg = {
        "enabled": True,
        "instance_name": "test",
        "listen_host": "127.0.0.1",
        "listen_port": 0,
        "relay_topics": ["task", "stats", "log"],
        "peers": [],
    }
    cfg.update(overrides)
    bridge = AionBridge(bus, cfg)
    bridge.set_config(cfg)
    bridge._running = True  # bypass start() so relay works without network
    return bridge


async def _drain_bus():
    """Let Bus callback tasks run."""
    await asyncio.sleep(0)
    await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# wire protocol
# ---------------------------------------------------------------------------
def test_bridge_message_roundtrip():
    msg = BridgeMessage(
        topic="task",
        payload={"action": "create", "id": "t001"},
        origin="air",
        seq=42,
        ts=1234567890.0,
    )
    raw = msg.to_json()
    restored = BridgeMessage.from_json(raw)
    assert restored.topic == "task"
    assert restored.payload["id"] == "t001"
    assert restored.origin == "air"
    assert restored.seq == 42
    assert restored.ts == 1234567890.0


def test_bridge_message_handles_non_dict_payload():
    msg = BridgeMessage(topic="log", payload="hello", origin="air", seq=1, ts=0.0)
    raw = msg.to_json()
    restored = BridgeMessage.from_json(raw)
    assert restored.payload == "hello"


def test_bridge_message_missing_origin_defaults():
    raw = json.dumps({"topic": "task", "payload": {}, "seq": 1, "ts": 0.0})
    msg = BridgeMessage.from_json(raw)
    assert msg.origin == "?"


# ---------------------------------------------------------------------------
# relay — events are serialised and enqueued to peers
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_bridge_relays_task_events():
    bus = Bus()
    bridge = _make_bridge(bus)
    q: asyncio.Queue = asyncio.Queue()
    bridge._peer_queues.append(q)

    await bridge._make_relay("task")({"action": "create", "id": "t001"})
    raw = await asyncio.wait_for(q.get(), timeout=2.0)

    msg = BridgeMessage.from_json(raw)
    assert msg.topic == "task"
    assert msg.payload["id"] == "t001"
    assert msg.origin == "test"


@pytest.mark.asyncio
async def test_bridge_relays_stats_events():
    bus = Bus()
    bridge = _make_bridge(bus, relay_topics=["stats"])
    q: asyncio.Queue = asyncio.Queue()
    bridge._peer_queues.append(q)

    await bridge._make_relay("stats")({"harness": "demo", "cost_usd": 0.42})
    raw = await asyncio.wait_for(q.get(), timeout=2.0)
    msg = BridgeMessage.from_json(raw)
    assert msg.topic == "stats"
    assert msg.payload["cost_usd"] == 0.42


# ---------------------------------------------------------------------------
# echo guard — don't relay our own messages
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_bridge_echo_guard():
    bus = Bus()
    bridge = _make_bridge(bus, instance_name="air")
    fake_ws = FakePeer()
    fake_ws.push(json.dumps({
        "topic": "task",
        "payload": {"action": "create"},
        "origin": "air",  # same as bridge.instance_name → should be dropped
        "seq": 5,
        "ts": 0.0,
    }))
    fake_ws.push(fake_ws._EOF)

    received = []

    async def capture(payload):
        received.append(payload)

    bus.subscribe("task", capture)
    await _handle_peer(fake_ws, bridge)
    await _drain_bus()
    assert len(received) == 0, "self-originated message should be dropped"


@pytest.mark.asyncio
async def test_bridge_accepts_remote_messages():
    bus = Bus()
    bridge = _make_bridge(bus, instance_name="air")
    fake_ws = FakePeer()
    fake_ws.push(json.dumps({
        "topic": "task",
        "payload": {"action": "create", "id": "remote-001"},
        "origin": "forge",
        "seq": 12,
        "ts": 100.0,
    }))
    fake_ws.push(fake_ws._EOF)

    received = []

    async def capture(payload):
        received.append(payload)

    bus.subscribe("task", capture)
    await _handle_peer(fake_ws, bridge)
    await _drain_bus()
    assert len(received) == 1
    assert received[0]["_origin"] == "forge"
    assert received[0]["id"] == "remote-001"


# ---------------------------------------------------------------------------
# bounded queue — overflow drops oldest
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_bridge_queue_overflow_drops_oldest():
    bus = Bus()
    bridge = _make_bridge(bus)
    q: asyncio.Queue = asyncio.Queue(maxsize=2)
    bridge._peer_queues.append(q)

    relay = bridge._make_relay("task")
    await relay({"n": 1})
    await relay({"n": 2})
    assert q.qsize() == 2

    # This should drop the oldest (n=1) and push n=3
    await relay({"n": 3})
    assert q.qsize() == 2

    m1 = BridgeMessage.from_json(q.get_nowait())
    m2 = BridgeMessage.from_json(q.get_nowait())
    assert m1.payload["n"] == 2  # n=1 was dropped
    assert m2.payload["n"] == 3


# ---------------------------------------------------------------------------
# health publish
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_bridge_health_publish():
    bus = Bus()
    bridge = _make_bridge(bus, relay_topics=[])
    received = []

    async def capture(payload):
        received.append(payload)

    bus.subscribe(TOPIC_BRIDGE, capture)
    await bridge._publish_health()
    await _drain_bus()
    assert len(received) == 1
    assert received[0]["action"] == "status"
    assert received[0]["instance_name"] == "test"


# ---------------------------------------------------------------------------
# config loading
# ---------------------------------------------------------------------------
def test_load_bridge_config_missing():
    assert load_bridge_config("/nonexistent/path.json") is None


def test_load_bridge_config_invalid_json(tmp_path):
    p = tmp_path / "bridge.json"
    p.write_text("not json")
    assert load_bridge_config(p) is None


def test_load_bridge_config_valid(tmp_path):
    p = tmp_path / "bridge.json"
    p.write_text(json.dumps({
        "enabled": True,
        "instance_name": "test-box",
        "listen_port": 9876,
        "peers": [{"host": "10.0.0.2", "port": 9876}],
        "relay_topics": ["task"],
    }))
    cfg = load_bridge_config(p)
    assert cfg is not None
    assert cfg["instance_name"] == "test-box"
    assert len(cfg["peers"]) == 1


@pytest.mark.asyncio
async def test_start_bridge_from_config_no_config():
    """start_bridge_from_config returns None when no config."""
    from aion.bridge import start_bridge_from_config
    bus = Bus()
    result = await start_bridge_from_config(bus)
    assert result is None


# ---------------------------------------------------------------------------
# integration: bridge → bus → bridge end-to-end
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_bridge_full_flow():
    """Publishing on one bridge appears on another instance's bus."""
    bus_a = Bus()
    bus_b = Bus()

    bridge_a = _make_bridge(bus_a, instance_name="air")
    bridge_b = _make_bridge(bus_b, instance_name="forge")

    # Wire them via a shared in-memory queue (simulating WebSocket)
    q_ab: asyncio.Queue = asyncio.Queue()

    bridge_a._peer_queues.append(q_ab)

    received_by_b: list = []

    async def capture_b(payload):
        received_by_b.append(payload)

    bus_b.subscribe("task", capture_b)

    # A publishes a task event → it goes to A's peer queues
    await bridge_a._make_relay("task")({"action": "create", "id": "t001"})
    raw = await asyncio.wait_for(q_ab.get(), timeout=2.0)

    # B receives this as an incoming WebSocket message
    fake_ws_for_b = FakePeer()
    fake_ws_for_b.push(raw)
    fake_ws_for_b.push(fake_ws_for_b._EOF)
    await _handle_peer(fake_ws_for_b, bridge_b)
    await _drain_bus()

    assert len(received_by_b) == 1
    assert received_by_b[0]["_origin"] == "air"
    assert received_by_b[0]["id"] == "t001"