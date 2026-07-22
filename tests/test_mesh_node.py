"""Security invariants for the mesh node (src/aion/agents/node.py).

node.py is a standalone deployable (not part of the aion package), so it's
loaded by path. These lock the fixes for the flagged vulns: no SSH material is
collected/advertised, the endpoint binds fail-closed without a token, and
peer-reported data is whitelisted (no ssh_*/hostname injection).
"""
import importlib.util
from pathlib import Path

import pytest

NODE_PY = Path(__file__).resolve().parents[1] / "src" / "aion" / "agents" / "node.py"


@pytest.fixture
def mesh(monkeypatch):
    monkeypatch.delenv("MESH_TOKEN", raising=False)
    spec = importlib.util.spec_from_file_location("meshnode_under_test", NODE_PY)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_own_node_advertises_no_ssh_or_hostname(mesh):
    own = mesh._own_node()
    assert not any(k.startswith("ssh") or k == "hostname" for k in own)
    assert set(own) == {"host", "agent_type", "port", "status", "source", "last_seen"}


def test_no_ssh_harvest_function(mesh):
    # the ~/.ssh/id_*.pub scraper must be gone entirely
    assert not hasattr(mesh, "_get_ssh_pub_key")


def test_bind_is_fail_closed_without_token(mesh):
    assert mesh.MESH_TOKEN == ""
    assert mesh._bind_host() == "127.0.0.1"


def test_bind_opens_only_with_token(mesh):
    mesh.MESH_TOKEN = "s3cret"
    assert mesh._bind_host() == "0.0.0.0"


def test_peer_node_ssh_fields_are_stripped(mesh):
    poisoned = {
        "host": "192.168.1.9", "agent_type": "aion", "port": 8765,
        "status": "online", "last_seen": "t",
        "ssh_user": "root", "ssh_pub_key": "ssh-ed25519 AAAA... attacker",
        "hostname": "evil", "source": "self",
    }
    safe = mesh._sanitize_peer_node(poisoned, my_ip="192.168.1.1")
    assert safe == {
        "host": "192.168.1.9", "agent_type": "aion", "port": 8765,
        "status": "online", "last_seen": "t", "source": "peer",
    }
    assert "ssh_user" not in safe and "ssh_pub_key" not in safe and "hostname" not in safe


def test_peer_node_dropped_when_self_or_malformed(mesh):
    assert mesh._sanitize_peer_node({"host": "10.0.0.5"}, my_ip="10.0.0.5") is None
    assert mesh._sanitize_peer_node({"no_host": 1}, my_ip="10.0.0.5") is None
    assert mesh._sanitize_peer_node("not-a-dict", my_ip="10.0.0.5") is None
