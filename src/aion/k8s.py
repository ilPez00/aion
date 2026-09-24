"""k8s.py — Kubernetes as one more aion execution provider.

aion already answers "where SHOULD this run" for the SSH fleet (select_lib.sh,
mirrored by fleetplace.py). This module adds the *in-cluster* provider: a task
with capability requirements is matched against the Kubernetes nodes Randomesh
already inventoried — ~/.local/state/randomesh/k8s.json, produced by
randomesh/scripts/fleet/k8s-discover.sh — and submitted as a Job.

It deliberately does not reimplement scheduling. It hands a pod spec to
Kubernetes and reports what Kubernetes did with it:

    aion task -> capability requirements -> Randomesh inventory
              -> placement decision -> Kubernetes adapter -> Job/Pod

  aion k8s nodes             the cluster as aion sees it (with live usage)
  aion k8s place "<cmd>" ... which node WOULD take it (no side effects)
  aion k8s submit "<cmd>" ... create the Job, print its id
  aion k8s status [id]       what is running, what happened
  aion k8s logs <id>         logs of that job's pod
  aion k8s delete <id>       remove the job (and its pods)
  aion k8s backend           is this provider usable at all

Needs vocabulary is the fleet's own (`gpu|ram:MB|cpu:CORES|label:K=V`), so a
task written for `mesh dispatch --needs` can be aimed at Kubernetes unchanged.
The two scorers stay separate on purpose: select_lib.sh places across SSH
targets, this places inside one cluster.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

STATE = Path.home() / ".local/state/randomesh"
K8S_JSON = STATE / "k8s.json"
DISCOVER = os.path.expanduser("~/dev/randomesh/scripts/fleet/k8s-discover.sh")
DEFAULT_IMAGE = "randomesh/lab-http:0.1.0"
DEFAULT_NS = "randomesh-lab"
MANAGED_BY = "randomesh.io/managed-by=aion"
STALE_S = 120


class ProviderError(RuntimeError):
    """Kubernetes is not reachable, or the request cannot be honoured."""


# --------------------------------------------------------------------- kubectl
def kubectl(args: list[str], timeout: int = 30) -> str:
    try:
        p = subprocess.run(["kubectl", *args], capture_output=True, text=True,
                           timeout=timeout, check=False)
    except FileNotFoundError as e:
        raise ProviderError("kubectl not found — no Kubernetes on this node") from e
    except subprocess.TimeoutExpired as e:
        raise ProviderError(f"kubectl {' '.join(args)} timed out") from e
    if p.returncode != 0:
        raise ProviderError((p.stderr or p.stdout or "").strip().splitlines()[-1:][0]
                            if (p.stderr or p.stdout) else "kubectl failed")
    return p.stdout


def kubectl_json(args: list[str], timeout: int = 30) -> dict:
    out = kubectl([*args, "-o", "json"], timeout=timeout)
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {}


# ------------------------------------------------------------------ inventory
def inventory(refresh: bool = True) -> dict:
    """Randomesh's Kubernetes view, refreshed if the cache is stale.

    Refreshing is a `kubectl get` sweep, so it is only done when the file is
    missing or old — a placement query must not cost a cluster round trip.
    """
    fresh = False
    try:
        fresh = (time.time() - K8S_JSON.stat().st_mtime) < STALE_S
    except OSError:
        fresh = False
    if refresh and not fresh and os.path.exists(DISCOVER):
        subprocess.run(["bash", DISCOVER], capture_output=True, text=True,
                       timeout=60, check=False)
    try:
        return json.loads(K8S_JSON.read_text())
    except OSError as e:
        raise ProviderError(
            "no Kubernetes inventory at ~/.local/state/randomesh/k8s.json — "
            "run: mesh k8s discover") from e


def cluster(doc: dict | None = None) -> dict:
    doc = doc or inventory()
    clusters = doc.get("clusters") or {}
    if not clusters:
        raise ProviderError("inventory has no clusters")
    return next(iter(clusters.values()))


def nodes(doc: dict | None = None) -> dict:
    return cluster(doc).get("nodes", {})


# --------------------------------------------------------------------- needs
@dataclass
class Needs:
    ram_mb: int = 0
    cpu: float = 0.0
    gpu: bool = False
    labels: dict = field(default_factory=dict)

    @classmethod
    def parse(cls, specs: list[str]) -> "Needs":
        n = cls()
        for s in specs:
            if s == "gpu":
                n.gpu = True
            elif s.startswith("ram:"):
                n.ram_mb = int(float(s[4:]))
            elif s.startswith("cpu:"):
                n.cpu = float(s[4:])
            elif s.startswith("label:"):
                k, _, v = s[6:].partition("=")
                if k:
                    n.labels[k] = v
            else:
                raise ProviderError(f"unknown need '{s}' (gpu|ram:MB|cpu:N|label:K=V)")
        return n

    def describe(self) -> str:
        bits = []
        if self.gpu:
            bits.append("gpu")
        if self.ram_mb:
            bits.append(f"ram>={self.ram_mb}MB")
        if self.cpu:
            bits.append(f"cpu>={self.cpu}")
        bits += [f"{k}={v}" for k, v in self.labels.items()]
        return " ".join(bits) or "none"


def _mi(s: str) -> float:
    """Kubernetes memory quantity -> MiB. Handles Ki/Mi/Gi and bare bytes."""
    m = re.match(r"^([0-9.]+)\s*([KMGTP]i?)?$", (s or "").strip())
    if not m:
        return 0.0
    v = float(m.group(1))
    unit = m.group(2) or ""
    factor = {"": 1 / 1048576, "Ki": 1 / 1024, "Mi": 1, "Gi": 1024,
              "Ti": 1024 ** 2, "K": 1000 / 1048576, "M": 1e6 / 1048576,
              "G": 1e9 / 1048576, "T": 1e12 / 1048576}.get(unit, 0.0)
    return v * factor


def _cores(s: str) -> float:
    s = (s or "").strip()
    return float(s[:-1]) / 1000 if s.endswith("m") else (float(s) if s else 0.0)


def _free(node: dict) -> tuple[float, float]:
    """(free MiB, free cores) — allocatable minus what is already asked for."""
    k = node["kubernetes"]
    alloc_mi = _mi(k.get("allocatable", {}).get("memory", ""))
    alloc_cpu = _cores(k.get("allocatable", {}).get("cpu", ""))
    used_mi = used_cpu = 0.0
    for p in node.get("pods", []):
        # Only count pods that are actually consuming; a Succeeded pod's
        # requests are free again and counting them would starve the node.
        if p.get("phase") not in ("Running", "Pending"):
            continue
        for r in p.get("resources", []):
            q = r.get("requests", {})
            used_mi += _mi(q.get("memory", ""))
            used_cpu += _cores(q.get("cpu", ""))
    return max(0.0, alloc_mi - used_mi), max(0.0, alloc_cpu - used_cpu)


def place(needs: Needs, doc: dict | None = None,
          require_gpu: bool | None = None) -> list[dict]:
    """Rank cluster nodes for these needs. Highest free memory wins.

    Returns [] when nothing qualifies — callers report that rather than
    falling back to "just try the cluster and see", which is how a scheduler
    turns a capability question into a crash loop.
    """
    doc = doc or inventory()
    if require_gpu is not None:
        needs.gpu = require_gpu
    ranked = []
    for alias, node in nodes(doc).items():
        k = node["kubernetes"]
        if not k.get("ready"):
            continue
        if any(t.get("effect") == "NoSchedule" for t in k.get("taints", [])):
            continue
        labels = k.get("randomesh_labels", {})
        if needs.gpu and labels.get("randomesh.io/gpu", "none") in ("none", ""):
            continue
        if needs.gpu and labels.get("randomesh.io/gpu-driver", "none") == "none":
            continue  # a card with no working driver is not an accelerator
        if any(labels.get(key) != val for key, val in needs.labels.items()):
            continue
        free_mi, free_cpu = _free(node)
        if needs.ram_mb and free_mi < needs.ram_mb:
            continue
        if needs.cpu and free_cpu < needs.cpu:
            continue
        ranked.append({"node": alias, "k8s_name": k.get("node_name"),
                       "free_mib": round(free_mi), "free_cores": round(free_cpu, 2),
                       "usage": k.get("usage") or {},
                       "labels": labels})
    ranked.sort(key=lambda r: (-r["free_mib"], -r["free_cores"]))
    return ranked


# ---------------------------------------------------------------------- submit
def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:28] or "task").strip("-")


def submit(command: str, needs: Needs | None = None, image: str = DEFAULT_IMAGE,
           namespace: str = DEFAULT_NS, node: str = "", env: dict | None = None,
           ttl_s: int = 86400, dry_run: bool = False) -> dict:
    """Create a Job that runs `command`. Returns the chosen node and the job id.

    ttlSecondsAfterFinished keeps the lab from filling with dead Jobs, but it
    also means logs evaporate an hour after success — long enough to read, short
    enough not to hoard.
    """
    needs = needs or Needs()
    chosen = place(needs)
    if node:
        chosen = [c for c in chosen if c["k8s_name"] == node or c["node"] == node]
        if not chosen:
            raise ProviderError(f"node '{node}' does not exist or cannot take the task")
    if not chosen:
        raise ProviderError(f"no cluster node satisfies [{needs.describe()}]")

    job_id = f"aion-{_slug(command.split()[0] if command.split() else 'task')}-{int(time.time())}"
    target = chosen[0].get("k8s_name")
    pod_spec = {
        "restartPolicy": "Never",
        "containers": [{
            "name": "task",
            "image": image,
            "imagePullPolicy": "IfNotPresent",
            "command": ["/bin/sh", "-c", command],
            "env": [{"name": k, "value": v} for k, v in (env or {}).items()],
            "resources": {
                "requests": {
                    "cpu": f"{max(50, int(needs.cpu * 1000))}m",
                    "memory": f"{needs.ram_mb or 64}Mi",
                },
                "limits": {
                    "cpu": f"{max(200, int(needs.cpu * 1000 * 2))}m",
                    "memory": f"{max(needs.ram_mb * 2, 128)}Mi",
                },
            },
        }],
    }
    if target:
        # nodeSelector rather than nodeName: aion already picked the node, but
        # the pod still goes through the Kubernetes scheduler, so resource fit
        # and taints are enforced by the thing that owns them. nodeName would
        # silently bypass that.
        pod_spec["nodeSelector"] = {"kubernetes.io/hostname": target}
    manifest = {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {
            "name": job_id, "namespace": namespace,
            # The labels are the whole point: this is how Randomesh and the
            # cockpit find aion-submitted work without a side database.
            "labels": {"randomesh.io/managed-by": "aion",
                       "aion.io/task": job_id,
                       "app.kubernetes.io/name": "aion-task"},
            "annotations": {
                "randomesh.io/owner": os.environ.get("USER", "gio"),
                "aion.io/needs": needs.describe(),
                "aion.io/command": command[:512],
                "aion.io/node-choice": target or "",
            },
        },
        "spec": {
            "backoffLimit": 1,
            "ttlSecondsAfterFinished": ttl_s,
            "template": {
                "metadata": {"labels": {"randomesh.io/managed-by": "aion",
                                        "aion.io/task": job_id}},
                "spec": pod_spec,
            },
        },
    }
    if dry_run:
        return {"dry_run": True, "job": job_id, "node": chosen[0], "manifest": manifest}
    p = subprocess.run(["kubectl", "apply", "-f", "-"], input=json.dumps(manifest),
                       capture_output=True, text=True, timeout=60, check=False)
    if p.returncode != 0:
        raise ProviderError((p.stderr or p.stdout).strip().splitlines()[-1])
    return {"dry_run": False, "job": job_id, "node": chosen[0],
            "created": (p.stdout or "").strip()}


def jobs(namespace: str = DEFAULT_NS) -> list[dict]:
    j = kubectl_json(["get", "jobs", "-n", namespace,
                      "-l", "randomesh.io/managed-by=aion"])
    out = []
    for item in j.get("items", []):
        st = item.get("status", {})
        mt = item.get("metadata", {})
        out.append({
            "id": mt.get("name"), "created": mt.get("creationTimestamp"),
            "succeeded": st.get("succeeded", 0), "failed": st.get("failed", 0),
            "active": st.get("active", 0),
            "conditions": [c.get("type") for c in st.get("conditions", [])
                           if c.get("status") == "True"],
            "command": (mt.get("annotations", {}) or {}).get("aion.io/command", ""),
            "needs": (mt.get("annotations", {}) or {}).get("aion.io/needs", ""),
        })
    out.sort(key=lambda r: r.get("created") or "", reverse=True)
    return out


def job_pods(job_id: str, namespace: str = DEFAULT_NS) -> list[dict]:
    j = kubectl_json(["get", "pods", "-n", namespace, "-l", f"aion.io/task={job_id}"])
    return [{"name": p["metadata"]["name"], "phase": p["status"].get("phase"),
             "node": p["spec"].get("nodeName"),
             "image": p["spec"]["containers"][0].get("image")}
            for p in j.get("items", [])]


def logs(job_id: str, namespace: str = DEFAULT_NS, tail: int = 100) -> str:
    pods = job_pods(job_id, namespace)
    if not pods:
        raise ProviderError(f"no pods for job {job_id}")
    return kubectl(["logs", "-n", namespace, pods[0]["name"], f"--tail={tail}"])


def delete(job_id: str, namespace: str = DEFAULT_NS) -> str:
    # Foreground so the pods go with the job; a Job deleted in background leaves
    # pods that Randomesh would then report as orphaned work.
    return kubectl(["delete", "job", "-n", namespace, job_id,
                    "--cascade=foreground", "--wait=false"])


# ------------------------------------------------------------------------ cli
def run_cli(argv: list[str]) -> str:
    """Text in, text out — shared by `aion k8s` and the cockpit's mesh verb."""
    sub = argv[0] if argv else "status"
    rest = argv[1:]
    try:
        if sub == "backend":
            out = kubectl(["version"], timeout=15)
            return "k8s provider: usable\n" + out.strip()
        if sub == "nodes":
            doc = inventory()
            lines = []
            for alias, n in nodes(doc).items():
                k = n["kubernetes"]
                u = k.get("usage") or {}
                lines.append(
                    f"  {alias:10s} {k.get('node_name',''):10s} "
                    f"{'Ready' if k.get('ready') else 'NotReady':8s} "
                    f"pods={n.get('pod_count',0):<3} "
                    f"cpu={u.get('cpu_cores','?')}/{k.get('allocatable',{}).get('cpu','?')} "
                    f"mem={u.get('memory_bytes','?')}/{k.get('allocatable',{}).get('memory','?')} "
                    f"gpu={k.get('randomesh_labels',{}).get('randomesh.io/gpu','?')}"
                    f"/{k.get('randomesh_labels',{}).get('randomesh.io/gpu-driver','?')}")
            head = (f"cluster {list(doc.get('clusters',{}))[0]} "
                    f"server={doc.get('server_version')} at={doc.get('at')}")
            return head + "\n" + "\n".join(lines) if lines else head + "\n  (no nodes)"

        if sub == "place":
            toks = [t for t in rest if not t.startswith("--needs")]
            needs = Needs.parse([rest[i + 1] for i in range(len(rest) - 1)
                                 if rest[i] == "--needs"])
            ranked = place(needs)
            if not ranked:
                return f"no cluster node can take [{needs.describe()}]"
            best = ranked[0]
            return (f"would run on {best['node']} ({best['k8s_name']}) "
                    f"free {best['free_mib']}Mi / {best['free_cores']} cores "
                    f"[needs: {needs.describe()}]"
                    + ("\n  runners-up: " + ", ".join(
                        f"{r['node']}({r['free_mib']}Mi)" for r in ranked[1:4])
                       if len(ranked) > 1 else ""))

        if sub == "submit":
            if not rest:
                return 'usage: aion k8s submit "<command>" [--needs SPEC] [--image IMG] [--node N] [--dry-run]'
            def opt(name, default=""):
                return rest[rest.index(name) + 1] if name in rest else default
            needs_list = [rest[i + 1] for i in range(len(rest) - 1) if rest[i] == "--needs"]
            cmd = " ".join(t for i, t in enumerate(rest)
                           if not t.startswith("--")
                           and (i == 0 or rest[i - 1] not in ("--needs", "--image", "--node")))
            res = submit(cmd, needs=Needs.parse(needs_list), image=opt("--image", DEFAULT_IMAGE),
                         node=opt("--node"), dry_run="--dry-run" in rest)
            if res["dry_run"]:
                return (f"dry-run: {res['job']} would run on {res['node']['k8s_name']} "
                        f"({res['node']['free_mib']}Mi free)")
            return (f"submitted {res['job']} to Kubernetes on "
                    f"{res['node']['k8s_name']} ({res['created']})")

        if sub == "status":
            if rest:
                jid = rest[0]
                pods = job_pods(jid)
                return (f"{jid}: " + (", ".join(
                    f"{p['name']} {p['phase']} on {p['node']}" for p in pods)
                    or "no pods")) + "\n" + logs(jid, tail=20)
            rows = jobs()
            if not rows:
                return ("no aion tasks on the cluster "
                        "(jobs labelled randomesh.io/managed-by=aion)")
            lines = [f"  {r['id']:34s} active={r['active']} ok={r['succeeded']} "
                     f"fail={r['failed']} {','.join(r['conditions']):10s} {r['needs']}"
                     for r in rows]
            return f"aion tasks on Kubernetes: {len(rows)}\n" + "\n".join(lines)

        if sub == "logs":
            if not rest:
                return "usage: aion k8s logs <job-id>"
            return logs(rest[0])

        if sub == "delete":
            if not rest:
                return "usage: aion k8s delete <job-id>"
            return delete(rest[0]).strip()

        return ("usage: aion k8s <backend|nodes|place|submit|status|logs|delete>\n"
                "  submit: aion k8s submit \"<command>\" [--needs ram:MB|cpu:N|gpu|label:K=V] "
                "[--image IMG] [--node NAME] [--dry-run]")
    except ProviderError as e:
        return f"k8s provider: {e}"


def main(argv: list[str] | None = None) -> int:
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    print(run_cli(argv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
