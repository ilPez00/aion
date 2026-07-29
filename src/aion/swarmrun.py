"""swarmrun.py — the thing that actually runs a swarm.

`SwarmOrchestrator` tracked a dependency DAG faithfully and never advanced it.
Nothing in the repo set an agent to DONE, so `run_ready()` moved layer one to
WORKING and layer two waited forever; `run_all()` existed only in a docstring.
The DAG was a whiteboard.

This is the bridge, and it is deliberately small: a swarm agent's goal is a
prompt, and a harness already runs prompts. So an agent does not get a new
execution engine — it gets a task, spawned through the cockpit's normal path,
which means it inherits HITL gates, physis classification, checkpointing,
cancellation and the task log for free. When that task reaches a terminal
state the agent follows it, and the scheduler runs again.

    admit()      pure. who may start, given deps, parallelism and VRAM.
    prompt_for() pure. what an agent is actually asked, including upstream
                 output — the thing that makes a dependency mean more than
                 "wait for".
    SwarmRunner  the only part that touches the bus: spawn, watch, advance.

The split matters because scheduling is where the interesting mistakes live
(starvation, over-admission, a stuck layer) and none of them need an event
loop to reproduce.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# A swarm with no limit will start every ready agent at once. On one GPU that
# is slower than running them in sequence, and on an API-backed harness it is
# a burst of concurrent spend. Both defaults are deliberately timid.
DEFAULT_MAX_PARALLEL = 3
DEFAULT_VRAM_MB = 0          # 0 = unlimited; harnesses declare vram_mb


@dataclass
class Slot:
    """One agent being considered for admission."""
    id: str
    name: str = ""
    harness: str = ""
    vram_mb: int = 0


@dataclass
class Admission:
    admit: list[str] = field(default_factory=list)
    deferred: list[dict] = field(default_factory=list)   # {id, name, reason}

    def as_dict(self) -> dict:
        return {"admit": list(self.admit), "deferred": list(self.deferred)}


def admit(slots: list[Slot], *, running: int = 0,
          max_parallel: int = DEFAULT_MAX_PARALLEL,
          vram_total: int = DEFAULT_VRAM_MB,
          vram_used: int = 0) -> Admission:
    """Which of these ready agents may start right now. Pure.

    Rules, in order:

      * `max_parallel` counts work ALREADY running, not just this batch.
        Counting only the batch is how a scheduler ends up with three agents
        per tick regardless of what is in flight.
      * VRAM is a budget, not a filter: agents are admitted while the running
        total fits. `vram_total = 0` means unlimited, because most harnesses
        are API-backed and declare nothing.
      * An agent that can NEVER fit — its own requirement exceeds the whole
        budget — is called out by name rather than deferred forever. Silent
        starvation is the worst failure a scheduler has, because the DAG just
        stops and nothing anywhere says why.

    Order is the caller's order, so a tick is reproducible.
    """
    out = Admission()
    if max_parallel < 1:
        max_parallel = 1
    free_slots = max_parallel - max(0, running)
    used = max(0, vram_used)

    for s in slots:
        need = max(0, int(s.vram_mb or 0))
        if vram_total and need > vram_total:
            out.deferred.append({
                "id": s.id, "name": s.name,
                "reason": (f"needs {need}MB but the whole budget is "
                           f"{vram_total}MB — it can never be admitted")})
            continue
        if free_slots <= 0:
            out.deferred.append({
                "id": s.id, "name": s.name,
                "reason": f"at the parallel limit ({max_parallel})"})
            continue
        if vram_total and used + need > vram_total:
            out.deferred.append({
                "id": s.id, "name": s.name,
                "reason": (f"{used + need}MB would exceed the {vram_total}MB "
                           f"VRAM budget")})
            continue
        out.admit.append(s.id)
        free_slots -= 1
        used += need
    return out


# ── prompts ──────────────────────────────────────────────────────────────────
# A dependency that only means "wait for" wastes most of what a DAG is for.
# The upstream output is the reason `writer` waits for `scout`, so it goes into
# the prompt. Truncated per-dependency rather than in total, so one chatty
# upstream cannot crowd every other one out of the context.
UPSTREAM_BUDGET_CHARS = 2000


def prompt_for(goal: str, upstream: list[tuple[str, str]],
               budget: int = UPSTREAM_BUDGET_CHARS) -> str:
    """The prompt an agent is actually given. Pure.

    `upstream` is [(name, output)] in dependency order. Dependencies that
    produced nothing are named anyway: "scout finished and produced no output"
    is information, and hiding it makes a downstream agent hallucinate an
    input it never received.
    """
    goal = (goal or "").strip()
    if not upstream:
        return goal
    share = max(200, budget // max(1, len(upstream)))
    blocks = []
    for name, text in upstream:
        text = (text or "").strip()
        if not text:
            blocks.append(f"### {name}\n(finished, produced no output)")
            continue
        clipped = text[:share]
        if len(text) > share:
            clipped += f"\n… [{len(text) - share} more characters truncated]"
        blocks.append(f"### {name}\n{clipped}")
    joined = "\n\n".join(blocks)
    return (f"{goal}\n\n"
            f"---\nContext from the steps this depends on:\n\n{joined}")


# ── runner ───────────────────────────────────────────────────────────────────
class SwarmRunner:
    """Drives a SwarmOrchestrator by spawning real tasks.

    Deliberately dependency-injected rather than reaching into a Store: the
    only things it needs are "start this prompt on this harness, give me a task
    id" and "tell me when a task ends". That keeps it testable with two small
    fakes, and keeps execution policy out of the orchestrator, which should
    stay a data structure.
    """

    def __init__(self, orchestrator, spawn, *, harness: str = "",
                 max_parallel: int = DEFAULT_MAX_PARALLEL,
                 vram_total: int = DEFAULT_VRAM_MB,
                 harness_vram=None) -> None:
        self.swarm = orchestrator
        self.spawn = spawn                 # (agent, prompt) -> task_id | None
        self.harness = harness
        self.max_parallel = max_parallel
        self.vram_total = vram_total
        self._harness_vram = harness_vram or (lambda hid: 0)
        # agent id <-> task id, both ways: one to advance the agent when its
        # task ends, the other to find the task when the agent is cancelled.
        self.task_of: dict[str, str] = {}
        self.agent_of: dict[str, str] = {}
        self.last: Admission = Admission()

    # -- scheduling --------------------------------------------------------
    def _slots(self) -> list[Slot]:
        from .swarm import AgentStatus
        out = []
        for a in self.swarm.agents.values():
            if a.status is not AgentStatus.IDLE:
                continue
            if self.swarm.dep_state(a)[0] != "ready":
                continue
            hid = getattr(a, "harness", "") or self.harness
            out.append(Slot(id=a.id, name=a.name, harness=hid,
                            vram_mb=self._harness_vram(hid)))
        return out

    def _in_flight(self) -> int:
        from .swarm import AgentStatus
        return sum(1 for a in self.swarm.agents.values()
                   if a.status is AgentStatus.WORKING)

    def _vram_used(self) -> int:
        from .swarm import AgentStatus
        total = 0
        for a in self.swarm.agents.values():
            if a.status is AgentStatus.WORKING:
                total += self._harness_vram(getattr(a, "harness", "") or self.harness)
        return total

    def pump(self) -> dict:
        """One scheduler tick: admit what fits, spawn it, report the rest.

        Safe to call as often as you like — it is driven by agent state, not
        by a queue, so a duplicate tick admits nothing twice.
        """
        from .swarm import AgentStatus

        plan = admit(self._slots(), running=self._in_flight(),
                     max_parallel=self.max_parallel,
                     vram_total=self.vram_total, vram_used=self._vram_used())
        self.last = plan
        started = []
        for aid in plan.admit:
            agent = self.swarm.agents.get(aid)
            if agent is None:
                continue
            prompt = prompt_for(agent.goal, self.upstream_of(agent))
            # WORKING before spawning: a synchronous spawn that completes
            # immediately would otherwise find the agent still IDLE and the
            # completion would be dropped.
            self.swarm.set_status(aid, AgentStatus.WORKING)
            try:
                task_id = self.spawn(agent, prompt)
            except Exception as e:  # noqa: BLE001
                self.fail(aid, f"could not start: {type(e).__name__}: {str(e)[:120]}")
                continue
            if not task_id:
                self.fail(aid, "harness did not accept the task")
                continue
            self.task_of[aid] = task_id
            self.agent_of[task_id] = aid
            self.swarm.log(aid, f"[run] task {task_id}")
            started.append(agent.name)
        return {"started": started, "deferred": plan.deferred,
                "in_flight": self._in_flight()}

    def upstream_of(self, agent) -> list[tuple[str, str]]:
        """(name, output) for each dependency, in the order declared."""
        out = []
        for dep in agent.dependencies:
            other = self.swarm.agent_by_name(dep)
            if other is not None:
                out.append((other.name, other.output))
        return out

    # -- completion --------------------------------------------------------
    def on_task_state(self, task_id: str, state: str, output: str = "",
                      error: str = "") -> dict | None:
        """A task we own reached a new state. Advance its agent if terminal.

        Returns the pump result when the DAG moved, so a caller driving this
        from the bus does not have to know whether to tick.
        """
        aid = self.agent_of.get(task_id)
        if aid is None:
            return None                     # not ours; the cockpit has others
        if state in ("running", "pending"):
            return None
        if state == "done":
            self.finish(aid, output)
        elif state == "failed":
            self.fail(aid, error or "task failed")
        elif state in ("cancelled", "interrupted"):
            # Not a failure of the agent's own making, but downstream work
            # still has no input, so it must not be treated as satisfied.
            self.cancel(aid, f"task {state}")
        else:
            return None
        return self.pump()

    def finish(self, agent_id: str, output: str = "") -> None:
        from .swarm import AgentStatus
        agent = self.swarm.agents.get(agent_id)
        if agent is None:
            return
        agent.output = output or agent.output
        agent.progress = 1.0
        self.swarm.set_status(agent_id, AgentStatus.DONE)
        self._forget(agent_id)

    def fail(self, agent_id: str, error: str) -> None:
        from .swarm import AgentStatus
        agent = self.swarm.agents.get(agent_id)
        if agent is None:
            return
        agent.error = error
        self.swarm.log(agent_id, f"[run] failed: {error[:120]}")
        self.swarm.set_status(agent_id, AgentStatus.FAILED)
        self._forget(agent_id)

    def cancel(self, agent_id: str, why: str = "") -> None:
        from .swarm import AgentStatus
        if agent_id not in self.swarm.agents:
            return
        if why:
            self.swarm.log(agent_id, f"[run] {why}")
        self.swarm.set_status(agent_id, AgentStatus.CANCELLED)
        self._forget(agent_id)

    def _forget(self, agent_id: str) -> None:
        task_id = self.task_of.pop(agent_id, None)
        if task_id:
            self.agent_of.pop(task_id, None)

    # -- introspection -----------------------------------------------------
    def status(self) -> dict:
        """What the HUD needs to explain a swarm that is not moving."""
        summary = self.swarm.status_summary()
        stalled = self.stalled()
        return {
            **summary,
            "in_flight": self._in_flight(),
            "max_parallel": self.max_parallel,
            "vram_used": self._vram_used(),
            "vram_total": self.vram_total,
            "deferred": list(self.last.deferred),
            "stalled": stalled,
            "running_tasks": dict(self.task_of),
        }

    def stalled(self) -> str:
        """Why is nothing happening? Empty string when something still can.

        A DAG that quietly stops is the failure mode this whole module exists
        to prevent, so the reason is computed rather than left to be inferred
        from six panels.
        """
        from .swarm import AgentStatus

        if self._in_flight():
            return ""
        if not self.swarm.agents:
            return ""
        idle = [a for a in self.swarm.agents.values()
                if a.status is AgentStatus.IDLE]
        if not idle:
            return ""
        if self.swarm.agents_ready():
            return ""                       # a pump would start something
        blocked = self.swarm.blocked_agents()
        if blocked:
            why = "; ".join(f"{a.name}: {self.swarm.dep_state(a)[1]}"
                            for a in blocked[:3])
            return f"nothing running and nothing can start — {why}"
        # Idle, not ready, not blocked: every remaining agent is waiting on a
        # dependency that is itself idle. That is a cycle, and it is the one
        # thing add_checked cannot catch at insert time.
        return ("nothing running and no agent is ready — the remaining "
                "dependencies form a cycle")
