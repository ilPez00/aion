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
import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
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
    # Agent NAMES we wait for -- not ids. `agents_ready`/`blocked_agents`/
    # `_is_done` all resolve these through `agent_by_name`, and `swarm add
    # <name> <goal> << dep1,dep2` takes names from the user. The comment here
    # used to say "ids", which is how the HUD first drew zero dependency
    # edges: it looked up `s<dep>` as an id and matched nothing.
    dependencies: list[str] = field(default_factory=list)
    # Which harness runs this step. Empty means "the runner's default", so a
    # swarm stays usable without anyone choosing per-step models -- but a
    # research step and a code step wanting different harnesses is the normal
    # case, not an exotic one.
    harness: str = ""
    # Which fleet instance runs this step. Empty means here. A DAG can span
    # machines: a heavy step on the workstation, the rest wherever you are.
    instance: str = ""
    # The task doing this agent's work right now. An agent is not a process --
    # it owns a task id -- and that id has to survive a checkpoint, or a
    # restart cannot tell "this step is running on the workstation" from
    # "this step has not started", and starts it a second time.
    task_id: str = ""
    # How many times this step has been RUN, and the earliest moment it may be
    # run again. Both are checkpointed: a restart that reset the count would
    # turn a bounded retry policy into an unbounded one, which is the exact
    # failure the bound exists to prevent.
    attempts: int = 0
    retry_at: float = 0.0            # epoch seconds; 0 = now
    # How many rounds of replanning stand between this step and the plan a
    # human approved. 0 = a human wrote it. Checkpointed, because it is the
    # bound on recursive self-expansion: a restart that reset it would let a
    # swarm that had reached its depth limit start growing again.
    generation: int = 0
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
            "deps": self.dependencies, "harness": self.harness,
            "instance": self.instance, "task_id": self.task_id,
            "attempts": self.attempts, "retry_at": self.retry_at,
            "generation": self.generation,
            "parent": self.parent_id,
            "subs": len(self.sub_agents),
            "has_output": bool(self.output),
            "has_error": bool(self.error),
            "age_s": round(time.time() - self.created, 1),
        }

    def as_record(self) -> dict:
        """Full form for the on-disk checkpoint.

        A superset of `as_dict()` with the same key names, so anything already
        reading the display shape (the HUD's process graph) reads a checkpoint
        unchanged. `as_dict` truncates the goal and drops logs/output/error —
        fine for a dashboard, lossy for something we intend to reload.
        """
        return {
            **self.as_dict(),
            "goal": self.goal,                 # untruncated
            "output": self.output, "error": self.error,
            "logs": self.logs[-50:],
            "sub_agents": self.sub_agents,
            "created": self.created, "started": self.started,
            "completed": self.completed,
        }

    @classmethod
    def from_record(cls, d: dict) -> "SwarmAgent":
        """Rebuild from a checkpoint. Unknown status falls back to IDLE."""
        try:
            status = AgentStatus(d.get("status", "idle"))
        except ValueError:
            status = AgentStatus.IDLE
        instance = str(d.get("instance", "") or "")
        task_id = str(d.get("task_id", "") or "")
        # A LOCAL step that was mid-flight cannot be resumed -- the harness
        # coroutine died with the process -- so it comes back IDLE and
        # eligible to re-run rather than lying about still working.
        #
        # A REMOTE one is the opposite: the peer never noticed we died and is
        # still working. Resetting it to IDLE is not conservative, it is a
        # second copy of the same job -- double spend, and whatever side
        # effects that step has, twice. It stays WORKING and the runner
        # re-attaches its watch.
        if status in (AgentStatus.WORKING, AgentStatus.PLANNING):
            if not (instance and task_id):
                status = AgentStatus.IDLE
        return cls(
            id=str(d["id"]), name=str(d.get("name", d["id"])),
            goal=str(d.get("goal", "")), status=status,
            dependencies=[str(x) for x in (d.get("deps") or [])],
            harness=str(d.get("harness", "") or ""),
            instance=instance, task_id=task_id,
            attempts=int(d.get("attempts", 0) or 0),
            retry_at=float(d.get("retry_at", 0) or 0.0),
            generation=int(d.get("generation", 0) or 0),
            parent_id=d.get("parent"),
            sub_agents=[str(x) for x in (d.get("sub_agents") or [])],
            progress=float(d.get("progress", 0.0) or 0.0),
            output=str(d.get("output", "")), error=str(d.get("error", "")),
            created=float(d.get("created", 0) or time.time()),
            started=d.get("started"), completed=d.get("completed"),
            logs=[str(x) for x in (d.get("logs") or [])],
        )


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


