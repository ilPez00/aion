"""procgraph.py — aion's own work, as a graph.

The cockpit already renders directories and notes as organic graphs. This
module supplies the third corpus: **what aion itself is doing**. Fleet
instances, the harnesses registered on each, the tasks those harnesses spawned
and the swarm agents waiting on one another are all one connected structure —

    instance ── harness ── task ── subtask
                              swarm agent ── depends-on ── swarm agent

and a graph is the honest shape for it. A flat task list cannot show that six
tasks are all queued behind one stalled harness, or that half the fleet is
idle while one box is saturated.

Read-only and out-of-process
----------------------------
The web HUD is a separate process from the TUI cockpit, so it cannot see the
live `TaskRegistry` in memory. Everything here is reconstructed from what the
cockpit already checkpoints to disk (`~/.aion/instances/<id>/session.json`,
`meta.json`) — which is also why this works when no cockpit is running at all:
you get the last known state instead of an empty screen.

Nothing in this module writes. A viewer that can mutate the fleet is a much
larger security question than a viewer that cannot.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

# Task states, mirrored from core.TaskState. Duplicated deliberately: importing
# core drags in asyncio, the bus and the whole harness registry, and this module
# is read by a plain HTTP handler that should stay cheap to import.
ACTIVE_STATES = ("running", "pending")
DEAD_STATES = ("done", "failed", "cancelled", "interrupted")

# Presentation grouping. Hue is paired with a glyph in the UI so state never
# rides on colour alone.
STATE_GROUP = {
    "running": "active", "pending": "queued", "done": "done",
    "failed": "failed", "cancelled": "failed", "interrupted": "stalled",
}
SWARM_GROUP = {
    "working": "active", "planning": "active", "idle": "queued",
    "waiting": "queued", "blocked": "stalled", "done": "done",
    "failed": "failed", "cancelled": "failed",
}


@dataclass
class Instance:
    id: str
    hostname: str = ""
    pid: int = 0
    port: int = 0
    alive: bool = False
    active_harness: str = ""
    running_count: int = 0
    started_at: float = 0.0
    updated_at: float = 0.0

    @property
    def age_s(self) -> float:
        return max(0.0, time.time() - self.updated_at) if self.updated_at else 0.0


@dataclass
class ProcTask:
    id: str
    label: str
    harness: str
    instance: str
    state: str = "pending"
    progress: float = 0.0
    eta: float | None = None
    domain: str = ""
    log: list[str] = field(default_factory=list)


def _pid_alive(pid: int) -> bool:
    """Signal 0 probes existence without touching the process."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # exists, owned by somebody else
    except OSError:
        return False
    return True


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def instances_root() -> Path:
    return Path(os.environ.get("AION_HOME", os.path.expanduser("~/.aion"))) / "instances"


def read_instances(root: Path | None = None) -> list[Instance]:
    """Every cockpit that has ever run on this box, live or not.

    Dead instances are kept (flagged `alive=False`) rather than hidden: the
    tasks they left behind are exactly what you want to find after a crash,
    and silently dropping them is how INTERRUPTED work gets forgotten.
    """
    root = root or instances_root()
    out: list[Instance] = []
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        meta = _read_json(d / "meta.json", {})
        out.append(Instance(
            id=meta.get("id", d.name),
            hostname=meta.get("hostname", ""),
            pid=int(meta.get("pid", 0) or 0),
            port=int(meta.get("port", 0) or 0),
            alive=_pid_alive(int(meta.get("pid", 0) or 0)),
            active_harness=meta.get("active_harness", ""),
            running_count=int(meta.get("running_count", 0) or 0),
            started_at=float(meta.get("started_at", 0) or 0),
            updated_at=float(meta.get("updated_at", 0) or 0),
        ))
    return out


def read_tasks(instance_id: str, root: Path | None = None) -> list[ProcTask]:
    """Tasks checkpointed by one instance."""
    root = root or instances_root()
    raw = _read_json(root / instance_id / "session.json", [])
    if not isinstance(raw, list):
        return []
    out: list[ProcTask] = []
    for d in raw:
        if not isinstance(d, dict) or "id" not in d:
            continue
        out.append(ProcTask(
            id=str(d.get("id")), label=str(d.get("label", "")),
            harness=str(d.get("harness", "")), instance=instance_id,
            state=str(d.get("state", "pending")),
            progress=float(d.get("progress", 0.0) or 0.0),
            eta=d.get("eta"), domain=str(d.get("domain", "")),
            log=[str(x) for x in (d.get("log") or [])][-12:],
        ))
    return out


