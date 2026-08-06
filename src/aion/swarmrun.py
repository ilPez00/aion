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

from .swarmfacts import instruction as facts_instruction
from .swarmfacts import note as facts_note
from .swarmfacts import parse as parse_facts
from .swarmio import artifact_note, blocking_writes, conflicts
from .swarmpolicy import backoff, classify, should_retry

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
    # Estimated, not billed — see swarmbudget. Zero for an unpriced harness,
    # which is most of them.
    cost: float = 0.0


@dataclass
class Admission:
    admit: list[str] = field(default_factory=list)
    deferred: list[dict] = field(default_factory=list)   # {id, name, reason}

    def as_dict(self) -> dict:
        return {"admit": list(self.admit), "deferred": list(self.deferred)}


def admit(slots: list[Slot], *, running: int = 0,
          max_parallel: int = DEFAULT_MAX_PARALLEL,
          vram_total: int = DEFAULT_VRAM_MB,
          vram_used: int = 0,
          budget: float = 0.0, committed: float = 0.0) -> Admission:
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
      * Money is the same shape as VRAM: a running total against a ceiling,
        `budget = 0` meaning no ceiling. It is checked last because it is the
        only limit that is an ESTIMATE, and a step should be refused for a
        thing we know before a thing we guessed.

    Order is the caller's order, so a tick is reproducible.
    """
    from .swarmbudget import affordable

    out = Admission()
    if max_parallel < 1:
        max_parallel = 1
    free_slots = max_parallel - max(0, running)
    used = max(0, vram_used)
    spent = max(0.0, committed)

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
        ok, why = affordable(s.cost, spent, budget)
        if not ok:
            out.deferred.append({"id": s.id, "name": s.name, "reason": why})
            continue
        out.admit.append(s.id)
        free_slots -= 1
        used += need
        spent += s.cost
    return out


# ── prompts ──────────────────────────────────────────────────────────────────
# A dependency that only means "wait for" wastes most of what a DAG is for.
# The upstream output is the reason `writer` waits for `scout`, so it goes into
# the prompt. Truncated per-dependency rather than in total, so one chatty
# upstream cannot crowd every other one out of the context.
UPSTREAM_BUDGET_CHARS = 2000


def prompt_for(goal: str, upstream: list[tuple[str, str]],
               budget: int = UPSTREAM_BUDGET_CHARS,
               artifacts: str = "") -> str:
    """The prompt an agent is actually given. Pure.

    `upstream` is [(name, output)] in dependency order. Dependencies that
    produced nothing are named anyway: "scout finished and produced no output"
    is information, and hiding it makes a downstream agent hallucinate an
    input it never received.
    """
    goal = (goal or "").strip()
    # Where the work LANDED, not only what its stdout said. Without this an
    # agent told to "polish the draft" has to guess the filename, and its
    # usual guess is to write a second draft beside the first.
    tail = f"\n\n{artifacts}" if artifacts else ""
    if not upstream:
        return f"{goal}{tail}" if tail else goal
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
            f"---\nContext from the steps this depends on:\n\n{joined}{tail}")


# ── watching work on another machine ─────────────────────────────────────────
# A local task announces itself on the bus. A remote one does not, so it has to
# be asked — and asking can fail for reasons that have nothing to do with the
# work: a laptop sleeps, a tunnel drops, wifi blinks. Treating one silent poll
# as a failure would cancel real work and unblock downstream agents on nothing.
MAX_MISSES = 4          # ~4 polls of grace before a peer counts as lost

# Returned by a spawn_remote/poll_remote that went away to do network I/O and
# will answer later via `attach()` / `deliver()`.
#
# The cockpit calls pump() and poll() from inside its own event loop, so those
# hooks cannot block: awaiting a 10s request there freezes the entire UI, and
# asyncio.run() from a running loop simply raises -- which is how the first
# cross-machine run failed, with every remote step marked FAILED before a
# packet was sent. Tests still hand back a value directly; this sentinel is
# what lets both work through one code path.
PENDING = object()


@dataclass
class Watch:
    """One agent's task running on another instance."""
    agent_id: str
    instance: str
    task_id: str
    misses: int = 0
    # Last thing the peer said about the task. A local task can be asked about
    # directly; a remote one cannot, so pausing one has to be judged against
    # the most recent answer rather than against nothing.
    state: str = ""
    paused: bool = False


