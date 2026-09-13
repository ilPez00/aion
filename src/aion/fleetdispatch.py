"""fleetdispatch.py — cockpit read/submit over dispatch.sh (the roaming path).

agent-task.sh runs where it was born (local cwd, local pid); dispatch.sh
stages command+tree onto whichever node satisfies the needs and runs there.
This module mirrors the fleettask.py pattern: pure table parsers plus a thin
`submit_preview` wrapper that only ever uses --dry-run. Real submission from
the cockpit goes through the palette's preview-then-confirm two-step, the
same HITL shape the mesh verbs already use.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field

DISPATCH_SCRIPT = os.path.expanduser(
    "~/dev/randomesh/scripts/fleet/dispatch.sh")


@dataclass
class DispatchJob:
    id: str = ""
    node: str = ""
    name: str = ""
    state: str = ""
    rc: str = ""
    idempotent: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        return self.state in ("done", "failed", "lost", "cancelled")

    def as_dict(self) -> dict:
        return {"id": self.id, "node": self.node, "name": self.name,
                "state": self.state, "rc": self.rc,
                "idempotent": self.idempotent, "terminal": self.terminal}


_ROW = re.compile(r"^(\S+)\s+(\S+)\s+(.{1,24}?)\s{2,}(\S+)\s+(\S+)\s*(\S*)\s*$")


def parse_status_table(text: str) -> list[DispatchJob]:
    """Parse `dispatch.sh status` (ID NODE NAME STATE IDE RC). Never raises."""
    jobs: list[DispatchJob] = []
    for line in text.splitlines():
        s = line.rstrip()
        if not s or s.startswith("ID ") or s.startswith("ID\t"):
            continue
        m = _ROW.match(s)
        if not m:
            continue
        jid, node, name, state, ide, rc = m.groups()
        if state not in ("queued", "running", "done", "failed", "lost",
                         "cancelled", "unreachable", "missing"):
            continue
        jobs.append(DispatchJob(id=jid, node=node, name=name.strip(),
                                state=state, rc=rc or "-",
                                idempotent=(ide == "Y")))
    return jobs


def submit_preview(command: str, *, needs: list[str] = (),
                   stage: str = "", env: dict | None = None,
                   host: str = "", timeout: int = 3600,
                   script: str = DISPATCH_SCRIPT) -> tuple[str, str]:
    """Dry-run submit. Returns (job_id, node). Raises RuntimeError with the
    script's rejection reasons when nothing qualifies."""
    cmd = [script, "submit", command, "--dry-run", "--timeout", str(timeout)]
    for q in needs:
        cmd += ["--needs", q]
    if stage:
        cmd += ["--stage", stage]
    for k, v in (env or {}).items():
        cmd += ["--env", f"{k}={v}"]
    if host:
        cmd += ["--host", host]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    out = (p.stdout or "") + (p.stderr or "")
    m = re.search(r"\(dry-run\) job (\S+) -> (\S+)", out)
    if p.returncode != 0 or not m:
        raise RuntimeError(out.strip().splitlines()[-1][:300] if out.strip()
                           else "dispatch preview failed")
    return m.group(1), m.group(2)


def submit(command: str, *, script: str = DISPATCH_SCRIPT,
           needs: list[str] = (), stage: str = "",
           env: dict | None = None, host: str = "", name: str = "",
           timeout: int = 3600, idempotent: bool = False) -> str:
    """Real submit (no --dry-run). Returns the job id.
    Cockpit callers must confirm first (palette two-step)."""
    cmd = [script, "submit", command, "--timeout", str(timeout)]
    for q in needs:
        cmd += ["--needs", q]
    if stage:
        cmd += ["--stage", stage]
    for k, v in (env or {}).items():
        cmd += ["--env", f"{k}={v}"]
    if host:
        cmd += ["--host", host]
    if name:
        cmd += ["--name", name]
    if idempotent:
        cmd += ["--idempotent"]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if p.returncode != 0:
        raise RuntimeError(((p.stderr or p.stdout) or "").strip()
                           .splitlines()[-1][:300])
    return (p.stdout or "").strip().splitlines()[0].strip()
