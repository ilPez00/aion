"""
swarm.py — Multi-agent swarm orchestration for aion.

Coordinates multiple sub-agents on related goals, tracks their progress,
and surfaces a unified dashboard. Inspired by OpenAI Swarm, CrewAI, and
PewDiePie's Odysseus multi-agent loops.

Architecture:
- A Swarm is a collection of Agent nodes with dependencies
- Each Agent has a goal, status, and can spawn sub-tasks
- The orchestrator polls progress and detects stuck/dead agents
- Agents communicate via the bus (TOPIC_SWARM)
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable

from .core import Bus, TOPIC_STATS


class AgentStatus(Enum):
    IDLE = "idle"
    PLANNING = "planning"
    WORKING = "working"
    WAITING = "waiting"      # waiting on dependency
    BLOCKED = "blocked"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class SwarmAgent:
    id: str
    name: str
    goal: str
    status: AgentStatus = AgentStatus.IDLE
    dependencies: list[str] = field(default_factory=list)  # agent ids we wait for
    parent_id: str | None = None
    sub_agents: list[str] = field(default_factory=list)
    progress: float = 0.0            # 0..1
    output: str = ""
    error: str = ""
    created: float = field(default_factory=time.time)
    started: float | None = None
    completed: float | None = None
    logs: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "goal": self.goal[:80],
            "status": self.status.value, "progress": round(self.progress, 2),
            "deps": self.dependencies, "parent": self.parent_id,
            "subs": len(self.sub_agents),
            "has_output": bool(self.output),
            "has_error": bool(self.error),
            "age_s": round(time.time() - self.created, 1),
        }


@dataclass
class SwarmPlan:
    """A structured plan the orchestrator decomposes a goal into."""
    goal: str
    steps: list[dict] = field(default_factory=list)  # [{"name", "prompt", "deps": []}]
    created: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return {
            "goal": self.goal[:80],
            "steps": len(self.steps),
            "done": sum(1 for s in self.steps if s.get("done")),
            "created": self.created,
        }


class SwarmOrchestrator:
    """Coordinates a swarm of agents working on a shared goal.

    Usage:
        orch = SwarmOrchestrator(bus)
        orch.decompose("research + build a terminal dashboard")
        orch.agent("Agent A", "search web for TUI libs")
        orch.agent("Agent B", "find python plot libraries", deps=["Agent A"])
        await orch.run_all()
    """

    def __init__(self, bus: Bus | None = None) -> None:
        self.bus = bus or Bus()
        self.agents: dict[str, SwarmAgent] = {}
        self.plans: list[SwarmPlan] = []
        self._running = False
        self._paused = False

    # ---- Agent management ------------------------------------------------

    def add_agent(self, name: str, goal: str, deps: list[str] | None = None,
                  parent: str | None = None) -> SwarmAgent:
        aid = f"swarm_{uuid.uuid4().hex[:8]}"
        a = SwarmAgent(
            id=aid, name=name, goal=goal,
            dependencies=deps or [],
            parent_id=parent,
        )
        self.agents[aid] = a
        return a

    def agent_by_name(self, name: str) -> SwarmAgent | None:
        for a in self.agents.values():
            if a.name == name:
                return a
        return None

    def set_status(self, agent_id: str, status: AgentStatus) -> None:
        a = self.agents.get(agent_id)
        if a is None:
            return
        a.status = status
        if status == AgentStatus.WORKING and a.started is None:
            a.started = time.time()
        if status in (AgentStatus.DONE, AgentStatus.FAILED, AgentStatus.CANCELLED):
            a.completed = time.time()
        self._publish()

    def set_progress(self, agent_id: str, progress: float) -> None:
        a = self.agents.get(agent_id)
        if a is None:
            return
        a.progress = max(0.0, min(1.0, progress))
        self._publish()

    def log(self, agent_id: str, line: str) -> None:
        a = self.agents.get(agent_id)
        if a is None:
            return
        a.logs.append(line)
        a.logs = a.logs[-50:]

    # ---- Orchestration ---------------------------------------------------

    def decompose(self, goal: str, steps: list[dict] | None = None) -> SwarmPlan:
        """Create a plan from a high-level goal. If steps not provided,
        create a stub plan the user can fill."""
        plan = SwarmPlan(goal=goal, steps=steps or [])
        self.plans.append(plan)
        return plan

    def agents_ready(self) -> list[SwarmAgent]:
        """Return agents whose dependencies are all satisfied."""
        ready = []
        for a in self.agents.values():
            if a.status != AgentStatus.IDLE:
                continue
            if all(self._is_done(dep) for dep in a.dependencies):
                ready.append(a)
        return ready

    def agents_by_status(self, status: AgentStatus) -> list[SwarmAgent]:
        return [a for a in self.agents.values() if a.status == status]

    def blocked_agents(self) -> list[SwarmAgent]:
        """Agents stuck waiting on a dependency that failed or doesn't exist."""
        blocked = []
        for a in self.agents.values():
            if a.status != AgentStatus.IDLE:
                continue
            for dep in a.dependencies:
                dep_agent = self.agent_by_name(dep)
                if dep_agent is None or dep_agent.status == AgentStatus.FAILED:
                    blocked.append(a)
                    break
        return blocked

    def status_summary(self) -> dict:
        """Dashboard-ready summary."""
        total = len(self.agents)
        return {
            "total": total,
            "working": len(self.agents_by_status(AgentStatus.WORKING)),
            "waiting": len(self.agents_by_status(AgentStatus.WAITING)),
            "done": len(self.agents_by_status(AgentStatus.DONE)),
            "failed": len(self.agents_by_status(AgentStatus.FAILED)),
            "idle": len(self.agents_by_status(AgentStatus.IDLE)),
            "blocked": len(self.blocked_agents()),
            "plans": len(self.plans),
            "active_plan": self.plans[-1].as_dict() if self.plans else None,
        }

    def _is_done(self, name: str) -> bool:
        a = self.agent_by_name(name)
        return a is not None and a.status in (AgentStatus.DONE, AgentStatus.FAILED, AgentStatus.CANCELLED)

    # ---- Bus integration -------------------------------------------------

    def _publish(self) -> None:
        if self.bus:
            asyncio.ensure_future(self._async_publish())

    async def _async_publish(self) -> None:
        summary = self.status_summary()
        summary["agents"] = [a.as_dict() for a in self.agents.values()]
        await self.bus.publish(TOPIC_STATS, {"harness": "swarm", "metrics": summary})

    def dashboard(self) -> str:
        """Rich text dashboard of the swarm state."""
        lines = ["╔══ SWARM DASHBOARD ═══════════════════╗"]
        summary = self.status_summary()
        counts = f"🧠 {summary['working']}W · {summary['waiting']}⏳ · {summary['done']}✓ · {summary['failed']}✗ · {summary['blocked']}⊘"
        lines.append(f"║ {counts:>34} ║")
        lines.append("╠════════════════════════════════════╣")
        for a in self.agents.values():
            icon = {"idle": "○", "working": "●", "waiting": "⏳", "done": "✓",
                    "failed": "✗", "blocked": "⊘", "cancelled": "—", "planning": "◇"}.get(a.status.value, "?")
            bar_len = 10
            filled = int(a.progress * bar_len)
            bar = "█" * filled + "░" * (bar_len - filled)
            dep_str = f" ←{','.join(a.dependencies)}" if a.dependencies else ""
            lines.append(f"║ {icon} {a.name[:20]:20s} {bar} {a.goal[:30]:30s}{dep_str} ║")
        lines.append("╚════════════════════════════════════╝")
        return "\n".join(lines)
