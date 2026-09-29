"""containers.py — Docker/Kubernetes/mirrord as one container surface for aion.

Same contract as sentinelx.py and k8s.py: aion does not reimplement the
container platforms, it shells out to their CLIs, parses JSON, and reports.
Every call soft-fails — no docker, no kubectl, no cluster, returns rows=[] and
a `backend` note, never an exception that stalls the cockpit.

Backends probed once per snapshot via shutil.which:
    docker    -> `docker ps -a` (local daemon)
    kubectl   -> `kubectl get pods -A` (current context)
Mirrord is only probed: `mirror` needs it present to wrap a command.

Rows are the shape ui/containers_panel.py renders:
    {id, name, kind: "pod"|"container", state, image, node, age_s, backend}
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time

STALE_S = 120


class ContainerError(RuntimeError):
    """A container op the CLI refused. Message is the CLI's own stderr."""


def backend() -> str:
    """One-line capability note: which of docker/kubectl/mirrord exist."""
    have = [b for b in ("docker", "kubectl", "mirrord") if shutil.which(b)]
    return ", ".join(have) if have else "none of docker/kubectl/mirrord on PATH"


def _run(cli: list[str], timeout: int = 30) -> str:
    try:
        p = subprocess.run(cli, capture_output=True, text=True, timeout=timeout,
                           check=False)
    except FileNotFoundError as e:
        raise ContainerError(f"{cli[0]} not found") from e
    except subprocess.TimeoutExpired as e:
        raise ContainerError(f"{cli[0]} timed out") from e
    if p.returncode != 0:
        raise ContainerError((p.stderr or p.stdout or "failed").strip()[:200])
    return p.stdout


def _parse_age(ts: str | None) -> float:
    """Kubernetes RFC3339 timestamp -> seconds since start. 0.0 on junk."""
    if not ts:
        return 0.0
    try:
        from datetime import datetime, timezone
        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return max(0.0, time.time() - t.timestamp())
    except Exception:
        return 0.0


def snapshot(timeout: int = 30) -> dict:
    """Live rows from both backends. Never raises. `{rows, backends, ts}`."""
    rows: list[dict] = []
    errors: list[str] = []
    if shutil.which("kubectl"):
        try:
            data = json.loads(_run(["kubectl", "get", "pods", "-A", "-o", "json"],
                                   timeout=timeout))
            for it in data.get("items", []):
                meta = it.get("metadata", {})
                st = it.get("status", {})
                cs = (st.get("containerStatuses") or [{}])
                waiting = (cs[0].get("state", {}).get("waiting") or {}).get("reason", "")
                rows.append({
                    "id": f"{meta.get('namespace', '')}/{meta.get('name', '')}",
                    "name": meta.get("name", "?"),
                    "kind": "pod",
                    "state": waiting or st.get("phase", "Unknown"),
                    "image": ((it.get("spec", {}).get("containers") or [{}])[0]
                              .get("image", "")[:40]),
                    "node": it.get("spec", {}).get("nodeName", ""),
                    "age_s": _parse_age(st.get("startTime")),
                    "backend": "kubectl",
                })
        except (ContainerError, ValueError) as e:
            errors.append(f"kubectl: {e}")
    if shutil.which("docker"):
        try:
            items = json.loads(_run(["docker", "ps", "-a", "--format", "json"],
                                    timeout=timeout))
            for line in items if isinstance(items, list) else [items]:
                if not line:
                    continue
                rows.append({
                    "id": line.get("ID", "")[:12],
                    "name": line.get("Names", "?"),
                    "kind": "container",
                    "state": line.get("State", "unknown"),
                    "image": line.get("Image", "")[:40],
                    "node": "local",
                    "age_s": 0.0,
                    "backend": "docker",
                })
        except (ContainerError, ValueError) as e:
            errors.append(f"docker: {e}")
    return {"rows": rows, "backends": backend(), "errors": errors,
            "ts": time.time()}


def logs(target: str, tail: int = 50) -> str:
    """Last `tail` lines of a pod's or container's logs."""
    if "/" in target and shutil.which("kubectl"):
        ns, name = target.split("/", 1)
        return _run(["kubectl", "logs", "-n", ns, name, f"--tail={tail}"])
    return _run(["docker", "logs", "--tail", str(tail), target])


def restart(target: str) -> str:
    if "/" in target and shutil.which("kubectl"):
        ns, name = target.split("/", 1)
        return _run(["kubectl", "rollout", "restart",
                     f"deployment/{name}", "-n", ns])
    return _run(["docker", "restart", target])


def delete(target: str) -> str:
    if "/" in target and shutil.which("kubectl"):
        ns, name = target.split("/", 1)
        return _run(["kubectl", "delete", "pod", name, "-n", ns])
    return _run(["docker", "rm", "-f", target])


def mirror(target: str, cmd: str, timeout: int = 20) -> str:
    """Wrap `cmd` with mirrord against a k8s target (pod/deployment)."""
    if not shutil.which("mirrord"):
        raise ContainerError(
            "mirrord not found — install: https://mirrord.dev (cargo/npm)")
    if not shutil.which("kubectl"):
        raise ContainerError("mirrord needs kubectl for cluster targets")
    # validate the target against the live cluster before spending a run on it
    _run(["kubectl", "get", target if "/" in target else f"pod/{target}",
          "-o", "name"], timeout=timeout)
    full = target if "/" in target else f"pod/{target}"
    return (f"run in Term:  mirrord exec --target {full} -- {cmd}\n"
            f"(aion assembles the argv; the PTY owns the session)")