class SwarmStore:
    """Crash-safe persistence for the swarm, mirroring `core.SessionStore`.

    The orchestrator was memory-only, so a dependency DAG — the most
    graph-shaped data aion has — vanished the moment the cockpit exited, and
    the web HUD (a different process) could never see it at all. This writes
    the same `swarm.json` that `procgraph.read_swarm` already looks for, so
    the DAG appears in the process graph with no change on the reader side.

    Per-instance, like the session store: a swarm belongs to the cockpit that
    spawned it, and two cockpits on one machine must not share the file.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        from .fleet import instance_path
        self.path = Path(path or instance_path("swarm.json"))
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def save(self, agents: dict[str, "SwarmAgent"]) -> None:
        from .fleet import write_json_atomic
        try:
            write_json_atomic(self.path, [a.as_record() for a in agents.values()])
        except Exception as e:  # noqa: BLE001
            print(f"[swarm] save failed: {e}")

    def load(self) -> list["SwarmAgent"]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text())
        except Exception:  # noqa: BLE001
            return []
        out: list[SwarmAgent] = []
        for d in raw if isinstance(raw, list) else []:
            try:
                out.append(SwarmAgent.from_record(d))
            except Exception:  # noqa: BLE001
                continue        # one bad record must not lose the whole swarm
        return out

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            print(f"[swarm] clear failed: {e}")


class SwarmOrchestrator:
    """Coordinates a swarm of agents working on a shared goal.

    Usage:
        orch = SwarmOrchestrator(bus)
        orch.decompose("research + build a terminal dashboard")
        orch.agent("Agent A", "search web for TUI libs")
        orch.agent("Agent B", "find python plot libraries", deps=["Agent A"])
        await orch.run_all()
    """

    def __init__(self, bus: Bus | None = None,
                 store: "SwarmStore | None" = None,
                 persist: bool = True) -> None:
        self.bus = bus or Bus()
        self.agents: dict[str, SwarmAgent] = {}
        self.plans: list[SwarmPlan] = []
        self._running = False
        self._paused = False
        # `persist=False` keeps unit tests off the real ~/.aion; the store is
        # created lazily so constructing an orchestrator never touches disk.
        self._persist = persist
        self._store = store
        if store is not None:
            self._persist = True

    @property
    def store(self) -> "SwarmStore | None":
        if not self._persist:
            return None
        if self._store is None:
            self._store = SwarmStore()
        return self._store

    def checkpoint(self) -> None:
        """Write the swarm to disk. Called on every mutation."""
        st = self.store
        if st is not None:
            st.save(self.agents)

    def restore(self) -> int:
        """Reload a checkpointed swarm. Returns how many agents came back."""
        st = self.store
        if st is None:
            return 0
        loaded = st.load()
        for a in loaded:
            self.agents[a.id] = a
        return len(loaded)

    # ---- Agent management ------------------------------------------------

    def add_agent(self, name: str, goal: str, deps: list[str] | None = None,
                  parent: str | None = None, harness: str = "",
                  instance: str = "") -> SwarmAgent:
        aid = f"swarm_{uuid.uuid4().hex[:8]}"
        a = SwarmAgent(
            id=aid, name=name, goal=goal,
            dependencies=deps or [],
            harness=harness,
            instance=instance,
            parent_id=parent,
        )
        self.agents[aid] = a
        self.checkpoint()
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
        self.checkpoint()

    # ---- Orchestration ---------------------------------------------------

    def decompose(self, goal: str, steps: list[dict] | None = None) -> SwarmPlan:
        """Create a plan from a high-level goal. If steps not provided,
        create a stub plan the user can fill."""
        plan = SwarmPlan(goal=goal, steps=steps or [])
        self.plans.append(plan)
        return plan

    def agents_ready(self) -> list[SwarmAgent]:
        """Agents that can start right now: idle, with every dependency DONE.

        This used to accept a dependency that had FAILED or been CANCELLED as
        satisfied, because it asked `_is_done`, which means "reached a terminal
        state". So `swarm run` would start a step whose input never arrived —
        and `blocked_agents()` would report the very same agent as blocked, a
        flat contradiction between two functions over one graph. Readiness now
        asks the same question the blocked list asks.
        """
        return [a for a in self.agents.values()
                if self.dep_state(a)[0] == "ready"]

    def dep_state(self, a: SwarmAgent) -> tuple[str, str]:
        """(state, reason) for one agent's dependencies.

        One place, so "ready", "waiting" and "blocked" cannot disagree. The
        reason names the dependency, because "blocked" on its own tells you
        nothing about which of five steps to go and look at.
        """
        if a.status != AgentStatus.IDLE:
            return "not-idle", f"status is {a.status.value}"
        for dep in a.dependencies:
            other = self.agent_by_name(dep)
            if other is None:
                return "blocked", f"depends on {dep!r}, which does not exist"
            if other.status == AgentStatus.FAILED:
                return "blocked", f"{dep} failed"
            if other.status == AgentStatus.CANCELLED:
                return "blocked", f"{dep} was cancelled"
            if other.status != AgentStatus.DONE:
                return "waiting", f"waiting for {dep} ({other.status.value})"
        return "ready", ""

    def agents_by_status(self, status: AgentStatus) -> list[SwarmAgent]:
        return [a for a in self.agents.values() if a.status == status]

    def blocked_agents(self) -> list[SwarmAgent]:
        """Agents stuck on a dependency that failed, was cancelled, or is gone."""
        return [a for a in self.agents.values()
                if self.dep_state(a)[0] == "blocked"]

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

    # ---- Control ---------------------------------------------------------
    # Per-agent verbs, so the swarm can be steered from the web HUD rather than
    # only from `swarm run` / `swarm stop`, which were all-or-nothing. Same
    # shape as agentctl: legality is a pure question with a REASON attached,
    # and a refusal always says which dependency or which state is in the way.

    # pause/resume are absent on purpose: an agent has no execution of its own
    # to suspend, only the task it owns, and this class does not know about
    # tasks. `agentctl.DELEGATED` names them and `agentctl.route` decides them
    # against the real task state; the store does the delegating.
    ACTIONS = ("start", "cancel", "retry", "remove")

    def can(self, agent_id: str, action: str) -> tuple[bool, str]:
        from . import agentctl

        a = self.agents.get(agent_id)
        if a is None:
            return False, "no such agent"
        if action not in self.ACTIONS:
            return False, f"unknown action {action!r}"
        state = a.status

        if action == "start":
            if state != AgentStatus.IDLE:
                return False, f"only an idle agent can start (this is {state.value})"
            kind, why = self.dep_state(a)
            return (True, "") if kind == "ready" else (False, why)

        if action == "cancel":
            # Which states are over is a task question, answered once in
            # agentctl. A second list here is a second thing to forget to
            # update when a state is added.
            return agentctl.legal("cancel", agentctl.AGENT_AS_TASK.get(
                state.value, state.value))

        if action == "retry":
            # Same set as a task rerun, minus "interrupted", which no agent
            # ever reaches — so the shared constant is the honest source.
            if state.value not in agentctl.RERUNNABLE:
                return False, f"only a failed or cancelled agent can be retried "\
                              f"(this is {state.value})"
            return True, ""

        # remove: refuse while anything still names it. Deleting a node out of
        # a DAG that others point at leaves those pointing at nothing, and the
        # dependency is by NAME, so it would silently become unsatisfiable.
        dependents = sorted(o.name for o in self.agents.values()
                            if a.name in o.dependencies and o.id != a.id)
        if dependents:
            verb = "depends" if len(dependents) == 1 else "depend"
            return False, f"{', '.join(dependents)} {verb} on it"
        return True, ""

    @staticmethod
    def _as_agent(out: dict) -> dict:
        """Outcome carries its subject as `task_id`; these are agents.

        Leaving the key alone would have the swarm endpoint answering with a
        task id that is not a task, which is the sort of thing that reads fine
        until someone writes a client against it.
        """
        out = dict(out)
        out["agent_id"] = out.pop("task_id", "")
        return out

    def control(self, agent_id: str, action: str) -> dict:
        """Apply an action to one agent. Returns an agentctl.Outcome dict."""
        from .agentctl import Outcome

        ok, why = self.can(agent_id, action)
        a = self.agents.get(agent_id)
        state = a.status.value if a else ""
        if not ok:
            return self._as_agent(Outcome(False, action, agent_id, why, state).as_dict())

        if action == "start":
            self.set_status(agent_id, AgentStatus.WORKING)
        elif action == "cancel":
            self.set_status(agent_id, AgentStatus.CANCELLED)
        elif action == "retry":
            # Back to idle rather than straight to working: its dependencies
            # may have changed since it failed, and `start` is the thing that
            # checks them.
            a.error = ""
            a.progress = 0.0
            a.completed = None
            a.started = None
            # A human retry is a fresh decision, so the automatic policy's
            # attempt count starts over and the backoff is waived. Anything
            # else would have someone press retry and watch nothing happen for
            # four minutes, or press it on a step that has no attempts left
            # and watch it fail again immediately.
            a.attempts = 0
            a.retry_at = 0.0
            self.set_status(agent_id, AgentStatus.IDLE)
        elif action == "remove":
            self.agents.pop(agent_id, None)
            self.checkpoint()
            return self._as_agent(
                Outcome(True, action, agent_id, "", "removed").as_dict())

        self.log(agent_id, f"[control] {action}")
        return self._as_agent(Outcome(
            True, action, agent_id, "", self.agents[agent_id].status.value).as_dict())

    def run_ready(self) -> dict:
        """Start every agent whose dependencies are satisfied."""
        ready = self.agents_ready()
        for a in ready:
            self.set_status(a.id, AgentStatus.WORKING)
        blocked = self.blocked_agents()
        return {"ok": bool(ready), "started": [a.name for a in ready],
                "blocked": [{"name": a.name, "reason": self.dep_state(a)[1]}
                            for a in blocked],
                "reason": "" if ready else (
                    "nothing is ready" + (f" — {len(blocked)} blocked" if blocked else ""))}

    def stop_all(self) -> dict:
        """Cancel everything currently in flight. Idle agents are left alone."""
        live = (AgentStatus.WORKING, AgentStatus.PLANNING, AgentStatus.WAITING)
        stopped = [a for a in self.agents.values() if a.status in live]
        for a in stopped:
            self.set_status(a.id, AgentStatus.CANCELLED)
        return {"ok": True, "stopped": [a.name for a in stopped]}

    def add_checked(self, name: str, goal: str,
                    deps: list[str] | None = None, harness: str = "",
                    instance: str = "") -> dict:
        """add_agent with the validation a public entry point needs.

        Names are the dependency key, so a duplicate name does not create a
        second agent — it creates an ambiguity that `agent_by_name` resolves
        arbitrarily, and every dependency on that name becomes a coin flip.
        """
        from .agentctl import Outcome

        name = (name or "").strip()
        goal = (goal or "").strip()
        if not name:
            return self._as_agent(Outcome(False, "add", reason="a name is required").as_dict())
        if not goal:
            return self._as_agent(Outcome(False, "add", reason="a goal is required").as_dict())
        if self.agent_by_name(name) is not None:
            return self._as_agent(Outcome(False, "add", reason=(
                f"an agent named {name!r} already exists — dependencies are "
                f"by name, so two would be ambiguous")).as_dict())
        clean, missing = [], []
        for d in (deps or []):
            d = str(d).strip()
            if not d:
                continue
            (clean if self.agent_by_name(d) else missing).append(d)
        if missing:
            return self._as_agent(Outcome(False, "add", reason=(
                f"no agent named {', '.join(missing)} — add dependencies "
                f"before what depends on them")).as_dict())
        a = self.add_agent(name, goal, deps=clean, harness=harness,
                           instance=instance)
        out = self._as_agent(Outcome(True, "add", a.id, "", a.status.value).as_dict())
        out["name"] = a.name
        return out

    def _is_done(self, name: str) -> bool:
        a = self.agent_by_name(name)
        return a is not None and a.status in (AgentStatus.DONE, AgentStatus.FAILED, AgentStatus.CANCELLED)

    # ---- Bus integration -------------------------------------------------

    def _publish(self) -> None:
        # Checkpoint first: the bus publish is fire-and-forget on the event
        # loop, so a crash between the two would otherwise lose the change
        # that was just broadcast.
        self.checkpoint()
        if not self.bus:
            return
        # `asyncio.ensure_future` needs a loop, and raises RuntimeError when
        # there isn't one. Mutating a swarm from synchronous code — a CLI, a
        # test, a restore path — is legitimate and must not blow up just
        # because nobody is listening on the bus. The state is already on
        # disk by this point, so dropping the notification loses nothing but
        # the live nudge.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._async_publish())

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