def read_harnesses(config_path: Path | None = None) -> list[dict]:
    """Harness registry from config/layout.json."""
    if config_path is None:
        from .paths import config_file
        config_path = config_file()
    cfg = _read_json(Path(config_path), {})
    out = []
    for h in cfg.get("harnesses", []) or []:
        if not isinstance(h, dict) or "id" not in h:
            continue
        out.append({
            "id": h["id"], "name": h.get("name", h["id"]),
            "type": h.get("type", ""), "tier": h.get("tier", "standard"),
            "vram_mb": h.get("vram_mb", 0), "enabled": bool(h.get("enabled", True)),
            "max_steps": h.get("max_steps"),
            "requires_approval": bool(h.get("requires_approval", False)),
        })
    return out


def read_swarm(root: Path | None = None) -> list[dict]:
    """Swarm agents, if any instance checkpointed one.

    `SwarmOrchestrator` is in-memory today, so this is usually empty. It reads
    an optional `swarm.json` so that the moment the orchestrator gains
    persistence the graph picks it up with no further work here.
    """
    root = root or instances_root()
    if not root.is_dir():
        return []
    out: list[dict] = []
    for d in sorted(root.iterdir()):
        for a in _read_json(d / "swarm.json", []) or []:
            if isinstance(a, dict) and "id" in a:
                # Two different "instance" meanings collide here: the cockpit
                # whose checkpoint this is, and (since cross-machine steps)
                # the instance the agent RUNS on. `**a` first would let the
                # directory name silently overwrite the agent's own, so the
                # agent's is preserved under its own key.
                out.append({**a, "instance": d.name,
                            "runs_on": str(a.get("instance", "") or "")})
    _tag_waves(out)
    return out


def _tag_waves(agents: list[dict]) -> None:
    """Stamp each swarm agent with its execution layer, per cockpit.

    A force-directed graph has no inherent reading order, so a DAG drawn in it
    is a blob that happens to be connected. `wave` gives the HUD something to
    order or anchor by, and it is computed here rather than in JavaScript so
    the TUI panel and the HUD cannot disagree about which step runs next.

    Layering is per instance directory: two cockpits each running their own
    swarm are two DAGs, and merging them by name would invent dependencies
    that do not exist.
    """
    from .swarmview import waves

    by_instance: dict[str, list[dict]] = {}
    for a in agents:
        by_instance.setdefault(str(a.get("instance", "")), []).append(a)
    for group in by_instance.values():
        for depth, layer in enumerate(waves(group)):
            for a in layer:
                a["wave"] = depth + 1


def snapshot(*, root: Path | None = None, config_path: Path | None = None,
             include_finished: bool = True) -> dict:
    """The whole picture, ready for the graph adapter in the HUD.

    Returns instances / harnesses / tasks / swarm plus a `summary` block the
    UI shows without having to re-derive counts client-side.
    """
    instances = read_instances(root)
    harnesses = read_harnesses(config_path)
    tasks: list[ProcTask] = []
    for inst in instances:
        tasks.extend(read_tasks(inst.id, root))
    if not include_finished:
        tasks = [t for t in tasks if t.state in ACTIVE_STATES]

    known = {h["id"] for h in harnesses}
    # A task can outlive the harness that spawned it (renamed or removed from
    # config). Synthesise a hub for it rather than dropping the task: orphaned
    # work is precisely what somebody opening this view is looking for.
    for t in tasks:
        if t.harness and t.harness not in known:
            known.add(t.harness)
            harnesses.append({"id": t.harness, "name": t.harness, "type": "?",
                              "tier": "unknown", "vram_mb": 0, "enabled": False,
                              "max_steps": None, "requires_approval": False,
                              "orphan": True})

    by_state: dict[str, int] = {}
    for t in tasks:
        by_state[t.state] = by_state.get(t.state, 0) + 1

    swarm = read_swarm(root)
    # The one line a viewer actually needs about a DAG. Derived server-side so
    # the HUD and the TUI say the same words about the same state — two
    # renderers each phrasing "nothing is running" their own way is how a
    # cockpit starts contradicting itself.
    # Per cockpit, for the same reason `_tag_waves` layers per cockpit: two
    # instances with a step called "research" are two separate DAGs, and one
    # merged sentence would report dependencies nobody declared.
    swarm_why = ""
    if swarm:
        from .swarmview import explain
        groups: dict[str, list[dict]] = {}
        for a in swarm:
            groups.setdefault(str(a.get("instance", "")), []).append(a)
        parts = [explain(g) if len(groups) == 1 else f"{inst}: {explain(g)}"
                 for inst, g in list(groups.items())[:2]]
        swarm_why = " · ".join(parts)

    return {
        "instances": [vars(i) | {"age_s": round(i.age_s, 1)} for i in instances],
        "harnesses": harnesses,
        "tasks": [vars(t) for t in tasks],
        "swarm": swarm,
        "summary": {
            "instances": len(instances),
            "live_instances": sum(1 for i in instances if i.alive),
            "harnesses": len(harnesses),
            "tasks": len(tasks),
            "active": sum(1 for t in tasks if t.state in ACTIVE_STATES),
            "by_state": by_state,
            "swarm": len(swarm),
            "swarm_why": swarm_why,
        },
    }


