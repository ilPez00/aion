"""storeswarm.py — the Store's swarm half.

`store.py` was 2093 lines, and the largest single thing in it was the swarm:
the lazy runner and its policies, the remote spawn/poll/control hooks, the
typed `swarm ...` verbs, the plan/apply pair, the replan tick, and the agent
tool that reads a DAG back. Around 560 lines answering one question — how does
a plan get run — sitting in the middle of a file that also handles todos, env
vars, chat, boards and the task registry.

This is a relocation, not a redesign. `SwarmCommands` is a mixin and every
method keeps the `self` it always had, so there is no call site to update and
nothing to get subtly wrong in a 2000-line reshuffle. What it buys is that the
swarm surface is readable in one sitting, and that a change to it stops
touching the file everything else also lives in.

It is honest about what it depends on rather than pretending to be decoupled.
From the Store it uses:

    self.swarm          the SwarmOrchestrator holding the DAG
    self.state          for `history` (typed-command output) and active harness
    self.registry       task lookup, for steps that own a task
    self.harnesses      VRAM and per-harness prices for the ledger
    self.cfg            the swarm_* policy keys, all absent-means-off
    self.control_task   pause/resume/cancel, applied to the step's task
    self._spawn_now     how a step actually becomes a running task

Read through `getattr` wherever a half-built Store might not have it yet: the
runner is lazy precisely because a Store gets constructed in pieces, and in
tests without a harness table at all.
"""

from __future__ import annotations

import asyncio


