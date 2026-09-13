"""fleetplace.py — cockpit mirror of the fleet placement scorer.

`scripts/fleet/select_lib.sh` owns the decision (capability-filtered,
least-loaded reachable node). This module mirrors it exactly so the cockpit
can rank candidates the same way — for display, for dry-run previews, and
for the parity test that keeps the two from drifting.

Filter (capability.json node entries): gpu | ram:MB | disk:MB | tool:NAME
(incl. the `tool:llama` special-case). Score (delegate formula):
0.6*(fre/100) + 0.3*idle + 0.1*light where idle = max(0, 1-load/cores),
light = max(0, 1-tasks/200). First-highest wins.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

DELEGATE_SCRIPT = os.path.expanduser(
    "~/dev/randomesh/scripts/fleet/delegate.sh")
CAPABILITY_CACHE = Path.home() / ".local/state/randomesh/capability.json"


@dataclass
class Candidate:
    name: str
    cores: float = 0.0
    load: float = 0.0
    fre: float = 0.0       # % RAM available (probe.sh memfree_pct)
    fre_mb: float | None = None  # MB RAM available (memfree_mb; None = legacy
    gpu: float = 0.0       # % GPU busy, 0 if none/unknown
    tasks: float = 0.0
    caps: dict = field(default_factory=dict)  # capability.json node entry


def parse_probe_line(line: str) -> Candidate | None:
    """Parse one probe.sh line (6-field legacy or 7-field with MB). None for
    down/unparseable (never a zero-score row — an unprobed node must not
    outrank a merely busy one)."""
    parts = line.strip().split()
    if len(parts) not in (6, 7):
        return None
    name, rest = parts[0], parts[1:]
    if rest[0] == "down":
        return None  # "host down 0 ..." — unreachable, not idle
    try:
        nums = [float(x) for x in rest]
    except ValueError:
        return None
    if len(nums) == 5:
        cores, load, fre, gpu, tasks = nums
        fre_mb = None
    else:
        cores, load, fre, fre_mb, gpu, tasks = nums
    return Candidate(name=name, cores=cores, load=load, fre=fre, fre_mb=fre_mb,
                     gpu=gpu, tasks=tasks)


def score(c: Candidate) -> float:
    """delegate.sh's awk formula, verbatim."""
    idle = 1 - (c.load / c.cores) if c.cores > 0 else 0.0
    idle = max(idle, 0.0)
    light = max(1 - (c.tasks / 200), 0.0)
    return 0.6 * (c.fre / 100) + 0.3 * idle + 0.1 * light


def gpu_usable_caps(n: dict) -> int:
    """VRAM that can actually hold weights (mirrors select_lib/gpu_usable)."""
    try:
        vram = int(n.get("gpu_vram_mb") or 0)
    except (TypeError, ValueError):
        return 0
    if vram < 1024:
        return 0
    if n.get("vulkan") != "yes" and n.get("kfd") != "yes":
        return 0
    return vram


def satisfies(caps: dict, need: str) -> bool:
    """One requirement against a capability.json node entry."""
    kind, _, arg = need.partition(":")
    if kind == "gpu":
        return gpu_usable_caps(caps) > 0
    if kind == "ram":
        try:
            return int(caps.get("mem_avail_mb") or 0) >= int(arg)
        except ValueError:
            return False
    if kind == "disk":
        try:
            return any(v >= int(arg) for v in (caps.get("disks") or {}).values())
        except ValueError:
            return False
    if kind == "tool":
        return caps.get(f"has_{arg}") == "yes" or (
            arg == "llama" and caps.get("llama_server", "none") != "none")
    return True


def load_capability(path: str | Path = CAPABILITY_CACHE) -> dict:
    """capability.json nodes map; missing/corrupt → {} (never raises)."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    nodes = data.get("nodes", {}) if isinstance(data, dict) else {}
    return nodes if isinstance(nodes, dict) else {}


def pick_with_reasons(candidates: list[Candidate], *,
                      need_gpu: bool = False,
                      min_mem_mb: int = 0,
                      needs: list[str] = ()) -> tuple[Candidate | None, dict]:
    """First-highest scorer among satisfiers; ties keep the earlier candidate.

    Returns (winner, rejections: alias -> [missing needs]). need_gpu /
    min_mem_mb are sugar for needs entries, mirroring the shell flags.
    """
    wants = list(needs)
    if need_gpu:
        wants.append("gpu")
    if min_mem_mb > 0:
        wants.append(f"ram:{min_mem_mb}")
    rejections: dict[str, list[str]] = {}
    best: Candidate | None = None
    best_score = -1.0
    for c in candidates:
        if c.caps:
            missing = [q for q in wants if not satisfies(c.caps, q)]
        else:
            # legacy probe rows carry no capability data: gpu falls back to
            # the probe flag, ram: to the live MB gate below, everything else
            # fails closed (same as select_lib.sh without a cache — an
            # unverified node must not win).
            missing = [q for q in wants
                       if q != "gpu" and not q.startswith("ram:")]
            if "gpu" in wants and c.gpu == 0:
                missing.append("gpu")
        if missing:
            rejections[c.name] = missing
            continue
        if min_mem_mb > 0 and c.fre_mb is not None and c.fre_mb < min_mem_mb:
            rejections[c.name] = [f"ram:{min_mem_mb}"]
            continue
        s = score(c)
        if s > best_score:
            best, best_score = c, s
    return best, rejections


def pick(candidates: list[Candidate], *, need_gpu: bool = False,
         min_mem_mb: int = 0,
         needs: list[str] = ()) -> Candidate | None:
    """First-highest scorer wins; ties keep the earlier candidate.

    The MB gate applies only when the probe carried MB data (legacy 6-field
    probes can't be gated — same rule as delegate.sh).
    """
    best, _ = pick_with_reasons(candidates, need_gpu=need_gpu,
                                min_mem_mb=min_mem_mb, needs=needs)
    return best


def delegate_dry_run(hosts: list[str] | None, command: str = "true", *,
                     need_gpu: bool = False, min_mem_mb: int = 0,
                     needs: list[str] = (),
                     script: str = DELEGATE_SCRIPT,
                     env: dict | None = None) -> str | None:
    """Run the REAL delegate.sh --dry-run; return the chosen host (or None).

    hosts=None omits --host, so the script uses its own FLEET_DELEGATE_HOSTS
    default. The cockpit proposes with pick(); the fleet disposes with this.
    A parity test asserts both agree on controlled inputs.
    """
    cmd = [script, command, "--dry-run"]
    if hosts:
        cmd += ["--host", ",".join(hosts)]
    if need_gpu:
        cmd.append("--gpu")
    if min_mem_mb > 0:
        cmd += ["--min-mem-mb", str(min_mem_mb)]
    for q in needs:
        cmd += ["--needs", q]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                       env=env)
    for line in p.stdout.splitlines():
        if line.startswith("=== chosen:"):
            # "=== chosen: <host> (score=N) ===" — host is the token after the tag
            rest = line.split("=== chosen:", 1)[1].strip()
            return rest.split()[0] if rest else None
    return None