# ── live watch ───────────────────────────────────────────────────────────────
# The web HUD runs in a different process from the cockpit, so it cannot
# subscribe to the in-memory `core.Bus`. What it *can* see is the same thing
# the cockpit already writes on every change: the checkpoint files. Watching
# those gives push-quality liveness with zero coupling — no cockpit changes, no
# extra socket, and it keeps working when the cockpit is restarted underneath.
WATCH_FILES = ("meta.json", "session.json", "swarm.json")


FP_MAX_BYTES = 1 << 20      # never hash more than 1MB of one checkpoint


def fingerprint(root: Path | None = None) -> str:
    """Cheap signature of the fleet's on-disk state.

    Hashes the checkpoint bytes rather than stat()ing them. Stat looks
    cheaper and is wrong: `st_mtime_ns` is only as precise as the filesystem
    actually records, and on the dev box here every file written inside the
    same granule reports an identical mtime_ns. Size does not rescue it —
    the edits that matter most are same-length rewrites:

        "state": "running"  ->  "state": "pending"
        "progress": 0.4     ->  "progress": 0.9

    Both are invisible to (mtime, size) and both are exactly what the live
    view exists to show. Reading a few small JSON files at 4Hz costs far less
    than the `snapshot()` + `diff()` this guards, so correctness is free here.
    """
    root = root or instances_root()
    if not root.is_dir():
        return ""
    h = hashlib.blake2b(digest_size=16)
    try:
        for d in sorted(root.iterdir()):
            if not d.is_dir():
                continue
            for name in WATCH_FILES:
                p = d / name
                try:
                    with open(p, "rb") as fh:
                        blob = fh.read(FP_MAX_BYTES)
                except OSError:
                    continue          # missing/unreadable: contributes nothing
                h.update(f"{d.name}/{name}:".encode())
                h.update(blob)
    except OSError:
        return ""
    return h.hexdigest()


def diff(prev: dict | None, cur: dict) -> dict:
    """What changed between two snapshots.

    Returns only the moving parts, so a five-second-old browser tab does not
    need a full graph rebuild to catch up — and so the UI can animate the
    specific nodes that changed rather than flashing the whole view.
    """
    if not prev:
        return {"full": True, "changed": cur["tasks"], "removed": [],
                "instances": cur["instances"], "summary": cur["summary"]}

    def key(t):
        return f"{t['instance']}:{t['id']}"

    before = {key(t): t for t in prev.get("tasks", [])}
    after = {key(t): t for t in cur.get("tasks", [])}
    changed = []
    for k, t in after.items():
        old = before.get(k)
        if old is None:
            changed.append({**t, "_new": True})
        elif (old["state"] != t["state"] or old["progress"] != t["progress"]
              or old["log"] != t["log"]):
            changed.append({**t, "_was": old["state"]})
    return {
        "full": False,
        "changed": changed,
        "removed": [k for k in before if k not in after],
        "instances": cur["instances"],
        "summary": cur["summary"],
    }


def search(query: str, snap: dict | None = None, limit: int = 40) -> list[dict]:
    """Substring search across the process graph.

    Feeds the HUD's command palette, so results carry the coordinates needed
    to jump straight to the node: `{type, id, label, sub, module, node}`.
    """
    q = (query or "").strip().lower()
    if not q:
        return []
    snap = snap or snapshot()
    hits: list[dict] = []

    for h in snap["harnesses"]:
        if q in h["id"].lower() or q in h["name"].lower():
            hits.append({"type": "harness", "id": h["id"], "label": h["name"],
                         "sub": f"{h['tier']} · {h['type']}",
                         "module": "agents", "node": f"h{h['id']}"})
    for t in snap["tasks"]:
        blob = f"{t['label']} {t['harness']} {t['domain']} {' '.join(t['log'])}".lower()
        if q in blob:
            hits.append({"type": "task", "id": t["id"], "label": t["label"] or t["id"],
                         "sub": f"{t['state']} · {t['harness']}",
                         "module": "agents", "node": f"t{t['instance']}:{t['id']}"})
    for i in snap["instances"]:
        if q in i["id"].lower() or q in (i["hostname"] or "").lower():
            hits.append({"type": "instance", "id": i["id"],
                         "label": f"{i['id']}@{i['hostname'] or 'local'}",
                         "sub": "live" if i["alive"] else "offline",
                         "module": "agents", "node": f"i{i['id']}"})
    for a in snap["swarm"]:
        if q in str(a.get("name", "")).lower() or q in str(a.get("goal", "")).lower():
            hits.append({"type": "swarm", "id": a["id"], "label": a.get("name", a["id"]),
                         "sub": a.get("status", ""),
                         "module": "agents", "node": f"s{a['id']}"})
    return hits[:limit]