class SwarmCommands:
    """Swarm behaviour, mixed into `Store`. Not usable on its own."""

    @property
    def swarm_runner(self):
        """The thing that actually advances the DAG.

        Lazy because it needs the harness table for VRAM accounting, and a
        Store is constructed in tests without one.
        """
        if getattr(self, "_swarm_runner", None) is None:
            from .swarmrun import SwarmRunner

            # getattr throughout: this property is lazy precisely because a
            # Store gets built in pieces, and a runner that explodes on a
            # half-built one would be worse than one that runs with defaults.
            def vram(hid: str) -> int:
                h = getattr(self, "harnesses", {}).get(hid)
                return int(getattr(h, "vram_mb", 0) or 0) if h else 0

            def default_harness() -> str:
                return getattr(getattr(self, "state", None), "active_harness", "")

            from .swarmbudget import prices_from_harnesses
            from .swarmpolicy import policy_from_config
            from .swarmlive import policy_from_config as heartbeat_from_config
            from .swarmlog import EventLog
            from .swarmreplan import policy_from_config as replan_from_config

            # `swarm_budget` in config, currency per DAG. 0 = no ceiling.
            # Parallelism and VRAM say nothing about money: a swarm can sit
            # inside both limits and still spend all night, which is exactly
            # what leaving one running unattended means.
            try:
                budget = float(getattr(self, "cfg", {}).get("swarm_budget", 0) or 0)
            except (TypeError, ValueError):
                budget = 0.0

            self._swarm_runner = SwarmRunner(
                self.swarm,
                spawn_remote=self._swarm_spawn_remote,
                poll_remote=self._swarm_poll_remote,
                spawn=lambda agent, prompt: self._spawn_now(
                    getattr(agent, "harness", "") or default_harness(),
                    prompt, label=f"swarm/{agent.name}"),
                harness=default_harness(),
                harness_vram=vram,
                budget=budget,
                prices=prices_from_harnesses(getattr(self, "harnesses", {})),
                # `swarm_retry` in config: 3, or {"max_attempts": 3, ...}.
                # Absent means no automatic retry, so an upgrade never starts
                # re-running work — and spending on it — that nobody asked to
                # be re-run.
                retry=policy_from_config(getattr(self, "cfg", {})),
                # `swarm_replan` in config: 3, or the full dict. Absent means a
                # finished step proposes nothing, i.e. the static DAG everyone
                # already has — a swarm must not start writing its own work
                # because a version changed.
                replan=replan_from_config(getattr(self, "cfg", {})),
                # An append-only record of transitions, beside the snapshot.
                # The snapshot answers "what is"; nothing answered "what
                # happened", so a finished run could not say how long a step
                # took or how many tries it needed.
                events=EventLog().record,
                # `swarm_heartbeat` in config. Absent means a WORKING step is
                # never ended for going quiet — this is the only policy here
                # that stops work rather than declining to start it, so it
                # stays off until someone sets a bound on purpose.
                heartbeat=heartbeat_from_config(getattr(self, "cfg", {})),
            )
            # Adopt work that outlived the last process. Here rather than in
            # __init__ because the runner is lazy and this is the first moment
            # one exists; doing it twice is harmless (it is keyed on agent
            # state, not on a queue) but it only ever happens once.
            try:
                self._swarm_runner.rehydrate()
            except Exception as e:  # noqa: BLE001
                print(f"[swarm] rehydrate failed: {e}")
        return self._swarm_runner

    def _peer_node(self, instance: str):
        """Resolve an instance id to a RemoteNode. Discovery only -- an agent
        names an instance, never a machine, exactly as routing does."""
        from .fleet import discover_local
        from .remotes import RemoteNode
        for peer in discover_local(include_self=True):
            if peer.id == instance:
                return RemoteNode(id=peer.id, host=peer.host, port=peer.port)
        return None

    def _swarm_spawn_remote(self, instance: str, agent, prompt: str):
        """Ask another instance to run this step. Non-blocking.

        Returns PENDING and answers later through `runner.attach`. These hooks
        are called from inside the cockpit's event loop, so awaiting a network
        request here would freeze the UI -- and asyncio.run() from a running
        loop raises outright, which marked every remote step FAILED before a
        packet was ever sent.
        """
        from .swarmrun import PENDING
        node = self._peer_node(instance)
        if node is None:
            return ""                    # unknown instance: a real failure
        asyncio.create_task(self._remote_spawn_async(
            node, instance, agent.id, getattr(agent, "harness", ""), prompt))
        return PENDING

    async def _remote_spawn_async(self, node, instance, agent_id,
                                  harness, prompt) -> None:
        from .remotes import RemoteClient
        try:
            out = await RemoteClient(timeout=10.0).run_task(node, prompt, harness)
        except Exception:  # noqa: BLE001
            out = None
        self.swarm_runner.attach(agent_id, instance,
                                 str((out or {}).get("task_id", "") or ""))

    def _swarm_poll_remote(self, instance: str, task_id: str):
        """Non-blocking too; the answer arrives via `runner.deliver`."""
        from .swarmrun import PENDING
        node = self._peer_node(instance)
        if node is None:
            return None                  # unreachable: counts as a miss
        asyncio.create_task(self._remote_poll_async(node, task_id))
        return PENDING

    async def _remote_poll_async(self, node, task_id: str) -> None:
        from .remotes import RemoteClient
        try:
            reply = await RemoteClient(timeout=8.0).task_state(node, task_id)
        except Exception:  # noqa: BLE001
            reply = None
        self.swarm_runner.deliver(task_id, reply)

    def swarm_command(self, params: dict) -> dict:
        """One entry point for every swarm verb, used by the web HUD.

        Dispatch lives here rather than in the transport so the parameter
        shapes are checked once, in the process that owns the DAG. The
        orchestrator answers whether an action is legal and why not; this only
        decides which question to ask it.
        """
        from . import agentctl

        params = params if isinstance(params, dict) else {}
        action = str(params.get("action", "")).strip()
        agent_id = str(params.get("agent_id", "")).strip()

        if action == "add":
            deps = params.get("deps") or []
            if not isinstance(deps, list):
                return {"ok": False, "reason": "deps must be a list of names"}
            writes = params.get("writes") or []
            if not isinstance(writes, list):
                return {"ok": False, "reason": "writes must be a list of paths"}
            return self.swarm.add_checked(
                str(params.get("name", "")), str(params.get("goal", "")),
                [str(d) for d in deps], harness=str(params.get("harness", "")),
                instance=str(params.get("instance_for", "")),
                writes=[str(w) for w in writes])
        if action == "plan":
            # Propose a DAG from a goal. Creating it is a second, explicit
            # step, and running it is a third -- the same fail-closed shape as
            # routing, because every step becomes a prompt a harness executes.
            from . import swarmplan
            known = list(self.harnesses) if hasattr(self, "harnesses") else ()
            existing = [a.name for a in self.swarm.agents.values()]
            steps = params.get("steps")
            if isinstance(steps, list) and steps:
                # Apply EXACTLY what was reviewed. Re-running the planner here
                # would create a different DAG from the one somebody just read
                # and approved -- the model is not deterministic, and "review
                # then commit" means nothing if the commit re-rolls the dice.
                # Same validation either way: these arrive from a browser.
                plan = swarmplan.validate(str(params.get("goal", "")), steps,
                                          known_harnesses=known,
                                          existing_names=existing)
            else:
                plan = swarmplan.propose(
                    str(params.get("goal", "")),
                    harnesses=known, existing_names=existing)
            out = plan.as_dict()
            if params.get("apply") is True and plan.ok:
                out["applied"] = swarmplan.apply(self.swarm, plan)
            elif plan.ok:
                out["hint"] = "resend with apply to create these agents"
            return out
        if action == "run_ready":
            # Was: flip every ready agent to WORKING and hope. Now it spawns
            # real tasks, respects the parallel and VRAM budgets, and reports
            # what it held back and why.
            out = self.swarm_runner.pump()
            out["ok"] = bool(out["started"]) or not out["deferred"]
            out["reason"] = "" if out["started"] else (
                self.swarm_runner.stalled() or "nothing is ready")
            return out
        if action == "stop_all":
            out = self.swarm.stop_all()
            for aid, task_id in list(self.swarm_runner.task_of.items()):
                try:
                    self.control_task(task_id, "cancel")
                except Exception:  # noqa: BLE001
                    pass          # the agent is stopped; the task is a courtesy
                self.swarm_runner._forget(aid)
            return out
        if action == "status":
            st = self.swarm_runner.status()
            # The sentences, not just the numbers. The browser is a second
            # renderer of the same swarm, and two renderers each phrasing
            # "nothing is running" their own way is how a cockpit starts
            # contradicting itself — so the wording is decided once, here.
            from .swarmview import capacity, explain, spend
            st["why"] = explain(st.get("agents") or [])
            st["spend_text"] = spend(st)
            st["capacity_text"] = capacity(st)
            return st
        if action in agentctl.DELEGATED:
            return self._swarm_delegate(agent_id, action)
        if action in self.swarm.ACTIONS:
            if not agent_id:
                return {"ok": False, "action": action, "reason": "no agent"}
            # `start` goes through the runner so it gets a real task, the
            # upstream context and the budget checks -- not just a status flip.
            if action == "start":
                ok, why = self.swarm.can(agent_id, "start")
                if not ok:
                    return {"ok": False, "action": "start",
                            "agent_id": agent_id, "reason": why}
                out = self.swarm_runner.pump()
                started = agent_id in [a.id for a in self.swarm.agents.values()
                                       if a.id in self.swarm_runner.task_of]
                return {"ok": started, "action": "start", "agent_id": agent_id,
                        **out,
                        "reason": "" if started else "held back by the budget"}
            result = self.swarm.control(agent_id, action)
            # An agent stopped while its task runs must take the task with it,
            # or the work continues with nothing left watching for it.
            if result.get("ok") and action in ("cancel", "remove"):
                task_id = self.swarm_runner.task_of.get(agent_id)
                if task_id:
                    # Best effort, and the mapping is dropped either way: the
                    # agent is already stopped, and leaving it pointing at a
                    # task would let that task's completion resurrect it.
                    try:
                        self.control_task(task_id, "cancel")
                    except Exception as e:  # noqa: BLE001
                        self.swarm.log(agent_id,
                                       f"[run] could not cancel {task_id}: "
                                       f"{type(e).__name__}")
                    self.swarm_runner._forget(agent_id)
            return result
        return {"ok": False, "action": action,
                "reason": f"unknown swarm action {action!r}"}

    def _swarm_delegate(self, agent_id: str, action: str) -> dict:
        """pause/resume a swarm agent by acting on the task it owns.

        The whole point of the runner is that an agent's work IS a task, so
        these do not get an agent-level implementation — there is nothing at
        that level to suspend. They find the task and apply exactly the rules
        `control_task` already enforces, which is why a paused swarm step and a
        paused task typed into the cockpit behave identically.

        A step pinned to another instance is the interesting case: its task
        lives over there, so the request travels the same authenticated path
        the poll does, and the peer decides legality against the state it
        actually has. We answer from the last poll, which can be a few seconds
        stale — hence "asked", not "done".
        """
        from . import agentctl

        agent = self.swarm.agents.get(agent_id)
        if agent is None:
            return {"ok": False, "action": action, "agent_id": agent_id,
                    "reason": "no agent" if not agent_id else "no such agent"}

        ref = self.swarm_runner.task_ref(agent_id)
        task_id, instance = ref["task_id"], ref["instance"]
        if instance:
            state, paused = ref["state"], ref["paused"]
        else:
            task = self.registry.tasks.get(task_id) if task_id else None
            state = task.state.value if task is not None else ""
            paused = bool(getattr(task, "paused", False))

        def answer(ok: bool, reason: str = "", **extra) -> dict:
            out = agentctl.Outcome(ok, action, task_id, reason, state).as_dict()
            out["agent_id"] = agent_id       # the caller asked about an agent
            out.update(extra)
            return out

        where, why = agentctl.route(action, agent.status.value, state, paused)
        if where != "task":
            return answer(False, why)

        if not instance:
            out = self.control_task(task_id, action)
            out["agent_id"] = agent_id
            if out.get("ok"):
                self.swarm.log(agent_id, f"[control] {agentctl.describe(action)}")
            return out

        node = self._peer_node(instance)
        if node is None:
            return answer(False, f"{instance} is not in the fleet right now")
        try:
            asyncio.create_task(self._remote_control_async(
                node, instance, agent_id, task_id, action))
        except RuntimeError:
            # No loop: nothing can be sent, and claiming otherwise would leave
            # the caller believing a running task had been paused.
            return answer(False, f"cannot reach {instance} from here")
        return answer(True, f"asked {instance} to {action} it", pending=True)

    async def _remote_control_async(self, node, instance: str, agent_id: str,
                                    task_id: str, action: str) -> None:
        """Carry a pause/resume to the instance running the task."""
        from . import agentctl
        from .remotes import RemoteClient

        try:
            reply = await RemoteClient(timeout=8.0).control_task(
                node, task_id, action)
        except Exception as e:  # noqa: BLE001
            reply = {"reason": f"{type(e).__name__}: {str(e)[:80]}"}
        if isinstance(reply, dict) and reply.get("ok"):
            self.swarm.log(agent_id,
                           f"[control] {agentctl.describe(action)} on {instance}")
            return
        why = (reply or {}).get("reason", "") if isinstance(reply, dict) else ""
        self.swarm.log(agent_id, f"[control] {instance} refused {action}"
                                 f"{': ' + why[:80] if why else ''}")

    def _free_agent_name(self, stem: str = "Agent") -> str:
        """`stem`, `stem-2`, … — the first one not already taken.

        Names are the dependency key, so `add_checked` refuses a duplicate.
        Typing `swarm create` twice is a normal thing to do and must not be
        the thing that hits that wall.
        """
        n, name = 1, stem
        while self.swarm.agent_by_name(name) is not None:
            n += 1
            name = f"{stem}-{n}"
        return name

    async def swarm_replan_tick(self) -> list[dict]:
        """Ask the planner what each finished step made necessary, and apply it.

        Lives here rather than in the runner because it is the only part of
        replanning that blocks: `propose` is a model call, and the runner is
        driven from the event loop. `to_thread` keeps the cockpit responsive
        while it waits, exactly as `swarm plan` does.

        Every failure changes nothing. The honest default for "the planner had
        nothing to say" and for "the planner was unreachable" is the same DAG
        that was already there.
        """
        from . import swarmreplan

        runner = getattr(self, "_swarm_runner", None)
        if runner is None or not runner.replan.enabled:
            return []
        pending = runner.take_replans()
        applied = []
        for step in pending:
            names = [a.name for a in self.swarm.agents.values()]
            try:
                raw = await asyncio.to_thread(
                    swarmreplan.propose, step["goal"], step["output"],
                    existing_names=names,
                    max_new=runner.replan.max_new_steps)
            except Exception as e:  # noqa: BLE001
                self.state.logs.append(f"replan: {type(e).__name__}: {str(e)[:80]}")
                continue
            if not raw:
                continue
            out = runner.apply_expansion(step["id"], raw)
            if out.get("created"):
                self.state.history.append(
                    f"swarm: {step['name']} added {', '.join(out['created'])}")
                applied.append(out)
            elif out.get("reason"):
                self.state.history.append(
                    f"swarm: {step['name']} proposed work that was refused — "
                    f"{out['reason']}")
        return applied

    async def _swarm_plan(self, goal: str) -> None:
        """`swarm plan <goal>` — propose a DAG, create nothing.

        Building a DAG in the cockpit meant one `swarm add ... << deps` line
        per step, in an order that satisfied `add_checked`. The planner that
        removes that work already existed and only the browser could reach it,
        so the terminal — the surface people actually use — had the worst way
        to do the thing this program is for.

        The plan is held, not applied: creating N prompts a harness will
        execute is a decision worth one keystroke of confirmation, and the
        preview is the whole reason to show a DAG before it runs.
        """
        from . import swarmplan

        self.state.history.append(f"swarm: planning '{goal[:40]}' …")
        try:
            # The planner calls a model with a 30s timeout. On the event loop
            # that is a frozen cockpit: no keystrokes, no task updates, no
            # heartbeat, for half a minute.
            plan = await asyncio.to_thread(
                swarmplan.propose, goal,
                harnesses=list(self.harnesses),
                existing_names=[a.name for a in self.swarm.agents.values()])
        except Exception as e:  # noqa: BLE001
            self.state.swarm_plan = {}
            self.state.history.append(f"swarm plan: {type(e).__name__}: {str(e)[:80]}")
            return
        if not plan.ok:
            self.state.swarm_plan = {}
            self.state.history.append(
                f"swarm plan refused: {'; '.join(plan.problems)[:100]}")
            return
        self.state.swarm_plan = plan.as_dict()
        self.state.history.append(
            f"swarm plan: {len(plan.steps)} steps — 'swarm apply' to create them")

    def _swarm_apply(self) -> None:
        """`swarm apply` — create exactly the steps that were shown.

        Re-planning here would create a different DAG from the one on screen:
        the model is not deterministic, and "review, then commit" means nothing
        if the commit re-rolls the dice. So the held steps go back through
        `validate` (same fail-closed path the browser gets) and no further.
        """
        held = self.state.swarm_plan
        if not held or not held.get("steps"):
            self.state.history.append("swarm: nothing planned — 'swarm plan <goal>' first")
            return
        out = self.swarm_command({"action": "plan", "goal": held.get("goal", ""),
                                  "steps": held["steps"], "apply": True})
        applied = out.get("applied") or {}
        created = applied.get("created") or []
        if created:
            self.state.swarm_plan = {}
            self.state.history.append(
                f"swarm: created {', '.join(created)} — 'swarm run' to start")
        else:
            self.state.history.append(
                f"swarm apply: {applied.get('reason') or '; '.join(out.get('problems') or []) or 'refused'}")

    async def _swarm_command(self, text: str) -> None:
        """'swarm create|add|run|status|stop' — the same verbs the HUD sends.

        Everything here goes through `swarm_command`, because this path used to
        carry its own copy of each verb and the copies were wrong: `run` set
        every ready agent to WORKING and spawned nothing, so a DAG typed into
        the cockpit sat at layer one forever while the identical DAG driven
        from the HUD ran; `add` skipped the duplicate-name check that makes a
        dependency resolvable at all; `stop` cancelled the agents and left
        their tasks running.
        """
        parts = text.split()
        if len(parts) < 2:
            self.state.history.append("usage: swarm plan <goal> | apply | add <name> <goal> [<< deps] [>> writes] | run | status | log | stop")
            return
        sub = parts[1]
        rest = " ".join(parts[2:]) if len(parts) > 2 else ""
        if sub == "plan" and rest:
            await self._swarm_plan(rest)
        elif sub == "apply":
            self._swarm_apply()
        elif sub == "log":
            from .swarmlog import duration_text

            rows = self.swarm_runner.timeline()
            if not rows:
                self.state.history.append("swarm: nothing has run yet")
            for row in rows[-8:]:
                tries = f" ×{row['attempts']}" if row["attempts"] > 1 else ""
                self.state.history.append(
                    f"  {row['step']}: {row['outcome'] or 'running'} "
                    f"in {duration_text(row['seconds'])}{tries}")
        elif sub == "facts":
            # Every value the run has stated, in one place. Otherwise the only
            # way to see what a step produced is to open its output and read
            # for it, which is the situation FACT lines exist to end.
            stated = [(a.name, a.facts) for a in self.swarm.agents.values()
                      if getattr(a, "facts", None)]
            if not stated:
                self.state.history.append("swarm: no values stated yet")
            for name, facts in stated:
                for key, value in facts.items():
                    self.state.history.append(f"  {name}.{key} = {value}")
        elif sub == "create" and rest:
            self.swarm.decompose(rest)
            out = self.swarm_command({"action": "add",
                                      "name": self._free_agent_name(),
                                      "goal": rest})
            self.state.history.append(
                f"swarm created: {rest[:40]} — 'swarm run' to start it"
                if out.get("ok") else f"swarm: {out.get('reason', 'refused')}")
        elif sub == "add" and len(parts) >= 4:
            name = parts[2]
            goal = " ".join(parts[3:])
            deps: list[str] = []
            writes: list[str] = []
            # `>> path[, path]` declares what the step writes. Parsed before
            # deps so `goal << a >> f` and `goal >> f << a` both work — the
            # order somebody types two suffixes in is not a thing to be wrong
            # about.
            if " >> " in goal:
                goal, _, tail = goal.partition(" >> ")
                if " << " in tail:
                    tail, _, dep_tail = tail.partition(" << ")
                    deps = [d.strip() for d in dep_tail.split(",") if d.strip()]
                writes = [w.strip() for w in tail.split(",") if w.strip()]
            if " << " in goal:
                goal, _, dep_tail = goal.partition(" << ")
                deps = [d.strip() for d in dep_tail.split(",") if d.strip()]
            out = self.swarm_command({"action": "add", "name": name,
                                      "goal": goal.strip(), "deps": deps,
                                      "writes": writes})
            self.state.history.append(
                f"swarm agent added: {name}" if out.get("ok")
                else f"swarm: {out.get('reason', 'refused')}")
        elif sub == "run":
            out = self.swarm_command({"action": "run_ready"})
            started = out.get("started") or []
            self.state.history.append(
                f"swarm: started {', '.join(started)}" if started
                else f"swarm: {out.get('reason') or 'no agents ready (check deps)'}")
            for d in (out.get("deferred") or [])[:3]:
                self.state.history.append(f"  held back: {d['name']} — {d['reason']}")
        elif sub == "status":
            # The dict shape, same as the bus publishes. `dashboard()` returns
            # the legacy box-drawing TEXT, and every consumer downstream —
            # panel, HUD, jarvis, dashboard — reads the dict. Typing `swarm
            # status` therefore used to produce a WORSE view than doing
            # nothing, which is the opposite of what the command is for.
            summary = self.swarm.status_summary()
            summary["agents"] = [a.as_dict() for a in self.swarm.agents.values()]
            self.state.swarm_dashboard = summary
            from .swarmview import explain
            self.state.history.append(f"swarm: {explain(summary['agents'])}")
        elif sub == "stop":
            out = self.swarm_command({"action": "stop_all"})
            stopped = out.get("stopped") or []
            self.state.history.append(
                f"swarm: stopped {', '.join(stopped)}" if stopped
                else "swarm: nothing was running")
        else:
            self.state.history.append(f"unknown swarm subcommand: {sub}")

    def _agent_swarm_tool(self, goal: str) -> str:
        try:
            orch = self.swarm
            orch.decompose(goal)
            orch.add_agent("Agent A", f"work on: {goal}")
            self.state.swarm_dashboard = orch.dashboard()
            return f"swarm planned for: {goal[:60]}"
        except Exception as e:  # noqa: BLE001
            return f"swarm error: {e}"