def read_poll(reply, watch: Watch, max_misses: int = MAX_MISSES) -> tuple[str, str]:
    """(verdict, detail) from one poll of a remote task. Pure.

    Verdicts: "" (nothing to do), a task state to apply, or "lost".

    `reply` is whatever the peer sent — None when it could not be reached, a
    dict otherwise. The distinction between "cannot ask" and "asked, and the
    task is gone" matters: the first is the network, the second is real.
    """
    if reply is None:
        watch.misses += 1
        if watch.misses >= max_misses:
            return "lost", (f"{watch.instance} stopped answering after "
                            f"{watch.misses} attempts")
        return "", ""
    if not isinstance(reply, dict):
        watch.misses += 1
        return "", ""

    watch.misses = 0
    state = str(reply.get("state", "")).strip()
    watch.state = state
    watch.paused = bool(reply.get("paused"))
    if reply.get("error") or not state:
        # The peer answered and does not know this task. It restarted, or the
        # task was pruned. Either way the work is not coming back.
        return "lost", f"{watch.instance} has no task {watch.task_id}"
    return state, str(reply.get("output", "") or "")


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
                 harness_vram=None, spawn_remote=None, poll_remote=None,
                 budget: float = 0.0, prices=None, retry=None,
                 replan=None, events=None, heartbeat=None, clock=None) -> None:
        import time

        from .swarmbudget import Ledger
        from .swarmlive import HeartbeatPolicy
        from .swarmpolicy import RetryPolicy
        from .swarmreplan import ReplanPolicy

        self.swarm = orchestrator
        self.spawn = spawn                 # (agent, prompt) -> task_id | None
        self.harness = harness
        self.max_parallel = max_parallel
        self.vram_total = vram_total
        self._harness_vram = harness_vram or (lambda hid: 0)
        # Money. 0 = no ceiling, exactly like vram_total. The ledger is always
        # present so `status()` has one shape whether or not a budget is set.
        self.budget = float(budget or 0.0)
        self.ledger = Ledger(prices=dict(prices or {}))
        self._prompts: dict[str, str] = {}   # agent id -> what it was asked
        # What happens when a step fails. The default retries nothing, so a
        # swarm behaves exactly as it did before anyone configures one.
        self.retry = retry or RetryPolicy()
        # Whether a finished step may propose more work. Default is off, so a
        # DAG stays exactly as static as the one a person wrote.
        self.replan = replan or ReplanPolicy()
        # Steps that finished and are owed a proposal. The runner does not ask
        # the model itself: that is a blocking call, and this class is driven
        # from the event loop. The cockpit drains this, asks off-thread, and
        # hands the answer back to `apply_expansion`.
        self._replan_queue: list[str] = []
        # When a WORKING step stops counting as alive. Off by default: this is
        # the only policy here that ENDS work rather than declining to start
        # it, so it does nothing at all until someone asks for it.
        self.heartbeat = heartbeat or HeartbeatPolicy()
        # Where transitions go. Injected and no-op by default: the runner is a
        # scheduler, and a scheduler that cannot run without a writable disk is
        # a worse scheduler. `events(kind, step, **fields)`.
        self.events = events or (lambda kind, step="", **fields: None)
        # If the sink is an EventLog, keep the handle: `timeline()` reads the
        # file back rather than accumulating a second copy in memory, which
        # would be the snapshot problem again with extra steps.
        self.event_log = getattr(events, "__self__", None)
        # Injected so a backoff can be tested without sleeping through it.
        self._now = clock or time.time
        # Steps holding a slot open until their backoff expires. Rebuilt every
        # tick and folded into the admission report, because a step that is
        # merely WAITING must not look like a step nobody considered.
        self._backing_off: list[dict] = []
        # Optional, so a swarm works exactly as before with no peers set up.
        #   spawn_remote(instance, agent, prompt) -> task_id | ""
        #   poll_remote(instance, task_id)        -> dict | None
        self.spawn_remote = spawn_remote
        self.poll_remote = poll_remote
        self.watches: dict[str, Watch] = {}
        # agent id <-> task id, both ways: one to advance the agent when its
        # task ends, the other to find the task when the agent is cancelled.
        self.task_of: dict[str, str] = {}
        self.agent_of: dict[str, str] = {}
        self.last: Admission = Admission()

    # -- scheduling --------------------------------------------------------
    def _slots(self) -> list[Slot]:
        from .swarm import AgentStatus
        out = []
        claimed: list = []          # admitted this tick, for write conflicts
        self._backing_off = []
        now = self._now()
        for a in self.swarm.agents.values():
            if a.status is not AgentStatus.IDLE:
                continue
            if self.swarm.dep_state(a)[0] != "ready":
                continue
            due = float(getattr(a, "retry_at", 0.0) or 0.0)
            if due > now:
                # Ready, affordable, and deliberately not started yet. Reported
                # rather than skipped: from outside, a step held back by a
                # backoff is indistinguishable from one the scheduler forgot.
                self._backing_off.append({
                    "id": a.id, "name": a.name,
                    "reason": (f"retrying in {due - now:.0f}s "
                               f"(attempt {a.attempts + 1})")})
                continue
            # Two steps writing one file is not a merge conflict — there is no
            # merge, no branch and no lock, just one of them silently losing.
            # Holding this one until the other finishes turns a race into the
            # queue a human would have made by hand.
            # Both halves of the same question: something already running,
            # and something admitted earlier in THIS tick. Checking only the
            # first lets two writers start together on the very first pump,
            # which is exactly the tick where a fresh DAG races.
            busy = blocking_writes(a, [x for x in self.swarm.agents.values()
                                       if x.status is AgentStatus.WORKING]
                                      + claimed)
            if busy:
                self._backing_off.append({
                    "id": a.id, "name": a.name,
                    "reason": (f"{busy[0]['step']} is writing "
                               f"{busy[0]['paths'][0]}")})
                continue
            hid = getattr(a, "harness", "") or self.harness
            # The prompt is built here as well as in pump(): admission has to
            # price the real thing, and the upstream context it carries is
            # most of its length.
            prompt = self._prompt_for_agent(a)
            self._prompts[a.id] = prompt
            claimed.append(a)
            out.append(Slot(id=a.id, name=a.name, harness=hid,
                            vram_mb=self._harness_vram(hid),
                            cost=self.ledger.estimate(hid, prompt)))
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

        # Before admitting anything: a wedged step is still holding a slot,
        # VRAM and a place in `_in_flight()`, so a swarm can be entirely full
        # of work that stopped existing. Reaping first is what turns those
        # back into capacity on this tick rather than the next one.
        self.reap()

        plan = admit(self._slots(), running=self._in_flight(),
                     max_parallel=self.max_parallel,
                     vram_total=self.vram_total, vram_used=self._vram_used(),
                     budget=self.budget, committed=self.ledger.committed())
        plan.deferred.extend(self._backing_off)
        self.last = plan
        started = []
        for aid in plan.admit:
            agent = self.swarm.agents.get(aid)
            if agent is None:
                continue
            prompt = self._prompts.get(aid) or self._prompt_for_agent(agent)
            # Hold the estimate BEFORE the step starts. Without a hold, every
            # agent admitted in one tick sees the same "committed so far" and
            # the budget is blown once per step in a single pump.
            self.ledger.reserve(aid, getattr(agent, "harness", "") or self.harness,
                                prompt)
            # Counted here rather than on a successful spawn: a spawn that
            # throws is still an attempt, and a step whose attempts never
            # increment is a step that retries forever.
            agent.attempts = int(getattr(agent, "attempts", 0) or 0) + 1
            agent.retry_at = 0.0
            # WORKING before spawning: a synchronous spawn that completes
            # immediately would otherwise find the agent still IDLE and the
            # completion would be dropped.
            self.swarm.set_status(aid, AgentStatus.WORKING)
            # Stamped from the RUNNER's clock, not the orchestrator's. Liveness
            # compares `started` against `self._now()`, and two clocks in one
            # subtraction is a number that means nothing — which is what an
            # injected clock produced before this line existed. Re-stamped per
            # attempt on purpose: "has this attempt wedged" is the question,
            # and the event log already measures the whole story across tries.
            agent.started = self._now()
            # A pulse from the attempt that just died is not a pulse from this
            # one. Clearing it is what makes a retried step start out as "never
            # heard from" rather than inheriting the old run's liveness.
            agent.last_seen = 0.0
            self.events("started", agent.name, attempt=agent.attempts,
                        harness=getattr(agent, "harness", "") or self.harness,
                        instance=getattr(agent, "instance", "") or "")
            where = getattr(agent, "instance", "") or ""
            try:
                if where and self.spawn_remote is not None:
                    task_id = self.spawn_remote(where, agent, prompt)
                    if task_id is PENDING:
                        # In flight. The agent stays WORKING with no watch
                        # until attach() lands; poll() skips what it cannot
                        # see, so the gap is harmless.
                        started.append(agent.name)
                        continue
                elif where:
                    self.fail(aid, f"no way to reach {where}")
                    continue
                else:
                    task_id = self.spawn(agent, prompt)
            except Exception as e:  # noqa: BLE001
                self.fail(aid, f"could not start: {type(e).__name__}: {str(e)[:120]}")
                continue
            if not task_id:
                self.fail(aid, "harness did not accept the task")
                continue
            self._own(aid, task_id, where)
            self.swarm.log(aid, f"[run] task {task_id}"
                                + (f" on {where}" if where else ""))
            started.append(agent.name)
        return {"started": started, "deferred": plan.deferred,
                "in_flight": self._in_flight()}

    def _upstream_agents(self, agent) -> list:
        """The dependency agents themselves, for anything needing more than
        their text (what they wrote, where it landed)."""
        out = []
        for dep in agent.dependencies:
            other = self.swarm.agent_by_name(dep)
            if other is not None:
                out.append(other)
        return out

    def _has_dependents(self, agent) -> bool:
        """Is anything waiting on this step's result?

        Decides whether the step is asked to state facts. A leaf step's values
        are read by nobody, and a prompt that asks for them anyway spends room
        on ceremony — which is how an operator learns to skim the block.
        """
        return any(agent.name in (other.dependencies or [])
                   for other in self.swarm.agents.values())

    def _prompt_for_agent(self, agent) -> str:
        """The full prompt for a step: goal, upstream prose, where the work
        landed, the values it stated, and how to state its own.

        One function because admission has to PRICE the same string that gets
        sent — two builders drift, and the drift shows up as a budget that
        governs a prompt nobody sent.
        """
        upstream = self._upstream_agents(agent)
        extras = [artifact_note(upstream), facts_note(upstream)]
        if self._has_dependents(agent):
            extras.append(facts_instruction())
        return prompt_for(agent.goal, self.upstream_of(agent),
                          artifacts="\n\n".join(x for x in extras if x))

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
                      error: str = "", progress: float | None = None,
                      instance: str = "") -> dict | None:
        """A task we own reached a new state. Advance its agent if terminal.

        Returns the pump result when the DAG moved, so a caller driving this
        from the bus does not have to know whether to tick.

        `instance` namespaces the lookup. Task ids are allocated per registry
        (`t0001`, `t0002`, ...), so two machines running one DAG hand back the
        SAME id for different work — see `_key`.
        """
        aid = self.agent_of.get(self._key(instance, task_id))
        if aid is None:
            return None                     # not ours; the cockpit has others
        return self._advance(aid, state, output, error, progress)

    def _advance(self, aid: str, state: str, output: str = "",
                 error: str = "", progress: float | None = None) -> dict | None:
        """Move one KNOWN agent to a new task state.

        Split from `on_task_state` because a remote poll already knows which
        agent it is asking about — the watch carries it — and must not
        re-derive it from a task id that is only unique on one machine.
        """
        if state in ("running", "pending"):
            # Not nothing. This used to return immediately, so a swarm knew a
            # step's progress was 0.0 right up to the moment it was 1.0, and
            # had no way at all to tell a working step from a wedged one.
            self._heard(aid, progress)
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

    def live_lines(self) -> list[str]:
        """One line per running step: what it is, and how long it has been it.

        Plain text, composed once. Both surfaces print these verbatim for the
        same reason they share `explain()` — a cockpit whose two views phrase
        the same swarm differently is a cockpit that gets argued with.
        """
        from .swarm import AgentStatus
        from .swarmlive import assess, render_live

        out = []
        for a in self.swarm.agents.values():
            if a.status is not AgentStatus.WORKING:
                continue
            shown = render_live(assess(a, self._now(), self.heartbeat))
            out.append(f"{a.name} — {shown}" if shown else f"{a.name} — running")
        return out

    def reap(self) -> list[str]:
        """End the running steps the heartbeat policy no longer believes in.

        Routed through `fail()` rather than `cancel()` on purpose. A wedged
        step is a FAILURE — an unclassifiable one, which the retry policy
        treats as transient and runs again, and running a hung step again is
        very often the correct move. `cancel()` would mark it as a decision
        somebody made, and nobody made it. `fail()` also settles the ledger
        against the task the step really did run, where `cancel()` releases
        the hold: a reaped step burned whatever it burned before it wedged.

        `fail()` drops the task watch, so a harness that wakes up later finds
        no agent behind its id — a late completion cannot land on the retry.

        Does nothing at all unless a policy was configured.
        """
        from .swarm import AgentStatus
        from .swarmlive import sweep

        running = [a for a in self.swarm.agents.values()
                   if a.status is AgentStatus.WORKING]
        reaped = []
        for found in sweep(running, self._now(), self.heartbeat):
            agent = self.swarm.agents.get(found["id"])
            if agent is None:
                continue
            self.swarm.log(found["id"], f"[run] reaped: {found['why']}")
            self.events("reaped", agent.name, state=found["state"],
                        reason=found["why"])
            self.fail(found["id"], found["why"])
            reaped.append(agent.name)
        return reaped

    def _heard(self, agent_id: str, progress: float | None = None) -> None:
        """Something was heard from a running step. Stamp it.

        The stamp is what separates "never reported" from "reported and then
        stopped" — the only one of those two silences that says anything.
        """
        agent = self.swarm.agents.get(agent_id)
        if agent is None:
            return
        agent.last_seen = self._now()
        if progress is not None:
            try:
                agent.progress = max(0.0, min(1.0, float(progress)))
            except (TypeError, ValueError):
                pass

    def finish(self, agent_id: str, output: str = "") -> None:
        from .swarm import AgentStatus
        agent = self.swarm.agents.get(agent_id)
        if agent is None:
            return
        agent.output = output or agent.output
        # Pulled out HERE rather than when a downstream prompt is built: the
        # values a step stated are part of its result, and a result that has to
        # be re-derived on every read is a result nothing can be sure of.
        agent.facts = parse_facts(agent.output)
        agent.progress = 1.0
        # The hold becomes the real figure: this is the only moment both
        # halves of the exchange are known.
        self.ledger.settle(agent_id, self._prompts.get(agent_id, ""),
                           agent.output)
        self.swarm.set_status(agent_id, AgentStatus.DONE)
        self.events("finished", agent.name, chars=len(agent.output or ""))
        self._forget(agent_id)
        # A result is the only moment there is anything new to plan WITH, so
        # this is where a DAG stops being a fixed shape. Queued, not asked:
        # the proposal is a model call and this runs on the event loop.
        if (self.replan.enabled and agent.output
                and agent.generation < self.replan.max_generations):
            self._replan_queue.append(agent_id)

    def fail(self, agent_id: str, error: str) -> None:
        from .swarm import AgentStatus
        agent = self.swarm.agents.get(agent_id)
        if agent is None:
            return
        agent.error = error
        # A step that RAN and failed still consumed whatever it consumed
        # before failing, so it settles rather than releasing — otherwise a
        # DAG that fails repeatedly spends without limit. A step that never
        # reached a harness owns no task id and sent no tokens, so it does
        # release: charging for a request that was never made is just wrong.
        if self.task_of.get(agent_id):
            self.ledger.settle(agent_id, self._prompts.get(agent_id, ""),
                               agent.output)
        else:
            self.ledger.release(agent_id)
        self.swarm.log(agent_id, f"[run] failed: {error[:120]}")
        self._forget(agent_id)

        again, why = should_retry(error, int(getattr(agent, "attempts", 0) or 0),
                                  self.retry)
        if again:
            # Back to IDLE, not FAILED: FAILED blocks every dependent through
            # `dep_state`, and a step we intend to run again in thirty seconds
            # has not blocked anything yet. The backoff is what stops the next
            # pump from picking it straight back up.
            agent.retry_at = self._now() + backoff(agent.attempts, self.retry)
            agent.progress = 0.0
            self.swarm.log(agent_id, f"[run] {why}")
            self.events("retry", agent.name, attempt=agent.attempts,
                        wait=round(agent.retry_at - self._now(), 1),
                        error=error[:200])
            self.swarm.set_status(agent_id, AgentStatus.IDLE)
            return
        if self.retry.enabled:
            # The reason retrying STOPPED is the interesting half. Without it a
            # dead-lettered step and a step that never had a policy look
            # identical in the log.
            self.swarm.log(agent_id, f"[run] giving up: {why}")
        # `gave_up` when a policy stopped retrying, `failed` when there was no
        # policy to stop: a dead-lettered step and one that never had a retry
        # budget are different stories about the same red mark.
        self.events("gave_up" if self.retry.enabled else "failed", agent.name,
                    attempts=int(getattr(agent, "attempts", 0) or 0),
                    error=error[:200], reason=why)
        self.swarm.set_status(agent_id, AgentStatus.FAILED)

    def cancel(self, agent_id: str, why: str = "") -> None:
        from .swarm import AgentStatus
        if agent_id not in self.swarm.agents:
            return
        # Released: cancelled work owes nothing, and holding its reservation
        # would shrink the budget left for the rest of the DAG for no reason.
        self.ledger.release(agent_id)
        if why:
            self.swarm.log(agent_id, f"[run] {why}")
        self.swarm.set_status(agent_id, AgentStatus.CANCELLED)
        self.events("cancelled", self.swarm.agents[agent_id].name, reason=why)
        self._forget(agent_id)

    @staticmethod
    def _key(instance: str, task_id: str) -> str:
        """Task ids are unique per REGISTRY, not per fleet.

        Every instance allocates `t0001`, `t0002`, ... from zero, so a DAG with
        a step on pansa and a step on air gets `t0003` back from both. Keyed by
        the bare id, the second registration silently overwrote the first: one
        step was finished twice and the other waited forever, taking every
        dependent with it. Found the first time a real DAG ran on two machines.
        """
        return f"{instance}\x00{task_id}" if instance else task_id

    def _own(self, agent_id: str, task_id: str, instance: str = "") -> None:
        """Record that this agent's work is this task. One place, because the
        mapping now lives in three: two dicts and the checkpointed agent."""
        self.task_of[agent_id] = task_id
        self.agent_of[self._key(instance, task_id)] = agent_id
        agent = self.swarm.agents.get(agent_id)
        if agent is not None:
            agent.task_id = task_id
        if instance:
            self.watches[agent_id] = Watch(agent_id, instance, task_id)

    def _forget(self, agent_id: str) -> None:
        task_id = self.task_of.pop(agent_id, None)
        watch = self.watches.pop(agent_id, None)
        if task_id:
            # Namespaced, or forgetting a remote step deletes the mapping of
            # whichever OTHER machine happens to share that task id.
            self.agent_of.pop(
                self._key(watch.instance if watch else "", task_id), None)
        agent = self.swarm.agents.get(agent_id)
        if agent is not None:
            agent.task_id = ""

    def rehydrate(self) -> dict:
        """Re-adopt work that outlived the process. Call once after a restore.

        Only remote steps can survive: a local task died with the harness
        coroutine, and `SwarmAgent.from_record` has already put those back to
        IDLE. What is left WORKING is running on a peer that never noticed we
        went away, so the watch is rebuilt and the next `poll()` collects the
        result exactly as if we had never stopped.

        Without this the DAG restarts a job that is already running -- double
        spend, and every side effect that step has, twice.
        """
        from .swarm import AgentStatus

        adopted = []
        for agent in self.swarm.agents.values():
            task_id = getattr(agent, "task_id", "") or ""
            instance = getattr(agent, "instance", "") or ""
            if agent.status is not AgentStatus.WORKING or not task_id:
                continue
            if not instance:
                # Belt and braces: a local task cannot have survived, so if one
                # is somehow still marked WORKING it is stale, not running.
                self.cancel(agent.id, "the cockpit restarted while it ran")
                continue
            self._own(agent.id, task_id, instance)
            self.swarm.log(agent.id, f"[run] re-attached to {task_id} on {instance}")
            adopted.append(agent.name)
        return {"adopted": adopted}

    # -- remote --------------------------------------------------------------
    def attach(self, agent_id: str, instance: str, task_id: str) -> None:
        """A remote spawn came back with a task id. Start watching it."""
        from .swarm import AgentStatus
        agent = self.swarm.agents.get(agent_id)
        if agent is None or agent.status is not AgentStatus.WORKING:
            return                       # cancelled while the request was out
        if not task_id:
            self.fail(agent_id, f"{instance} did not accept the task")
            return
        self._own(agent_id, task_id, instance)
        self.swarm.log(agent_id, f"[run] task {task_id} on {instance}")

    def deliver(self, task_id: str, reply) -> None:
        """An async poll answered. Same handling as a synchronous one."""
        agent_id = self.agent_of.get(task_id)
        w = self.watches.get(agent_id) if agent_id else None
        if w is None:
            return
        self._apply_poll(w, reply)

    def _apply_poll(self, watch: Watch, reply) -> bool:
        verdict, detail = read_poll(reply, watch)
        if not verdict:
            return False
        if verdict == "lost":
            self.fail(watch.agent_id, detail)
        # `_advance`, not `on_task_state`: the watch already knows which agent
        # this is, and looking it up by task id resolved to the wrong step the
        # moment two machines both returned `t0003`.
        elif self._advance(watch.agent_id, verdict, output=detail) is None:
            return False
        self.pump()
        return True

    def poll(self) -> dict:
        """Ask each peer how our work is going. Call it on a timer.

        Remote tasks cannot announce themselves on this process's bus, so this
        is the only way a cross-machine DAG advances. Latency is the poll
        interval, which is why it is a separate call rather than something
        `pump()` does: the caller owns the cadence.
        """
        if self.poll_remote is None or not self.watches:
            return {"polled": 0, "advanced": []}
        advanced = []
        for watch in list(self.watches.values()):
            try:
                reply = self.poll_remote(watch.instance, watch.task_id)
            except Exception:  # noqa: BLE001
                reply = None
            if reply is PENDING:
                continue             # deliver() will handle it
            if self._apply_poll(watch, reply):
                advanced.append(watch.agent_id)
        return {"polled": len(self.watches), "advanced": advanced}

    # -- introspection -----------------------------------------------------
    def task_ref(self, agent_id: str) -> dict:
        """Where one agent's work is: task id, instance, last known state.

        The store needs all four to decide a pause: a local task it can look up
        in the registry, a remote one it can only know as of the last poll. An
        empty `task_id` means the agent owns no task — either it has not
        started, or a remote spawn is still in flight.
        """
        w = self.watches.get(agent_id)
        return {"task_id": self.task_of.get(agent_id, ""),
                "instance": w.instance if w else "",
                "state": w.state if w else "",
                "paused": bool(w.paused) if w else False}

    def due_for_retry(self) -> bool:
        """Is a step's backoff up? True means somebody should call `pump()`.

        A retry is the one thing that becomes startable with NO event to
        announce it: a task state change drives every other advance, and a step
        waiting on the clock produces none. Without a caller asking this on a
        timer, a backed-off DAG sits IDLE forever — which is a worse failure
        than the one retrying was meant to fix, because at least FAILED says so.
        """
        from .swarm import AgentStatus

        now = self._now()
        for a in self.swarm.agents.values():
            if a.status is not AgentStatus.IDLE:
                continue
            if not float(getattr(a, "retry_at", 0.0) or 0.0):
                continue
            if a.retry_at <= now and self.swarm.dep_state(a)[0] == "ready":
                return True
        return False

    # -- replanning --------------------------------------------------------
    def take_replans(self) -> list[dict]:
        """Finished steps owed a proposal. Drains: each is offered once.

        Deliberately NOT checkpointed. A cockpit that dies between a step
        finishing and its proposal arriving comes back with the DAG the human
        approved, which is the right side to fail on — the alternative is a
        restart that resumes growing a swarm nobody is watching.
        """
        out = []
        for aid in self._replan_queue:
            a = self.swarm.agents.get(aid)
            if a is None:
                continue
            out.append({"id": a.id, "name": a.name, "goal": a.goal,
                        "output": a.output, "generation": a.generation})
        self._replan_queue = []
        return out

    def apply_expansion(self, agent_id: str, raw_steps) -> dict:
        """Validate a proposal against the live swarm and create what survives.

        Everything refused is logged on the parent, not swallowed: a swarm that
        silently declines to grow looks identical to one whose planner said
        nothing, and those want very different responses from a human.
        """
        from .swarmreplan import validate

        agent = self.swarm.agents.get(agent_id)
        if agent is None:
            return {"ok": False, "created": [], "reason": "no such agent"}
        existing = {a.name: a.status.value for a in self.swarm.agents.values()}
        exp = validate(raw_steps, parent=agent.name,
                       parent_generation=int(getattr(agent, "generation", 0) or 0),
                       existing=existing, policy=self.replan)
        for why in exp.dropped:
            self.swarm.log(agent_id, f"[replan] dropped {why}")
        if exp.problems:
            self.swarm.log(agent_id, f"[replan] refused: {'; '.join(exp.problems)}")
            return {"ok": False, "created": [], "reason": "; ".join(exp.problems),
                    "dropped": exp.dropped}
        created = []
        for step in exp.steps:
            out = self.swarm.add_checked(step.name, step.goal, step.deps,
                                         harness=step.harness,
                                         writes=step.writes)
            if not out.get("ok"):
                self.swarm.log(agent_id,
                               f"[replan] dropped {step.name}: {out.get('reason', '')}")
                continue
            new = self.swarm.agent_by_name(step.name)
            if new is not None:
                # Provenance is the whole defence against a DAG nobody
                # recognises: generation bounds the recursion, parent_id says
                # which result asked for this.
                new.generation = int(getattr(agent, "generation", 0) or 0) + 1
                new.parent_id = agent.id
                created.append(step.name)
        if created:
            self.swarm.log(agent_id, f"[replan] added {', '.join(created)}")
            self.events("expanded", agent.name, count=len(created),
                        added=created)
            self.swarm.checkpoint()
        return {"ok": bool(created), "created": created,
                "dropped": exp.dropped, "reason": ""}

    def timeline(self, limit: int = 200) -> list[dict]:
        """One row per step from the event log: when, how long, how it ended.

        Empty when no log is attached — the runner never keeps a second copy in
        memory, because a history that lives in a process is the snapshot
        problem again with extra steps.
        """
        from .swarmlog import timeline as fold

        log = getattr(self, "event_log", None)
        if log is None:
            return []
        try:
            return fold(log.read(limit=limit))
        except Exception:  # noqa: BLE001
            return []      # a status call must not fail on an unreadable log

    def dead_letters(self) -> list[dict]:
        """Steps that ran out of attempts and now need a person.

        Derived from the agents rather than kept in a list of its own, so it
        survives a restart for free — the alternative is a second source of
        truth that a checkpoint quietly drops.
        """
        from .swarm import AgentStatus

        out = []
        for a in self.swarm.agents.values():
            if a.status is not AgentStatus.FAILED:
                continue
            out.append({"id": a.id, "name": a.name,
                        "attempts": int(getattr(a, "attempts", 0) or 0),
                        "error": (a.error or "")[:200],
                        "kind": classify(a.error or ""),
                        "blocks": sorted(o.name for o in self.swarm.agents.values()
                                         if a.name in o.dependencies)})
        return out

    def status(self) -> dict:
        """What the HUD needs to explain a swarm that is not moving."""
        summary = self.swarm.status_summary()
        stalled = self.stalled()
        return {
            **summary,
            # The steps themselves. `status_summary` counts them and only the
            # bus publish ever attached the list, so anything reading status()
            # alone — the browser, `swarmview.explain` — saw a swarm with no
            # agents in it and reported an empty DAG.
            "agents": [a.as_dict() for a in self.swarm.agents.values()],
            "in_flight": self._in_flight(),
            "max_parallel": self.max_parallel,
            "vram_used": self._vram_used(),
            "vram_total": self.vram_total,
            "budget": self.budget,
            "ledger": self.ledger.as_dict(),
            "max_attempts": self.retry.max_attempts,
            # Two steps writing one path with nothing ordering them is a race
            # whose outcome depends on the scheduler. Reported standing, not
            # only at admission: by the time one is admitted the other may
            # already have run, and the damage is silent either way.
            "write_conflicts": conflicts(list(self.swarm.agents.values())),
            # Composed here so the browser prints what the cockpit says rather
            # than deciding for itself when a step counts as quiet — two
            # renderers disagreeing about that is worse than either verdict.
            "live": self.live_lines(),
            "timeline": self.timeline(),
            "dead_letters": self.dead_letters(),
            "deferred": list(self.last.deferred),
            "remote": {w.agent_id: {"instance": w.instance, "task": w.task_id,
                                    "misses": w.misses, "state": w.state,
                                    "paused": w.paused}
                       for w in self.watches.values()},
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
        from .swarmlive import assess

        if self._in_flight():
            # "Something is running" was treated as proof that nothing is
            # wrong, which made the one failure that kills a swarm silently —
            # a step stuck in WORKING — the single condition this refused to
            # diagnose. Only reported for a step that WAS talking and stopped:
            # a harness that reports once at exit is silent and healthy, and
            # saying otherwise every time would train people to ignore it.
            quiet = []
            for a in self.swarm.agents.values():
                if a.status is not AgentStatus.WORKING:
                    continue
                live = assess(a, self._now(), self.heartbeat)
                if live.state in ("quiet", "stalled"):
                    quiet.append((live.silent_for, a.name))
            if quiet:
                silent_for, name = max(quiet)
                return (f"{name} has been silent for "
                        f"{silent_for / 60:.0f}m since last reporting")
            return ""
        if not self.swarm.agents:
            return ""
        idle = [a for a in self.swarm.agents.values()
                if a.status is AgentStatus.IDLE]
        if not idle:
            return ""
        if self.swarm.agents_ready():
            # Ready is not the same as due. A step inside its backoff is idle
            # on purpose, and saying so is the difference between "wait 30
            # seconds" and "something is broken".
            #
            # Read off the agents rather than off `last.deferred`: the plan was
            # built BEFORE the failure that started the backoff, so the tick
            # that most needs this answer is the one tick that would not have it.
            now = self._now()
            waiting = sorted((a.retry_at, a.name) for a in idle
                             if float(getattr(a, "retry_at", 0.0) or 0.0) > now)
            if waiting:
                due, name = waiting[0]
                return (f"nothing running — {name}: retrying in "
                        f"{due - now:.0f}s")
            # Ready is not the same as affordable. A DAG stopped by its budget
            # looks exactly like a healthy idle one from every other panel,
            # which is the most expensive kind of silence to debug.
            #
            # Read it off the last admission rather than comparing committed
            # against the budget: when the budget stops the FIRST step nothing
            # was ever reserved, so committed is zero and that comparison says
            # everything is fine.
            if self.budget:
                held = [d for d in self.last.deferred if "budget" in d["reason"]]
                if held:
                    return (f"nothing running — {held[0]['name']}: {held[0]['reason']}")
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
