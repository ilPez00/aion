"""fleetplace.py — cockpit mirror of the fleet placement scorer.

`scripts/fleet/delegate.sh` owns the decision (least-loaded reachable node,
probed live by `probe.sh`). This module mirrors its scoring exactly so the
cockpit can rank candidates the same way — for display, for dry-run previews,
and for the parity test that keeps the two from drifting.

Formula (delegate.sh): score = 0.6*(fre/100) + 0.3*idle + 0.1*light where
idle = max(0, 1-load/cores), light = max(0, 1-tasks/200). First-highest wins.
Skip rules: unreachable, GPU-needed-but-absent, and the --min-mem-mb hard gate
in MB (probe.sh's memfree_mb field; percent only weights the score).
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

DELEGATE_SCRIPT = os.path.expanduser(
    "~/dev/randomesh/scripts/fleet/delegate.sh")


@dataclass
class Candidate:
    name: str
    cores: float = 0.0
    load: float = 0.0
    fre: float = 0.0       # % RAM available (probe.sh memfree_pct)
    fre_mb: float | None = None  # MB RAM available (memfree_mb; None = legacy
    gpu: float = 0.0       # % GPU busy, 0 if none/unknown
    tasks: float = 0.0


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


def pick(candidates: list[Candidate], *, need_gpu: bool = False,
         min_mem_mb: int = 0) -> Candidate | None:
    """First-highest scorer wins; ties keep the earlier candidate.

    The MB gate applies only when the probe carried MB data (legacy 6-field
    probes can't be gated — same rule as delegate.sh).
    """
    best: Candidate | None = None
    best_score = -1.0
    for c in candidates:
        if need_gpu and c.gpu == 0:
            continue
        if min_mem_mb > 0 and c.fre_mb is not None and c.fre_mb < min_mem_mb:
            continue
        s = score(c)
        if s > best_score:
            best, best_score = c, s
    return best


def delegate_dry_run(hosts: list[str] | None, command: str = "true", *,
                     need_gpu: bool = False, min_mem_mb: int = 0,
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
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                       env=env)
    for line in p.stdout.splitlines():
        if line.startswith("=== chosen:"):
            # "=== chosen: <host> (score=N) ===" — host is the token after the tag
            rest = line.split("=== chosen:", 1)[1].strip()
            return rest.split()[0] if rest else None
    return None
