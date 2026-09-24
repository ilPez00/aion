"""The Kubernetes provider's decision logic, tested against a canned inventory.

No cluster, no kubectl: `place()` is the part that can be silently wrong (a
wrong filter sends a gpu task to a driverless card, or counts a finished pod's
requests as still consumed), so that is what is pinned here.
"""
from __future__ import annotations

import json

import pytest

from aion import k8s


def node(name, *, ready=True, taints=(), labels=None, alloc=None, pods=()):
    return {
        "name": name, "known_to_fleet": True, "pod_count": len(pods), "pods": list(pods),
        "kubernetes": {
            "node_name": name, "ready": ready,
            "taints": [{"key": t, "effect": "NoSchedule"} for t in taints],
            "allocatable": alloc or {"cpu": "12", "memory": "32768000Ki"},
            "usage": {}, "randomesh_labels": labels if labels is not None else {
                "randomesh.io/gpu": "none", "randomesh.io/gpu-driver": "none"},
        },
    }


def pod(phase, mem="0", cpu="0"):
    return {"phase": phase, "resources": [{"requests": {"memory": mem, "cpu": cpu}}]}


def doc(*nodes):
    return {"clusters": {"lab": {"nodes": {n["kubernetes"]["node_name"]: n for n in nodes}}}}


def test_quantities():
    assert k8s._mi("1024Ki") == 1
    assert k8s._mi("2Gi") == 2048
    assert k8s._mi("512") == pytest.approx(0.000488, rel=1e-3)
    assert k8s._mi("garbage") == 0
    assert k8s._cores("500m") == 0.5
    assert k8s._cores("4") == 4


def test_needs_parse_and_reject():
    n = k8s.Needs.parse(["ram:2048", "cpu:2", "gpu", "label:randomesh.io/role=dev"])
    assert (n.ram_mb, n.cpu, n.gpu) == (2048, 2.0, True)
    assert n.labels == {"randomesh.io/role": "dev"}
    with pytest.raises(k8s.ProviderError):
        k8s.Needs.parse(["nonsense"])


def test_place_skips_notready_tainted_and_too_small():
    d = doc(
        node("notready", ready=False),
        node("tainted", taints=["node.kubernetes.io/disk-pressure"]),
        node("small", alloc={"cpu": "12", "memory": "1024Mi"}),
        node("big", alloc={"cpu": "12", "memory": "16Gi"}),
    )
    ranked = k8s.place(k8s.Needs.parse(["ram:4096"]), d)
    assert [r["node"] for r in ranked] == ["big"]


def test_place_does_not_count_finished_pods_as_consumed():
    d = doc(node("p", alloc={"cpu": "4", "memory": "4096Mi"},
                 pods=[pod("Succeeded", mem="3000Mi", cpu="3"),
                       pod("Running", mem="500Mi", cpu="1")]))
    ranked = k8s.place(k8s.Needs.parse(["ram:3000"]), d)
    assert [r["node"] for r in ranked] == ["p"]
    assert ranked[0]["free_mib"] == 3596          # 4096 - 500, not 4096 - 3500
    assert ranked[0]["free_cores"] == 3.0


def test_gpu_need_skips_a_card_with_no_driver():
    """pansa's Baffin enumerates but has no stack: gpu=amd, gpu-driver=none."""
    d = doc(node("pansa", labels={"randomesh.io/gpu": "amd",
                                  "randomesh.io/gpu-driver": "none"}))
    assert k8s.place(k8s.Needs.parse(["gpu"]), d) == []


def test_label_need_is_exact():
    d = doc(node("a", labels={"randomesh.io/role": "inference"}),
            node("b", labels={"randomesh.io/role": "storage-node"}))
    ranked = k8s.place(k8s.Needs.parse(["label:randomesh.io/role=inference"]), d)
    assert [r["node"] for r in ranked] == ["a"]


def test_place_ranks_by_free_memory():
    d = doc(node("tight", alloc={"cpu": "8", "memory": "8Gi"},
                 pods=[pod("Running", mem="7000Mi")]),
            node("roomy", alloc={"cpu": "8", "memory": "8Gi"},
                 pods=[pod("Running", mem="100Mi")]))
    assert [r["node"] for r in k8s.place(k8s.Needs(), d)] == ["roomy", "tight"]


def test_submit_refuses_when_nothing_qualifies(monkeypatch):
    monkeypatch.setattr(k8s, "inventory", lambda *a, **k: doc(node("tiny", alloc={
        "cpu": "1", "memory": "64Mi"})))
    with pytest.raises(k8s.ProviderError):
        k8s.submit("echo hi", k8s.Needs.parse(["ram:8192"]))


def test_submit_dry_run_pins_the_chosen_node_and_carries_provenance(monkeypatch):
    monkeypatch.setattr(k8s, "inventory", lambda *a, **k: doc(node("pansa")))
    res = k8s.submit("echo hi", k8s.Needs.parse(["ram:64"]), dry_run=True)
    spec = res["manifest"]["spec"]
    assert spec["template"]["spec"]["nodeSelector"] == {"kubernetes.io/hostname": "pansa"}
    assert spec["template"]["spec"]["restartPolicy"] == "Never"
    assert res["manifest"]["metadata"]["labels"]["randomesh.io/managed-by"] == "aion"
    assert res["manifest"]["metadata"]["annotations"]["aion.io/needs"] == "ram>=64MB"
    json.dumps(res["manifest"])          # must stay JSON-serialisable for kubectl
