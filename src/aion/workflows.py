"""
workflows.py — unified agentic workflow rows for glanceable HUD.

Pure data + rich-markup render helpers. No Textual, no I/O.
UI (and optional web) consume WorkflowRow snapshots.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Any, Sequence


# Pipeline order for mission strips
STAGES = ("plan", "act", "wait", "blocked", "verify", "done", "failed")

STAGE_GLYPH = {
    "plan": "◇",
    "act": "●",
    "wait": "⏳",
    "blocked": "⊘",
    "verify": "◈",
    "done": "✓",
    "failed": "✗",
}

# theme key used by renderers
STAGE_THEME = {
    "plan": "dim",
    "act": "warn",
    "wait": "warn",
    "blocked": "err",
    "verify": "accent",
    "done": "ok",
    "failed": "err",
}

# default theme keys if caller passes incomplete theme
_DEFAULT_THEME = {
    "accent": "#5ad1ff",
    "ok": "#7CFFB2",
    "warn": "#FFD479",
    "err": "#FF6B6B",
    "dim": "#5a6b7b",
}


@dataclass
class WorkflowAgent:
    name: str
    status: str = "idle"
    progress: float = 0.0
    blocked_by: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WorkflowRow:
    id: str
    kind: str  # swarm | task | board | hermes | agent
    title: str
    stage: str = "plan"
    progress: float = 0.0
    agents: list[WorkflowAgent] = field(default_factory=list)
    blocked_by: str | None = None
    age_s: float = 0.0
    cost_hint: str = ""
    next_action: str = "none"  # pause | cancel | rerun | act | none
    counts: dict[str, int] = field(default_factory=dict)  # e.g. board B/A/D
    pipeline: list[str] = field(default_factory=list)  # stage chips for strip

    def as_dict(self) -> dict[str, Any]:
        d = {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "stage": self.stage,
            "progress": round(max(0.0, min(1.0, self.progress)), 3),
            "agents": [a.as_dict() for a in self.agents],
            "blocked_by": self.blocked_by,
            "age_s": round(self.age_s, 1),
            "cost_hint": self.cost_hint,
            "next_action": self.next_action,
            "counts": dict(self.counts),
            "pipeline": list(self.pipeline),
        }
        return d


def stage_glyph(stage: str) -> str:
    return STAGE_GLYPH.get(stage, "·")


def stage_theme_key(stage: str) -> str:
    return STAGE_THEME.get(stage, "dim")


def _theme(theme: dict | None) -> dict:
    t = dict(_DEFAULT_THEME)
    if theme:
        t.update(theme)
    return t


def _task_stage(state: str) -> str:
    return {
        "pending": "plan",
        "running": "act",
        "done": "done",
        "failed": "failed",
        "cancelled": "done",
        "interrupted": "blocked",
    }.get(state, "plan")


def _swarm_stage(status: str) -> str:
    return {
        "idle": "plan",
        "planning": "plan",
        "working": "act",
        "waiting": "wait",
        "blocked": "blocked",
        "done": "done",
        "failed": "failed",
        "cancelled": "done",
    }.get(status, "plan")


def _pipeline_for_swarm(agents: Sequence[dict]) -> list[str]:
    """Build plan→act→wait→done chips from agent statuses."""
    if not agents:
        return ["plan"]
    statuses = {_swarm_stage(a.get("status", "idle")) for a in agents}
    chips: list[str] = []
    if "plan" in statuses or any(a.get("status") in ("idle", "planning") for a in agents):
        chips.append("plan")
    if "act" in statuses:
        chips.append("act")
    if "wait" in statuses or "blocked" in statuses:
        chips.append("wait" if "wait" in statuses else "blocked")
    if "failed" in statuses:
        chips.append("failed")
    if "done" in statuses and not (statuses - {"done", "plan"}):
        chips.append("done")
    elif "done" in statuses:
        chips.append("done")
    # ensure at least current worst stage
    if not chips:
        chips = ["plan"]
    return chips


def _aggregate_swarm_stage(agents: Sequence[dict]) -> tuple[str, float, str | None]:
    """Worst-case stage + mean progress + first blocked_by."""
    if not agents:
        return "plan", 0.0, None
    order = ["failed", "blocked", "wait", "act", "plan", "verify", "done"]
    stages = [_swarm_stage(a.get("status", "idle")) for a in agents]
    stage = "done"
    for s in order:
        if s in stages:
            stage = s
            break
    progs = [float(a.get("progress", 0) or 0) for a in agents]
    progress = sum(progs) / max(len(progs), 1)
    blocked_by = None
    for a in agents:
        if _swarm_stage(a.get("status", "idle")) in ("wait", "blocked"):
            deps = a.get("deps") or a.get("dependencies") or []
            if deps:
                blocked_by = str(deps[0])
                break
            if a.get("blocked_by"):
                blocked_by = str(a["blocked_by"])
                break
    return stage, progress, blocked_by


def _normalize_swarm(swarm_dashboard: Any, swarm_agents: Sequence[dict] | None) -> dict:
    """Return {agents, working, waiting, done, failed, blocked, total, active_plan}."""
    out: dict[str, Any] = {
        "agents": [],
        "working": 0, "waiting": 0, "done": 0, "failed": 0, "blocked": 0, "total": 0,
        "active_plan": None,
    }
    if isinstance(swarm_dashboard, dict):
        out["agents"] = list(swarm_dashboard.get("agents") or [])
        for k in ("working", "waiting", "done", "failed", "blocked", "total"):
            if k in swarm_dashboard:
                out[k] = int(swarm_dashboard.get(k) or 0)
        out["active_plan"] = swarm_dashboard.get("active_plan")
    if swarm_agents:
        # orchestrator agents win when provided (fresher / richer)
        out["agents"] = list(swarm_agents)
    if out["agents"] and not out["total"]:
        out["total"] = len(out["agents"])
        out["working"] = sum(1 for a in out["agents"] if a.get("status") == "working")
        out["waiting"] = sum(1 for a in out["agents"] if a.get("status") == "waiting")
        out["done"] = sum(1 for a in out["agents"] if a.get("status") == "done")
        out["failed"] = sum(1 for a in out["agents"] if a.get("status") == "failed")
        out["blocked"] = sum(1 for a in out["agents"] if a.get("status") == "blocked")
    return out


def collect_workflows(
    *,
    tasks: Sequence[Any] | None = None,
    swarm_dashboard: Any = None,
    swarm_agents: Sequence[dict] | None = None,
    boards: Sequence[dict] | None = None,
    hermes_agents: Sequence[dict] | None = None,
    agent_entities: Sequence[dict] | None = None,
    external_agents: Sequence[dict] | None = None,
    now: float | None = None,
) -> list[WorkflowRow]:
    """Build unified workflow rows from live sources. Idle-only sources omitted."""
    now = now if now is not None else time.time()
    rows: list[WorkflowRow] = []

    # ---- swarm ----
    sw = _normalize_swarm(swarm_dashboard, swarm_agents)
    agents = sw["agents"]
    live_swarm = any(
        a.get("status") not in ("done", "cancelled", "idle")
        or float(a.get("progress", 0) or 0) > 0
        for a in agents
    ) or sw["working"] or sw["waiting"] or sw["blocked"] or sw["failed"]
    # also show swarm if plan exists with agents
    if agents and (live_swarm or sw.get("active_plan") or sw["total"] > 0):
        # skip fully idle empty plan with only idle zero-progress agents
        all_idle = all(a.get("status") in ("idle", "done", "cancelled") for a in agents)
        any_work = any(a.get("status") in ("working", "waiting", "blocked", "planning", "failed")
                       for a in agents)
        if any_work or (agents and not all_idle) or sw["working"] or sw["failed"]:
            stage, progress, blocked_by = _aggregate_swarm_stage(agents)
            plan = sw.get("active_plan") or {}
            title = (plan.get("goal") if isinstance(plan, dict) else None) or ""
            if not title and agents:
                title = agents[0].get("goal") or agents[0].get("name") or "swarm"
            title = str(title)[:48]
            wa = [
                WorkflowAgent(
                    name=str(a.get("name", "?"))[:16],
                    status=str(a.get("status", "idle")),
                    progress=float(a.get("progress", 0) or 0),
                    blocked_by=(
                        (a.get("deps") or a.get("dependencies") or [None])[0]
                        if (a.get("deps") or a.get("dependencies")) else None
                    ),
                )
                for a in agents[:12]
            ]
            next_act = "cancel" if stage in ("act", "wait", "plan") else \
                       "rerun" if stage == "failed" else "none"
            rows.append(WorkflowRow(
                id="swarm",
                kind="swarm",
                title=title or "swarm",
                stage=stage,
                progress=progress,
                agents=wa,
                blocked_by=blocked_by,
                next_action=next_act,
                counts={
                    "working": sw["working"], "waiting": sw["waiting"],
                    "done": sw["done"], "failed": sw["failed"],
                    "blocked": sw["blocked"], "total": sw["total"] or len(agents),
                },
                pipeline=_pipeline_for_swarm(agents),
            ))

    # ---- harness tasks (running / pending / failed / interrupted) ----
    for t in tasks or []:
        if hasattr(t, "state"):
            st = t.state.value if hasattr(t.state, "value") else str(t.state)
            tid = t.id
            label = t.label
            harness = t.harness
            prog = float(getattr(t, "progress", 0) or 0)
            paused = bool(getattr(t, "paused", False))
            created = float(getattr(t, "created", now) or now)
        elif isinstance(t, dict):
            st = str(t.get("state", "pending"))
            tid = str(t.get("id", "?"))
            label = str(t.get("label", tid))
            harness = str(t.get("harness", "?"))
            prog = float(t.get("progress", 0) or 0)
            paused = bool(t.get("paused", False))
            created = float(t.get("created", now) or now)
        else:
            continue
        if st not in ("running", "pending", "failed", "interrupted"):
            continue
        stage = _task_stage(st)
        if paused and stage == "act":
            stage = "wait"
        next_act = "pause" if st == "running" and not paused else \
                   "rerun" if st in ("failed", "interrupted") else \
                   "cancel" if st in ("running", "pending") else "none"
        rows.append(WorkflowRow(
            id=f"task:{tid}",
            kind="task",
            title=f"{harness}:{label}"[:48],
            stage=stage,
            progress=prog,
            age_s=max(0.0, now - created),
            next_action=next_act,
            pipeline=["plan", "act", "done"] if stage != "failed" else ["plan", "act", "failed"],
        ))

    # ---- boards with open work ----
    for b in boards or []:
        if not isinstance(b, dict):
            continue
        col_data = b.get("column_data") or {}
        if not col_data and b.get("cards"):
            # build from flat cards
            col_data = {}
            for c in b.get("cards", []):
                col = c.get("column", "backlog")
                col_data.setdefault(col, []).append(c)
        backlog = len(col_data.get("backlog", []) or [])
        active = len(col_data.get("active", []) or [])
        done = len(col_data.get("done", []) or [])
        # also count nonstandard columns as active-ish if not done
        if not (backlog or active or done) and col_data:
            for name, cards in col_data.items():
                n = len(cards or [])
                if name == "done":
                    done += n
                elif name in ("active", "doing", "in_progress"):
                    active += n
                else:
                    backlog += n
        if backlog == 0 and active == 0:
            continue  # nothing open — not glance-critical
        if active:
            stage, progress = "act", active / max(active + backlog + done, 1)
        elif backlog:
            stage, progress = "plan", 0.0
        else:
            stage, progress = "done", 1.0
        title = str(b.get("title") or b.get("id") or "board")[:40]
        bid = str(b.get("id") or title)
        rows.append(WorkflowRow(
            id=f"board:{bid}",
            kind="board",
            title=title,
            stage=stage,
            progress=progress,
            counts={"backlog": backlog, "active": active, "done": done},
            next_action="none",
            pipeline=["plan", "act", "done"],
        ))

    # ---- Hermes live sessions ----
    for a in hermes_agents or []:
        if not isinstance(a, dict):
            continue
        where = a.get("branch") or a.get("repo") or ""
        model = str(a.get("model", "session")).split("/")[-1]
        title = (where or model)[:40]
        age = float(a.get("age_s", 0) or 0)
        rows.append(WorkflowRow(
            id=f"hermes:{a.get('id', title)}",
            kind="hermes",
            title=f"hermes:{title}",
            stage="act",
            progress=0.5,
            age_s=age,
            next_action="none",
            counts={"msgs": int(a.get("msgs", 0) or 0)},
            pipeline=["act"],
        ))

    # ---- persistent agent entities currently working ----
    for a in agent_entities or []:
        if not isinstance(a, dict):
            continue
        status = str(a.get("status", "idle"))
        task_status = str(a.get("task_status", "idle"))
        if status not in ("working", "blocked") and task_status in ("idle", "", "done"):
            continue
        stage = "blocked" if status == "blocked" else "act"
        if task_status == "failed":
            stage = "failed"
        title = str(a.get("goal") or a.get("task_label") or a.get("name") or "agent")[:48]
        rows.append(WorkflowRow(
            id=f"agent:{a.get('id', a.get('name', '?'))}",
            kind="agent",
            title=f"{a.get('name', '?')}:{title}"[:48],
            stage=stage,
            progress=float(a.get("task_progress", 0) or 0),
            next_action="cancel" if stage == "act" else "none",
            pipeline=["plan", "act", "done"],
        ))

    # ---- external coding agents (opencode, agy, claude, codex) ----
    for a in external_agents or []:
        if not isinstance(a, dict):
            continue
        name = str(a.get("name", "tool"))[:16]
        rows.append(WorkflowRow(
            id=f"tool:{name.lower()}",
            kind="tool",
            title=name,
            stage="act",
            progress=0.5,
            age_s=float(a.get("age_s", 0) or 0),
            next_action="none",
            pipeline=["act"],
        ))

    # priority: blocked/failed first, then act, then wait, then plan
    rank = {"failed": 0, "blocked": 1, "act": 2, "wait": 3, "verify": 4, "plan": 5, "done": 6}

    def sort_key(r: WorkflowRow) -> tuple:
        return (rank.get(r.stage, 9), -r.progress, r.kind, r.title)

    rows.sort(key=sort_key)
    return rows


# ── render helpers (rich markup) ─────────────────────────────────────────────

def pipeline_strip(pipeline: Sequence[str], current: str, theme: dict | None = None,
                   width: int = 28) -> str:
    """Compact Plan━● Act─○ Ver─○ style stage path."""
    th = _theme(theme)
    if not pipeline:
        pipeline = [current] if current else ["plan"]
    # normalize to display order without duplicates
    order = ["plan", "act", "wait", "blocked", "verify", "done", "failed"]
    chips = [s for s in order if s in pipeline or s == current]
    if current and current not in chips:
        chips.append(current)
    if not chips:
        chips = ["plan"]
    parts: list[str] = []
    for i, s in enumerate(chips):
        g = stage_glyph(s)
        key = stage_theme_key(s)
        col = th.get(key, th["dim"])
        label = s[:3].upper() if s not in ("blocked", "failed", "verify") else s[:4].upper()
        if s == current:
            parts.append(f"[{col}]{label}{g}[/]")
        elif rank_done(s, current):
            parts.append(f"[{th['ok']}]{label}✓[/]")
        else:
            parts.append(f"[{th['dim']}]{label}○[/]")
        if i < len(chips) - 1:
            parts.append(f"[{th['dim']}]─[/]")
    out = "".join(parts)
    # strip rich tags for length check is hard; just return
    return out


def rank_done(stage: str, current: str) -> bool:
    """Whether stage is strictly before current in happy path."""
    happy = ["plan", "act", "wait", "verify", "done"]
    if current in ("failed", "blocked"):
        return False
    try:
        return happy.index(stage) < happy.index(current) if current in happy and stage in happy else False
    except ValueError:
        return False


def workflow_types_summary(rows: Sequence[WorkflowRow] | Sequence[dict],
                           max_types: int = 4) -> str:
    """Compact summary of active workflow types, e.g. '2task 1swarm'."""
    kind_labels = {
        "task": "task", "swarm": "swarm", "hermes": "hermes",
        "board": "board", "agent": "agent", "tool": "tool",
    }
    live = [r for r in rows if _row_stage(r) not in ("done",)]
    if not live:
        return "idle"
    from collections import Counter
    kinds = Counter(_row_kind(r) for r in live)
    parts = []
    for k, c in kinds.most_common(max_types):
        label = kind_labels.get(k, k)
        parts.append(f"{c}{label}")
    return " ".join(parts)


def mission_strip(rows: Sequence[WorkflowRow] | Sequence[dict],
                  theme: dict | None = None,
                  max_title: int = 28) -> str:
    """Single header line for global mission awareness."""
    th = _theme(theme)
    a, ok, wa, er, di = th["accent"], th["ok"], th["warn"], th["err"], th["dim"]
    if not rows:
        return f"[{di}]MISSION[/] [{di}]idle · no agentic work[/]"
    live = [r for r in rows if _row_stage(r) not in ("done",)]
    n = len(live) if live else len(rows)
    top = (live or list(rows))[0]
    stage = _row_stage(top)
    title = _row_title(top)[:max_title]
    glyph = stage_glyph(stage)
    col = th.get(stage_theme_key(stage), di)
    pipe = pipeline_strip(_row_pipeline(top), stage, th)
    block = _row_blocked(top)
    block_s = f"  [{er}]⊘{block[:16]}[/]" if block else ""
    return (f"[{a}]MISSION[/] [{wa}]◆{n}[/] "
            f"[{col}]{glyph}[/]{title}  {pipe}{block_s}")


def workflow_pulse(rows: Sequence[WorkflowRow] | Sequence[dict],
                   theme: dict | None = None,
                   max_rows: int = 4) -> list[str]:
    """Right-rail Workflow Pulse lines."""
    th = _theme(theme)
    a, ok, wa, er, di = th["accent"], th["ok"], th["warn"], th["err"], th["dim"]
    lines: list[str] = []
    live = [r for r in rows if _row_stage(r) not in ("done",)] or list(rows)
    n_live = sum(1 for r in rows if _row_stage(r) in ("act", "wait", "blocked", "plan", "failed", "verify"))
    lines.append(f"[{a}]WORKFLOWS[/] [{wa if n_live else di}]◆{n_live} live[/]")
    if not rows:
        lines.append(f"[{di}](no agentic work)[/]")
        return lines
    for r in live[:max_rows]:
        stage = _row_stage(r)
        col = th.get(stage_theme_key(stage), di)
        g = stage_glyph(stage)
        kind = _row_kind(r)
        title = _row_title(r)[:22]
        prog = _row_progress(r)
        bar_w = 6
        filled = int(round(max(0.0, min(1.0, prog)) * bar_w))
        bar = f"[{col}]{'█' * filled}[/][{di}]{'░' * (bar_w - filled)}[/]"
        if kind == "board":
            c = _row_counts(r)
            detail = f"B{c.get('backlog', 0)} A{c.get('active', 0)} D{c.get('done', 0)}"
            lines.append(f"[{col}]{g}[/] [{di}]board[/] {title}  [{di}]{detail}[/]")
        elif kind == "swarm":
            c = _row_counts(r)
            detail = f"{c.get('working', 0)}W {c.get('waiting', 0)}⏳ {c.get('done', 0)}✓"
            block = _row_blocked(r)
            blk = f" [{er}]←{block[:10]}[/]" if block else ""
            lines.append(f"[{col}]{g}[/] [{a}]swarm[/] {title[:16]} {bar} [{di}]{detail}[/]{blk}")
        else:
            action = _row_action(r)
            hint = f" [{di}][{action[0]}][/]" if action and action != "none" else ""
            lines.append(f"[{col}]{g}[/] [{di}]{kind[:5]}[/] {title} {bar}{hint}")
    # anomaly line
    blocked = next((r for r in rows if _row_stage(r) == "blocked"), None)
    failed = next((r for r in rows if _row_stage(r) == "failed"), None)
    if blocked:
        bb = _row_blocked(blocked) or "dep"
        lines.append(f"[{er}]⚠ blocked[/] [{di}]{_row_title(blocked)[:20]} ← {bb[:14]}[/] [{di}][x][/]")
    elif failed:
        lines.append(f"[{er}]⚠ failed[/] [{di}]{_row_title(failed)[:24]}[/] [{di}][r] rerun[/]")
    return lines


def swarm_dag(agents: Sequence[dict], theme: dict | None = None) -> str:
    """ASCII dependency strip for swarm agents."""
    th = _theme(theme)
    a, ok, wa, er, di = th["accent"], th["ok"], th["warn"], th["err"], th["dim"]
    if not agents:
        return f"[{di}](no agents)[/]"
    by_name = {str(x.get("name", "")): x for x in agents}
    lines: list[str] = [f"[{a}]DAG[/]"]
    # roots: no deps or deps missing
    for ag in agents[:12]:
        name = str(ag.get("name", "?"))[:14]
        st = _swarm_stage(str(ag.get("status", "idle")))
        col = th.get(stage_theme_key(st), di)
        g = stage_glyph(st)
        prog = float(ag.get("progress", 0) or 0)
        bar_w = 6
        filled = int(round(prog * bar_w))
        bar = f"[{col}]{'█' * filled}[/][{di}]{'░' * (bar_w - filled)}[/]"
        deps = ag.get("deps") or ag.get("dependencies") or []
        if deps:
            dep_bits = []
            for d in deps[:3]:
                ds = str(d)
                dep_ag = by_name.get(ds)
                if dep_ag is None:
                    dep_bits.append(f"[{er}]{ds[:10]}?[/]")
                else:
                    dst = _swarm_stage(str(dep_ag.get("status", "idle")))
                    dcol = th.get(stage_theme_key(dst), di)
                    dep_bits.append(f"[{dcol}]{ds[:10]}{stage_glyph(dst)}[/]")
            dep_s = " ← " + ",".join(dep_bits)
        else:
            dep_s = ""
        lines.append(f"  [{col}]{g}[/] [{a}]{name:14s}[/] {bar}{dep_s}")
    return "\n".join(lines)


def board_glance(boards: Sequence[dict], theme: dict | None = None,
                 max_boards: int = 3) -> str:
    """3-up backlog/active/done counts per board."""
    th = _theme(theme)
    a, ok, wa, di = th["accent"], th["ok"], th["warn"], th["dim"]
    if not boards:
        return f"[{di}]No boards.[/]"
    lines = [f"[{a}]BOARD[/]"]
    for b in list(boards)[:max_boards]:
        title = str(b.get("title") or b.get("id") or "?")[:28]
        col_data = b.get("column_data") or {}
        if not col_data and b.get("cards"):
            col_data = {}
            for c in b.get("cards", []):
                col_data.setdefault(c.get("column", "backlog"), []).append(c)
        bl = len(col_data.get("backlog", []) or [])
        ac = len(col_data.get("active", []) or [])
        dn = len(col_data.get("done", []) or [])
        lines.append(
            f"  [{a}]⬡ {title}[/]  "
            f"[{di}]B{bl}[/] [{wa}]A{ac}[/] [{ok}]D{dn}[/]"
        )
        # one sample card per column
        for col_name, clr, icon in (
            ("backlog", di, "·"), ("active", wa, "⚡"), ("done", ok, "✓")
        ):
            cards = (col_data.get(col_name) or [])[:2]
            if not cards:
                continue
            for c in cards:
                tag = f" @{c.get('agent_id', '')[:6]}" if c.get("agent_id") else ""
                lines.append(f"    [{clr}]{icon}[/] [{di}]{str(c.get('title', ''))[:32]}{tag}[/]")
    return "\n".join(lines)


def desktop_missions(rows: Sequence[WorkflowRow] | Sequence[dict],
                     theme: dict | None = None,
                     max_rows: int = 3) -> str:
    """Desktop center MISSIONS block (replaces decorative viz when live)."""
    th = _theme(theme)
    a, wa, er, di = th["accent"], th["warn"], th["err"], th["dim"]
    lines = [f"[{a}]MISSIONS[/]"]
    if not rows:
        lines.append(f" [{di}](no agentic work)[/]")
        return "\n".join(lines)
    n_live = sum(1 for r in rows if _row_stage(r) in ("act", "wait", "blocked", "plan", "failed", "verify"))
    lines[0] = f"[{a}]MISSIONS[/]  [{wa if n_live else di}]{n_live} live[/]"
    for r in list(rows)[:max_rows]:
        stage = _row_stage(r)
        col = th.get(stage_theme_key(stage), di)
        g = stage_glyph(stage)
        title = _row_title(r)[:28]
        prog = _row_progress(r)
        bar_w = 8
        filled = int(round(max(0.0, min(1.0, prog)) * bar_w))
        bar = f"[{col}]{'█' * filled}[/][{di}]{'░' * (bar_w - filled)}[/]"
        block = _row_blocked(r)
        blk = f"  [{er}]⚠←{block[:12]}[/]" if block else ""
        lines.append(f" [{col}]{g}[/] {title} {bar}{blk}")
    return "\n".join(lines)


# ── row field accessors (WorkflowRow | dict) ─────────────────────────────────

def _row_stage(r: Any) -> str:
    return r.stage if isinstance(r, WorkflowRow) else str(r.get("stage", "plan"))


def _row_title(r: Any) -> str:
    return r.title if isinstance(r, WorkflowRow) else str(r.get("title", "?"))


def _row_kind(r: Any) -> str:
    return r.kind if isinstance(r, WorkflowRow) else str(r.get("kind", "?"))


def _row_progress(r: Any) -> float:
    return float(r.progress if isinstance(r, WorkflowRow) else r.get("progress", 0) or 0)


def _row_blocked(r: Any) -> str | None:
    b = r.blocked_by if isinstance(r, WorkflowRow) else r.get("blocked_by")
    return str(b) if b else None


def _row_pipeline(r: Any) -> list[str]:
    return list(r.pipeline if isinstance(r, WorkflowRow) else r.get("pipeline") or [])


def _row_counts(r: Any) -> dict:
    return dict(r.counts if isinstance(r, WorkflowRow) else r.get("counts") or {})


def _row_action(r: Any) -> str:
    return r.next_action if isinstance(r, WorkflowRow) else str(r.get("next_action", "none"))
