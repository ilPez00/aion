"""fleetplace.py — cockpit mirror of the fleet placement scorer.

`scripts/fleet/delegate.sh` owns the decision (least-loaded reachable node,
probed live by `probe.sh`). This module mirrors its scoring exactly so the
cockpit can rank candidates the same way — for display, for dry-run previews,
and for the parity test that keeps the two from drifting.

Formula (delegate.sh): score = 0.6*(fre/100) + 0.3*idle + 0.1*light where
idle = max(0, 1-load/cores), light = max(0, 1-tasks/200). First-highest wins.
Skip rules: unreachable, GPU-needed-but-absent, and the low-RAM gate.

NOTE on the low-RAM gate: delegate.sh compares the free-RAM *percent* against
a hardcoded 10 whenever --min-mem-mb > 0 (the MB value itself is unused — a
units mismatch in the script). This mirror replicates that behavior verbatim;
fixing the script is a separate change both sides must then adopt together.
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
    gpu: float = 0.0       # % GPU busy, 0 if none/unknown
    tasks: float = 0.0


def parse_probe_line(line: str) -> Candidate | None:
    """Parse one probe.sh line. None for down/unparseable (never a zero-score
    row — an unprobed node must not outrank a merely busy one)."""
    parts = line.strip().split()
    if len(parts) != 6:
        return None
    name, rest = parts[0], parts[1:]
    if rest[0] == "down":
        return None  # "host down 0 0 0 0 0" — unreachable, not idle
    try:
        cores, load, fre, gpu, tasks = (float(rest[0]), float(rest[1]),
                                        float(rest[2]), float(rest[3]),
                                        float(rest[4]))
    except ValueError:
        return None
    return Candidate(name=name, cores=cores, load=load, fre=fre, gpu=gpu,
                     tasks=tasks)


def score(c: Candidate) -> float:
    """delegate.sh's awk formula, verbatim."""
    idle = 1 - (c.load / c.cores) if c.cores > 0 else 0.0
    idle = max(idle, 0.0)
    light = max(1 - (c.tasks / 200), 0.0)
    return 0.6 * (c.fre / 100) + 0.3 * idle + 0.1 * light


def pick(candidates: list[Candidate], *, need_gpu: bool = False,
         min_mem_mb: int = 0) -> Candidate | None:
    """First-highest scorer wins; ties keep the earlier candidate."""
    best: Candidate | None = None
    best_score = -1.0
    for c in candidates:
        if need_gpu and c.gpu == 0:
            continue
        if min_mem_mb > 0 and c.fre < 10:
            continue
        s = score(c)
        if s > best_score:
            best, best_score = c, s
    return best


def delegate_dry_run(hosts: list[str], command: str = "true", *,
                     need_gpu: bool = False, min_mem_mb: int = 0,
                     script: str = DELEGATE_SCRIPT,
                     env: dict | None = None) -> str | None:
    """Run the REAL delegate.sh --dry-run; return the chosen host (or None).

    The cockpit proposes with pick(); the fleet disposes with this. A parity
    test asserts both agree on controlled inputs.
    """
    cmd = [script, command, "--host", ",".join(hosts), "--dry-run"]
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
