"""
store.py — the testable brain of aion (no Textual, no I/O beyond the bus).

This is the unidirectional-data-flow core:
  Intent -> Store.handle() -> state mutates -> emits events -> (UI re-renders)

All business logic lives here so it can be unit-tested with zero UI, and the
whole app can be driven as a pipeline (see tests/test_pipeline.py). The Textual
app is a thin view: it calls store.handle(intent) and renders store.state.
"""
from __future__ import annotations

import asyncio
import shutil
import time
from dataclasses import dataclass, field
from typing import Any

from .core import (
    Bus, Intent, IntentType, TaskRegistry, SessionStore,
    Task, TaskState, TOPIC_VOICE, TOPIC_HERMES, TOPIC_SKILL, TOPIC_SETTINGS,
    TOPIC_INTENT, TOPIC_PHYSIS, TOPIC_HITL, load_config,
)
from .hitl import GateBook, RISK_HIGH
from .memory import MemoryStore
from .gbrain import BrainStore
from .credentials import CredentialStore, PROVIDER_PRESETS
from .voice.persona import Persona
from .llm import ChatSession, format_conversation, chat_send
from .swarm import SwarmOrchestrator, AgentStatus as SwarmAgentStatus
from .modes import get_mode, list_modes, mode_command, MODES, ModeConfig
from .dashboard import collect_dashboard
from .physis import get_client as _get_physis  # coherence brain (soft-fails if down)
from .context import ContextRouter
from .agents import AgentStore
from .board import BoardStore



@dataclass
class ViewState:
    """Plain snapshot the UI renders from. No behavior, easy to assert on."""
    active_ws: int = 0
    focus: int = 0
    active_harness: str = ""
    voice_active: bool = False
    deck_app: bool = False        # CyclUno deck routed to the virtual gamepad
    tasks: list[Task] = field(default_factory=list)
    history: list[str] = field(default_factory=list)
    stats: dict[str, dict] = field(default_factory=dict)
    logs: list[str] = field(default_factory=list)
    hermes_kanban: list[dict] = field(default_factory=list)
    hermes_memory: list[dict] = field(default_factory=list)
    hermes_gateway: dict = field(default_factory=dict)
    skills: list[dict] = field(default_factory=list)
    settings_providers: dict[str, dict] = field(default_factory=dict)
    active_mode: str = "default"
    swarm_dashboard: str = ""
    task_history: list[dict] = field(default_factory=list)  # completed tasks
    compare_result: dict = field(default_factory=dict)      # multi-model compare
    suggestions: list[str] = field(default_factory=list)    # proactive jarvis
    term_command: str = ""        # app command for the Term workspace ("" = default btop)
    observer_ai: bool = False     # opt-in LLM layer of the Term observer HUD
    last_command: str = ""        # most recent command text (for context routing)
    workflows: list[dict] = field(default_factory=list)   # WorkflowRow dicts for HUD pulse
    workflows_live: int = 0       # count of non-done workflows
    runs_tab: str = "processes"   # Runs workspace: "processes" | "results"


class Store:
    """Owns all aion state and intent handling. UI-agnostic."""

    def __init__(self, cfg: dict | None = None, bus: Bus | None = None,
                 harnesses: dict | None = None, store: SessionStore | None = None) -> None:
        self.cfg = cfg or load_config()
        self.bus = bus or Bus()
        self.store = store or SessionStore()
        self.registry = TaskRegistry(self.bus)
        # harnesses: id -> Harness. Provided by caller (app wires real ones,
        # tests can pass fakes). Falls back to empty.
        self.harnesses: dict = harnesses or {}
        self._prev_task_states: dict[str, TaskState] = {}
        self._task_prompts: dict[str, str] = {}  # task id -> last prompt (for rerun)
        self._loop = None  # captured event loop (set in _chat for agent tools)
        self.remote_callback = None  # set by app: async fn(cmd, args) -> str
        self.fleet_callback = None   # set by app: async fn(text) -> str
        self.memory = BrainStore()        # gbrain MCP with MemoryStore fallback
        self.credentials = CredentialStore()  # structured provider profiles
        from .todos import TodoStore
        self.todos = TodoStore()
        self.agent_store = AgentStore()
        self.board_store = BoardStore()
        self._profile_scanning = False
        self.chat = ChatSession()
        self.swarm = SwarmOrchestrator(bus=self.bus)
        self.active_mode_cfg: ModeConfig = get_mode("default") or MODES["default"]
        self.state = ViewState(active_harness=self._first_harness())
        # human-in-the-loop approval gates. Fail-closed: nothing auto-approves
        # unless a policy vouches for it (none does by default).
        self.gates = GateBook()
        self._gate_store = None      # lazy: constructing a Store must not touch disk
        self._external_agents_cache: tuple[float, list[dict]] = (0.0, [])
        # subscribe to bus topics so the store stays the source of truth
        self.bus.subscribe("task", self._on_task_event)
        self.bus.subscribe("stats", self._on_stats)
        self.bus.subscribe("log", self._on_log)
        self.bus.subscribe("mode", self._on_mode)
        self.bus.subscribe(TOPIC_HERMES, self._on_hermes)
        self.bus.subscribe(TOPIC_SKILL, self._on_skill)
        self.bus.subscribe(TOPIC_SETTINGS, self._on_settings)
        self.bus.subscribe(TOPIC_PHYSIS, self._on_physis)
        # restore interrupted tasks from a previous crash
        for t in self.store.load():
            self.registry.ingest(t)

    # ---- helpers --------------------------------------------------------
    def _first_harness(self) -> str:
        return next(iter(self.harnesses), next(iter(self.cfg.get("harnesses", [{}])), {}).get("id", ""))

    async def _load_hermes_data(self) -> None:
        try:
            from .hermes import KanbanReader, HermesMemoryReader
            tasks = KanbanReader().tasks(limit=20)
            kanban = [{"id": t.id, "title": t.title, "status": t.status,
                       "assignee": t.assignee} for t in tasks]
            await self.bus.publish(TOPIC_HERMES, {"action": "kanban", "data": {"tasks": kanban}})
            sections = HermesMemoryReader().sections()
            await self.bus.publish(TOPIC_HERMES, {"action": "memory", "data": {"sections": sections}})
        except Exception:
            pass

    async def _load_settings_data(self) -> None:
        try:
            profiles = self.credentials.list()
            providers: dict[str, dict] = {}
            for p in profiles:
                providers[p["label"]] = {
                    "endpoint": p["endpoint"],
                    "key_preview": p["key_preview"],
                    "kind": p["kind"],
                    "active": p["active"],
                }
            self.state.settings_providers = providers
            await self.bus.publish(TOPIC_SETTINGS, {
                "action": "providers",
                "data": providers,
            })
        except Exception:
            pass

    async def _load_skills_data(self) -> None:
        try:
            tools = []
            for binary, name in [("opencode", "OpenCode"), ("hermes", "Hermes"),
                                 ("agy", "Antigravity"), ("omniroute", "OmniRoute"),
                                 ("claude", "Claude Code"), ("free-coding-models", "FCM"),
                                 ("codex", "Codex CLI")]:
                if shutil.which(binary) is not None:
                    tools.append({"name": name, "description": f"`{binary}` found", "source": "PATH"})
            # scan known skill dirs
            skill_dirs = [
                Path.home() / ".config" / "opencode" / "skills",
                Path.home() / ".claude" / "skills",
                Path.home() / ".agents" / "skills",
            ]
            for sd in skill_dirs:
                if sd.exists():
                    for p in sd.iterdir():
                        if p.is_dir() and (p / "SKILL.md").exists():
                            tools.append({"name": p.name, "description": "installed skill", "source": str(sd)})
            self.state.skills = tools
            await self.bus.publish(TOPIC_SKILL, {"action": "list", "data": tools})
        except Exception:
            pass

    def _current_items(self) -> list[dict]:
        ws = self.cfg["workspaces"][self.state.active_ws]["id"]

        # always collect workflows for the HUD pulse (stored on ViewState)
        self._collect_workflows()

        if ws == "desktop":
            # freshest task list for workflow collector
            self.state.tasks = list(self.registry.tasks.values())
            swarm_agents = [a.as_dict() for a in self.swarm.agents.values()] if self.swarm.agents else None
            board_list = None
            try:
                board_list = []
                for b in self.board_store.list_all():
                    bd = b.as_dict()
                    # column_data for glance
                    col_data: dict = {}
                    for c in b.cards:
                        col_data.setdefault(c.column, []).append(c.as_dict())
                    bd["column_data"] = col_data
                    bd["card_count"] = len(b.cards)
                    board_list.append(bd)
            except Exception:
                board_list = None
            ag_entities = None
            ae = self.state.stats.get("agent_entity")
            if ae and ae.get("agents"):
                ag_entities = ae["agents"]
            d = collect_dashboard(
                self.state, self.cfg, todos=self.todos,
                swarm_agents=swarm_agents,
                boards=board_list,
                agent_entities=ag_entities,
            )
            self._maybe_rescan_profile(d.profile)
            # merge project stats into desktop dashboard
            pj = self.state.stats.get("projects")
            proj_data = list(pj["projects"]) if pj and pj.get("projects") else []
            # merge recent AI sessions from Hermes/opencode
            st = self.state.stats.get("stats", {})
            recent_sessions = st.get("recent_sessions", []) if isinstance(st, dict) else []
            # merge interrupted tasks
            interrupted = [t.as_dict() for t in self.registry.tasks.values()
                          if t.state.value == "interrupted"]
            dashboard = d.as_dict()
            dashboard["projects"] = proj_data
            dashboard["recent_sessions"] = recent_sessions
            dashboard["interrupted_tasks"] = interrupted
            return [{"type": "dashboard", "data": dashboard}]
        if ws == "models":
            ctx = ContextRouter().resolve(self)
            domain_val = ctx.domain.value
            active_id = self.state.active_harness
            items = []
            for hid, h in self.harnesses.items():
                tags = h.cfg.context_tags
                if domain_val in tags or hid == active_id:
                    items.append({
                        "id": hid, "name": h.name, "vram": h.vram_mb,
                        "tier": h.tier, "running": self._running_for(hid),
                        "context_tag": domain_val if domain_val in tags else "active",
                    })
            # Show active harness first, then context-match, then rest
            matched = [i for i in items if i["context_tag"] != "active"]
            rest = [i for i in items if i["context_tag"] == "active"]
            if active_id and not any(i["id"] == active_id for i in items):
                h = self.harnesses.get(active_id)
                if h:
                    rest.append({"id": active_id, "name": h.name, "vram": h.vram_mb,
                                 "tier": h.tier, "running": self._running_for(active_id),
                                 "context_tag": "active"})
            return matched + rest
        if ws == "tasks":
            items = [t.as_dict() for t in self.registry.tasks.values()]
            # merge board kanban data
            bd = self.state.stats.get("board")
            if bd and bd.get("ok"):
                items.append({"type": "board", "boards": bd.get("boards", [])})
            return items
        if ws == "runs":
            from . import runs as _runs
            agent_ids = _runs.agent_harness_ids(self.harnesses)
            tasks = list(self.registry.tasks.values())
            counts = _runs.tab_counts(tasks, agent_ids)
            rows = _runs.collect_runs(tasks, agent_ids, self.state.runs_tab)
            # item 0 is the tab bar; the rest are runs. Activate on the bar
            # flips the tab, so focus starting at 0 is a feature.
            items = [{"type": "runs_tabs", "tab": self.state.runs_tab,
                      "counts": counts}]
            items += [r.as_dict() for r in rows]
            return items
        if ws == "settings":
            asyncio.create_task(self._load_settings_data())
            asyncio.create_task(self._load_skills_data())
            providers = [{"id": k, "endpoint": v.get("endpoint", ""),
                          "key_preview": v.get("key_preview", "")}
                         for k, v in self.state.settings_providers.items()]
            # merge skills data
            skills = [{"type": "skill", "id": s.get("name", s.get("id", "")),
                       "name": s.get("name", ""),
                       "description": s.get("description", ""),
                       "source": s.get("source", "")}
                      for s in self.state.skills]
            from .fleet import describe_settings, instance_root, shared_root
            fleet_rows = [{"type": "fleet_setting", "id": d["key"], **d}
                          for d in describe_settings()]
            fleet_hdr = [{"type": "fleet_header", "id": "fleet",
                          "instance_root": str(instance_root()),
                          "shared_root": str(shared_root())}]
            items = fleet_hdr + fleet_rows + providers + skills
            if not items:
                items = [{"type": "env_hint", "id": "setup",
                          "text": "No env vars or skills found.",
                          "hint": "Ctrl-K: 'setup wizard' to configure providers"}]
            return items
        if ws == "vault":
            v = self.state.stats.get("vault")
            items = []
            if v and v.get("ok"):
                items = list(v.get("nodes", []))
            else:
                items = [{"name": "(none)", "title": "vault not loaded",
                          "preview": v.get("error", "") if v else ""}]
            # merge memory facts
            memory_items = [{"type": "memory_fact", "n": m["n"], "text": m["text"],
                             "when": m.get("when", "")}
                            for m in self.memory.items()]
            return items + memory_items
        if ws in ("system", "sys"):
            return [{"kind": "live"}]  # rendered specially from stats
        if ws == "agent":
            ag = self.state.stats.get("agent_entity")
            s = self.state.swarm_dashboard
            cr = self.state.compare_result
            mode = ContextRouter().agent_mode_for(self)
            if mode == "swarm" and s:
                return [{"type": "swarm_dashboard", "data": s,
                         "mode_label": "SWARM"}]
            if mode == "compare" and cr and cr.get("answers"):
                return [{"type": "compare", "prompt": cr.get("prompt", ""),
                         "answers": cr["answers"], "done": cr.get("done", False),
                         "mode_label": "COMPARE"}]
            if mode == "agents" and ag and ag.get("ok"):
                return [{"type": "agents", "agents": ag.get("agents", []),
                         "health_context": ag.get("health_context", {}),
                         "mode_label": "AGENTS"}]
            if ag and ag.get("ok"):
                return [{"type": "agents", "agents": ag.get("agents", []),
                         "health_context": ag.get("health_context", {}),
                         "mode_label": "AGENTS"}]
            if s:
                return [{"type": "swarm_dashboard", "data": s,
                         "mode_label": "SWARM"}]
            if cr and cr.get("answers"):
                return [{"type": "compare", "prompt": cr.get("prompt", ""),
                         "answers": cr["answers"], "done": cr.get("done", False),
                         "mode_label": "COMPARE"}]
            msgs = format_conversation(self.chat)
            if msgs:
                return [{"type": "chat", "messages": msgs,
                         "mode_label": "CHAT"}]
            return [{"type": "empty",
                     "label": "Agent workspace. Create agents, spawn a swarm, or start a chat."}]
        if ws == "term":
            return [{"kind": "terminal"}]
        return []

    def _detect_external_agents(self) -> list[dict]:
        """Detect running external coding agent processes (opencode, agy, ...)."""
        import subprocess, os, time
        BINARIES = {
            "opencode": "OpenCode",
            "agy": "Antigravity",
            "claude": "Claude Code",
            "codex": "Codex CLI",
        }
        agents = []
        for binary, label in BINARIES.items():
            if shutil.which(binary) is None:
                continue
            try:
                result = subprocess.run(
                    ["pgrep", "-x", binary],
                    capture_output=True, text=True, timeout=2,
                )
                pids = [int(p) for p in result.stdout.strip().split() if p]
                for pid in pids:
                    try:
                        create_time = os.path.getctime(f"/proc/{pid}")
                        agents.append({
                            "name": label,
                            "pid": pid,
                            "age_s": time.time() - create_time,
                        })
                    except (OSError, PermissionError):
                        pass
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass
        return agents

    def _collect_workflows(self) -> None:
        from .workflows import collect_workflows
        self.state.tasks = list(self.registry.tasks.values())
        swarm_agents = [a.as_dict() for a in self.swarm.agents.values()] if self.swarm.agents else None
        hermes_agents = []
        st = self.state.stats.get("stats") or {}
        if isinstance(st, dict):
            hermes_agents = list(st.get("agents") or [])
        board_list = None
        try:
            board_list = []
            for b in self.board_store.list_all():
                bd = b.as_dict()
                col_data: dict = {}
                for c in b.cards:
                    col_data.setdefault(c.column, []).append(c.as_dict())
                bd["column_data"] = col_data
                bd["card_count"] = len(b.cards)
                board_list.append(bd)
        except Exception:
            board_list = None
        ag_entities = None
        ae = self.state.stats.get("agent_entity")
        if ae and ae.get("agents"):
            ag_entities = ae["agents"]
        ext_cache_ts, ext_cache_data = self._external_agents_cache
        if time.time() - ext_cache_ts > 5.0:
            self._external_agents_cache = (time.time(), self._detect_external_agents())
        external_agents = self._external_agents_cache[1]
        wrows = collect_workflows(
            tasks=list(self.registry.tasks.values()),
            swarm_dashboard=self.state.swarm_dashboard,
            swarm_agents=swarm_agents,
            boards=board_list,
            hermes_agents=hermes_agents,
            agent_entities=ag_entities,
            external_agents=external_agents,
        )
        self.state.workflows = [w.as_dict() for w in wrows]
        self.state.workflows_live = sum(
            1 for w in self.state.workflows
            if w.get("stage") in ("act", "wait", "blocked", "plan", "failed", "verify")
        )

    def _maybe_rescan_profile(self, profile: dict) -> None:
        """Keep the DATA trackers live: background rescan when stale."""
        import time
        from .profile import RESCAN_AFTER_S
        if (not profile or self._profile_scanning
                or time.time() - profile.get("scanned_at", 0) < RESCAN_AFTER_S):
            return
        self._profile_scanning = True

        def worker():
            from .profile import scan, save, load
            try:
                prev = load()
                p = scan(profile.get("scopes", []), prev=prev)
                save(p)
                self.state.stats["profile"] = p
            except Exception:
                pass
            finally:
                self._profile_scanning = False

        import threading
        threading.Thread(target=worker, daemon=True).start()

    def _running_for(self, hid: str) -> int:
        return sum(1 for t in self.registry.tasks.values()
                   if t.harness == hid and t.state.value == "running")

    def snapshot(self) -> ViewState:
        self.state.tasks = list(self.registry.tasks.values())
        self.state.history = self.state.history[-50:]
        return self.state

    # ---- intent handling (the only way to mutate state) -----------------
    def handle(self, intent: Intent) -> None:
        t = intent.type
        p = intent.payload
        # A pending HITL gate captures confirm/cancel: the same ACTIVATE that
        # any device emits (deck button, joystick click, ring tap, Enter) now
        # approves it, BACK rejects it — so no new per-device mapping is needed.
        if self.gates.has_pending():
            if t in (IntentType.ACTIVATE, IntentType.HITL_APPROVE):
                self._resolve_gate(True)
                return
            if t in (IntentType.BACK, IntentType.HITL_REJECT, IntentType.CANCEL):
                self._resolve_gate(False)
                return
        if t in (IntentType.HITL_APPROVE, IntentType.HITL_REJECT):
            return  # no gate pending — nothing to resolve
        if t == IntentType.NAVIGATE:
            self._navigate(p.get("dir", "down"))
        elif t == IntentType.ACTIVATE:
            self._activate()
        elif t == IntentType.BACK:
            pass
        elif t == IntentType.SWITCH_WORKSPACE:
            if p.get("index") is not None:
                self.state.active_ws = p["index"] % len(self.cfg["workspaces"])
            elif p.get("delta"):
                self.state.active_ws = (self.state.active_ws + p["delta"]) % len(self.cfg["workspaces"])
            self.state.focus = 0
        elif t == IntentType.COMMAND:
            asyncio.create_task(self._run_command(p.get("text", "")))
        elif t == IntentType.PAUSE:
            self._control("pause")
        elif t == IntentType.RESUME:
            self._control("resume")
        elif t == IntentType.CANCEL:
            self._control("cancel")
        elif t == IntentType.RERUN:
            self._control("rerun")
        elif t == IntentType.COMPARE:
            asyncio.create_task(self._run_compare(p.get("text", "")))
        elif t == IntentType.ACT:
            # Act on the top Jarvis suggestion: run its action command.
            sugg = self.state.suggestions
            if sugg and sugg[0].action:
                action = sugg[0].action
                self.state.logs.append(f"▶ Jarvis: {action}")
                self.state.logs = self.state.logs[-50:]
                self.state.suggestions = sugg[1:]
                asyncio.create_task(self._run_command(action))
        # MODE_TOGGLE/SELECT handled by app/voice layer if needed

    def _navigate(self, direction: str) -> None:
        items = self._current_items()
        if not items:
            if direction in ("left", "right"):
                self.state.active_ws = (self.state.active_ws + (1 if direction == "right" else -1)) % len(self.cfg["workspaces"])
            return
        if direction == "up":
            self.state.focus = max(0, self.state.focus - 1)
        elif direction == "down":
            self.state.focus = min(len(items) - 1, self.state.focus + 1)
        elif direction in ("left", "right"):
            self.state.active_ws = (self.state.active_ws + (1 if direction == "right" else -1)) % len(self.cfg["workspaces"])
            self.state.focus = 0

    def _focused_task(self) -> Task | None:
        items = self._current_items()
        ws_id = self.cfg["workspaces"][self.state.active_ws]["id"]
        if ws_id not in ("tasks", "runs") or not items:
            return None
        if self.state.focus >= len(items):
            return None
        item = items[self.state.focus]
        if "id" not in item:        # runs tab-bar row has no task
            return None
        return self.registry.tasks.get(item["id"])

    def toggle_runs_tab(self) -> None:
        from .runs import other_tab
        self.state.runs_tab = other_tab(self.state.runs_tab)
        self.state.focus = 0

    def _control(self, action: str) -> None:
        task = self._focused_task()
        if task is not None:
            self.control_task(task.id, action)

    def control_task(self, task_id: str, action: str) -> dict:
        """Apply an action to a task. The ONE implementation of these rules.

        Both the TUI keybindings and the web HUD (over the remote transport)
        land here, so the two can never disagree about whether a finished task
        can be paused. Returns an `agentctl.Outcome` dict — a refusal always
        carries its reason, because a button that silently does nothing is the
        worst answer available.
        """
        from . import agentctl

        task = self.registry.tasks.get(task_id)
        if task is None:
            return agentctl.Outcome(False, action, task_id,
                                    "no such task").as_dict()
        state = task.state.value
        ok, why = agentctl.legal(action, state, task.paused)
        if not ok:
            return agentctl.Outcome(False, action, task_id, why, state).as_dict()

        h = self.harnesses.get(task.harness, self.harnesses.get(self.state.active_harness))
        if h is None:
            return agentctl.Outcome(False, action, task_id,
                                    f"harness {task.harness!r} is not loaded",
                                    state).as_dict()

        if action == "pause":
            h.pause(task)
        elif action == "resume":
            h.resume(task)
        elif action == "cancel":
            h.cancel(task)
        elif action == "rerun":
            asyncio.create_task(self._respawn(task))

        self.registry.log(task, f"[control] {agentctl.describe(action)}")
        return agentctl.Outcome(True, action, task_id, "",
                                agentctl.next_state(action) or state).as_dict()

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

            self._swarm_runner = SwarmRunner(
                self.swarm,
                spawn=lambda agent, prompt: self._spawn_now(
                    getattr(agent, "harness", "") or default_harness(),
                    prompt, label=f"swarm/{agent.name}"),
                harness=default_harness(),
                harness_vram=vram,
            )
        return self._swarm_runner

    def swarm_command(self, params: dict) -> dict:
        """One entry point for every swarm verb, used by the web HUD.

        Dispatch lives here rather than in the transport so the parameter
        shapes are checked once, in the process that owns the DAG. The
        orchestrator answers whether an action is legal and why not; this only
        decides which question to ask it.
        """
        params = params if isinstance(params, dict) else {}
        action = str(params.get("action", "")).strip()
        agent_id = str(params.get("agent_id", "")).strip()

        if action == "add":
            deps = params.get("deps") or []
            if not isinstance(deps, list):
                return {"ok": False, "reason": "deps must be a list of names"}
            return self.swarm.add_checked(
                str(params.get("name", "")), str(params.get("goal", "")),
                [str(d) for d in deps], harness=str(params.get("harness", "")))
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
            return self.swarm_runner.status()
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

    def spawn_task(self, harness_id: str, prompt: str) -> dict:
        """Start a task from outside the TUI's own input path.

        Deliberately routed through `_spawn`, not around it, so a spawn from
        the web HUD gets the same physis classification and — crucially — the
        same HITL gate as one typed into the cockpit. The web never runs a
        harness itself; it asks this process to, and this process is where
        approval lives.
        """
        from . import agentctl

        prompt = (prompt or "").strip()
        if not prompt:
            return agentctl.Outcome(False, "spawn", reason="empty prompt").as_dict()
        hid = harness_id or self.state.active_harness
        if hid not in self.harnesses:
            return agentctl.Outcome(
                False, "spawn", reason=f"no harness {hid!r}").as_dict()
        before = set(self.registry.tasks)
        asyncio.create_task(self._spawn(hid, prompt))
        out = agentctl.Outcome(True, "spawn", reason="", state="pending").as_dict()
        # The task id is assigned inside the coroutine, so it is not available
        # yet. Say so rather than inventing one; the graph picks it up on the
        # next push either way.
        out["harness"] = hid
        out["pending_before"] = len(before)
        return out

    def _activate(self) -> None:
        ws = self.cfg["workspaces"][self.state.active_ws]["id"]
        items = self._current_items()
        if not items or self.state.focus >= len(items):
            return
        item = items[self.state.focus]
        if ws == "models":
            self.state.active_harness = item["id"]
            asyncio.create_task(self._spawn(item["id"], "manual run"))
        elif ws == "tasks":
            task = self.registry.tasks.get(item["id"])
            if task is None:
                return
            h = self.harnesses.get(task.harness, self.harnesses.get(self.state.active_harness))
            if task.state.value in ("interrupted", "cancelled", "failed"):
                asyncio.create_task(self._respawn(task))
            elif task.state.value == "running":
                if task.paused:
                    h.resume(task)
                else:
                    h.pause(task)
        elif ws == "runs":
            if item.get("type") == "runs_tabs":
                self.toggle_runs_tab()
                return
            task = self.registry.tasks.get(item.get("id", ""))
            if task is None:
                return
            h = self.harnesses.get(task.harness, self.harnesses.get(self.state.active_harness))
            # results re-run; live processes pause/resume — same as Tasks
            if task.state.value in ("interrupted", "cancelled", "failed"):
                asyncio.create_task(self._respawn(task))
            elif task.state.value == "running" and h is not None:
                h.resume(task) if task.paused else h.pause(task)

    # ---- human-in-the-loop gates ----------------------------------------
    def _resolve_gate(self, approved: bool) -> None:
        gate = self.gates.resolve_latest(approved)
        if gate is not None:
            verb = "approved" if approved else "rejected"
            t = self.registry.tasks.get(gate.task_id)
            if t is not None:
                self.registry.log(t, f"[hitl] {verb}: {gate.action[:80]}")
        self.gates.clear_resolved()
        self._publish_gates()

    def _publish_gates(self) -> None:
        self.state.stats["hitl"] = [g.as_dict() for g in self.gates.pending()]
        # Also publish to disk so the web HUD (a different process) can show
        # them. A gate nobody notices is indistinguishable from a denial,
        # because wait() is fail-closed.
        try:
            if self._gate_store is None:
                from .hitl import GateStore
                self._gate_store = GateStore()
            self._gate_store.publish(self.gates)
        except Exception as e:  # noqa: BLE001
            print(f"[hitl] could not publish gates: {e}")

    def resolve_gate_by_id(self, gate_id: str, approved: bool) -> dict:
        """Resolve one gate by id — what the remote /gate endpoint calls."""
        gate = self.gates.resolve(gate_id, approved)
        if gate is None:
            return {"ok": False, "error": f"no pending gate {gate_id!r}"}
        verb = "approved" if approved else "rejected"
        t = self.registry.tasks.get(gate.task_id)
        if t is not None:
            self.registry.log(t, f"[hitl] {verb} (remote): {gate.action[:80]}")
        self.gates.clear_resolved()
        self._publish_gates()
        return {"ok": True, "gate": gate.as_dict()}

    def _needs_gate(self, h, prompt: str) -> bool:
        """Does spawning this harness+prompt need a human yes first?

        Two triggers: a harness explicitly flagged (config extra
        requires_approval), or a prompt that names a destructive shell action.
        """
        extra = getattr(getattr(h, "cfg", None), "extra", None) or {}
        if extra.get("requires_approval"):
            return True
        low = prompt.lower()
        return any(sig in low for sig in (
            "rm -rf", "drop table", "force push", "git push --force",
            "mkfs", "dd if=", ":(){", "sudo rm",
        ))

    def _spawn_now(self, harness_id: str, prompt: str, label: str = "") -> str:
        """Create a task, start it through the gate, and return its id.

        The synchronous half of `_spawn`. The swarm runner needs the task id
        to map an agent to its work, and `_spawn` cannot give it one — the id
        is assigned inside a coroutine nobody awaits.
        """
        h = self.harnesses.get(harness_id, self.harnesses.get(self.state.active_harness))
        if h is None:
            return ""
        task = self.registry.create(label or f"{h.name}: {prompt[:30]}", h.id)
        self._task_prompts[task.id] = prompt
        asyncio.create_task(self._gated_run(h, task, prompt))
        return task.id

    async def _spawn(self, harness_id: str, prompt: str) -> None:
        h = self.harnesses.get(harness_id, self.harnesses.get(self.state.active_harness))
        if h is None:
            return
        task = self.registry.create(f"{h.name}: {prompt[:30]}", h.id)
        self._task_prompts[task.id] = prompt
        # physis brain: classily the goal so it gets a semiotic cell
        # (what DOMAIN of work it is). Soft-fail: if physis is down
        # we just lose the tag, the task still runs.
        client = _get_physis()
        res = client.classify(prompt)
        if res.top:
            task.domain = res.label()  # e.g. "CONSTRUCT/REST"
            await self.bus.publish("physis",
                {"action": "classify", "task": task.id,
                 "label": res.label(), "cells": [c.__dict__ for c in res.cells]})
            _ = client.register(f"task:{task.id}", 1.0, edge_to=res.label())
        asyncio.create_task(self._gated_run(h, task, prompt))

    async def _gated_run(self, h, task: Task, prompt: str) -> None:
        """Await a HITL gate for a privileged spawn, then run — or cancel."""
        if self._needs_gate(h, prompt):
            gate = self.gates.request(task.id, f"run {h.name}: {prompt[:60]}",
                                      risk=RISK_HIGH)
            self.registry.log(task, f"[hitl] awaiting approval: {gate.action[:80]}")
            self._publish_gates()
            await self.bus.publish(TOPIC_HITL,
                {"action": "request", "gates": self.state.stats.get("hitl", [])})
            approved = await self.gates.wait(gate)
            if not approved:
                self.registry.set_state(task, TaskState.CANCELLED)
                return
        await h.run(task, prompt)

    async def _respawn(self, old: Task) -> None:
        h = self.harnesses.get(old.harness, self.harnesses.get(self.state.active_harness))
        if h is None:
            return
        task = self.registry.create(f"{h.name}: {old.label[:24]}", h.id)
        # Through the gate, not around it. This used to call h.run() directly,
        # which meant a prompt dangerous enough to need approval the first time
        # could be re-run with none — and "rerun" is one button press, now
        # reachable from the web HUD as well as the cockpit.
        prompt = self._task_prompts.get(old.id, old.label)
        self._task_prompts[task.id] = prompt
        asyncio.create_task(self._gated_run(h, task, prompt))

    async def _run_command(self, text: str, _interpreted: bool = False) -> None:
        self.state.history.append(text)
        self.state.last_command = text
        parts = text.split(" ", 1)
        if parts[0] == "hitl" and len(parts) == 2:
            # voice/palette HITL: "hitl approve" / "hitl reject"
            if parts[1].strip() in ("approve", "yes"):
                self._resolve_gate(True)
            elif parts[1].strip() in ("reject", "no"):
                self._resolve_gate(False)
            return
        if parts[0] == "goto" and len(parts) == 2:
            ws_ids = [w["id"] for w in self.cfg["workspaces"]]
            target = parts[1].strip().lower()
            if target in ws_ids:
                self.state.active_ws = ws_ids.index(target)
                self.state.focus = 0
            else:
                self.state.logs.append(f"goto: no workspace '{target}' ({' '.join(ws_ids)})")
                self.state.logs = self.state.logs[-50:]
            return
        if parts[0] == "help":
            if len(parts) > 1 and parts[1] == "manual":
                return
            self.state.logs.extend([
                "Just type what you want — e.g.:",
                "  'todo buy milk' · 'agent create Alice' · 'swarm research'",
                "  'compare explain recursion' · 'goto vault'",
                "  'help manual' for full reference · press ? for keys",
            ])
            self.state.logs = self.state.logs[-50:]
            return
        if parts[0] == "note" and len(parts) == 2:
            result = self.memory.add(parts[1])
            src = result.get("source", "memory")
            self.state.logs.append(f"noted ({src}): {parts[1][:60]}")
            self.state.logs = self.state.logs[-50:]
            return
        if parts[0] == "mem":
            if len(parts) == 2:
                hits = self.memory.search(parts[1], limit=8)
                if hits:
                    lines = [f"  {h.get('text','')[:80]}" for h in hits]
                    self.state.logs.append(f"mem: {len(hits)} result(s) for '{parts[1]}'")
                    self.state.logs.extend(lines)
                else:
                    self.state.logs.append(f"mem: no results for '{parts[1]}'")
                self.state.logs = self.state.logs[-50:]
            else:
                self.memory.query = ""
                ws_ids = [w["id"] for w in self.cfg["workspaces"]]
                if "vault" in ws_ids:
                    self.state.active_ws = ws_ids.index("vault")
                    self.state.focus = 0
            return
        if parts[0] == "forget" and len(parts) == 2 and parts[1].strip().isdigit():
            ok = self.memory.forget(int(parts[1]))
            self.state.logs.append("forgotten" if ok else "bad index")
            self.state.logs = self.state.logs[-50:]
            return
        if parts[0] == "think" and len(parts) >= 2:
            question = parts[1]
            self.state.logs.append(f"thinking: {question[:60]}…")
            self.state.logs = self.state.logs[-50:]
            result = self.memory.think(question)
            if result and result.get("answer"):
                self.state.logs.append(f"→ {result['answer'][:200]}")
                self.state.logs.append(f"  (gap analysis: {result.get('gap_analysis', 'none')})")
            else:
                self.state.logs.append("think: gbrain not available")
            self.state.logs = self.state.logs[-50:]
            return
        if parts[0] == "gbrain" and len(parts) == 1:
            from .gbrain import identity
            info = identity()
            if "error" in info:
                self.state.logs.append(f"gbrain: {info['error']}")
            else:
                v = info.get("version", "?")
                pc = info.get("page_count", info.get("total_pages", "?"))
                cc = info.get("chunk_count", "?")
                self.state.logs.append(f"gbrain {v}: {pc} pages, {cc} chunks")
            self.state.logs = self.state.logs[-50:]
            return
        if parts[0] == "provider":
            await self._provider_command(text)
            return
        if parts[0] == "todo":
            arg = parts[1].strip() if len(parts) == 2 else ""
            sub = arg.split(" ", 1)
            if sub[0] == "done" and len(sub) == 2 and sub[1].strip().isdigit():
                ok = self.todos.done(int(sub[1]))
                self.state.logs.append(f"todo #{sub[1]} done" if ok else "todo: bad index")
            elif sub[0] == "rm" and len(sub) == 2 and sub[1].strip().isdigit():
                ok = self.todos.rm(int(sub[1]))
                self.state.logs.append(f"todo #{sub[1]} removed" if ok else "todo: bad index")
            elif arg:
                self.todos.add(arg)
                self.state.logs.append(f"todo added: {arg[:40]}")
            else:
                self.state.logs.append("usage: todo <text> | todo done <n> | todo rm <n>")
            self.state.logs = self.state.logs[-50:]
            return
        if parts[0] in ("setup", "scan"):
            if parts[0] == "setup" and len(parts) >= 2 and parts[1] == "wizard":
                return
            if parts[0] == "setup" and len(parts) >= 4 and parts[1] == "set":
                key = parts[2].upper()
                val = parts[3]
                env_path = Path.home() / ".env"
                lines = []
                found = False
                if env_path.exists():
                    for line in env_path.read_text().splitlines():
                        stripped = line.strip()
                        if "=" in stripped and not stripped.startswith("#") and stripped.split("=", 1)[0].strip() == key:
                            lines.append(f"{key}={val}")
                            found = True
                            continue
                        lines.append(line)
                if not found:
                    lines.append(f"{key}={val}")
                env_path.write_text("\n".join(lines) + "\n")
                self.state.logs.append(f"setup: {key} written to ~/.env")
                self.state.logs = self.state.logs[-50:]
                return
            from .profile import SCOPES, load, scan, save
            prev = load()
            if parts[0] == "setup":
                scopes = [s for s in (parts[1].split() if len(parts) == 2 else [])
                          if s in SCOPES]
                if not scopes:
                    opts = " ".join(f"{k}({v})" for k, v in SCOPES.items())
                    self.state.logs.append(f"usage: setup <scopes> — {opts}")
                    self.state.logs = self.state.logs[-50:]
                    return
            else:
                scopes = (prev or {}).get("scopes", [])
                if not scopes:
                    self.state.logs.append("scan: run 'setup <scopes>' first")
                    self.state.logs = self.state.logs[-50:]
                    return
            self.state.logs.append(f"scanning disk for scopes: {' '.join(scopes)} ...")
            self.state.logs = self.state.logs[-50:]

            def worker():
                try:
                    p = scan(scopes, prev=prev)
                    save(p)
                    self.state.stats["profile"] = p
                    self.state.logs.append(
                        f"scan done: {len(p['trackers'])} trackers, "
                        f"{len(p['disk'])} dirs sized")
                except Exception as e:
                    self.state.logs.append(f"scan failed: {e}")
                self.state.logs = self.state.logs[-50:]
            import threading
            threading.Thread(target=worker, daemon=True).start()
            return
        if parts[0] == "observe" and len(parts) == 2:
            mode = parts[1].strip().lower()
            self.state.observer_ai = (mode in ("ai", "on"))
            self.state.logs.append(f"observer AI {'on' if self.state.observer_ai else 'off'}")
            self.state.logs = self.state.logs[-50:]
            return
        if parts[0] == "apps":
            from .apps import list_apps
            self.state.logs.extend(list_apps())
            self.state.logs = self.state.logs[-50:]
            return
        if parts[0] == "app" and len(parts) == 2:
            from .apps import resolve
            sub = parts[1].split(" ", 1)
            cmd, note = resolve(sub[0], sub[1] if len(sub) == 2 else "")
            if cmd is None:
                self.state.logs.append(note)
                self.state.logs = self.state.logs[-50:]
                return
            self.state.term_command = cmd
            ws_ids = [w["id"] for w in self.cfg["workspaces"]]
            if "term" in ws_ids:
                self.state.active_ws = ws_ids.index("term")
                self.state.focus = 0
            self.state.history.append(f"app: {cmd}")
            return
        if parts[0] in ("search", "web") and len(parts) == 2:
            # DeepSearch: run the web harness with the query as the prompt
            await self._spawn("web", parts[1])
            return
        if parts[0] == "tier" and len(parts) == 2:
            tier = parts[1].strip().lower()
            hid = next((hid for hid, h in self.harnesses.items() if h.tier == tier), None)
            if hid:
                self.state.active_harness = hid
            return
        if parts[0] == "theme" and len(parts) == 2:
            theme_name = parts[1].strip().lower()
            themes = self.cfg.get("themes", {})
            if theme_name in themes:
                self.cfg["theme"] = dict(themes[theme_name])
                self.state.history.append(f"theme switched to {theme_name}")
            return
        # Mode switching
        mode_id = mode_command(text)
        if mode_id:
            self._set_mode(mode_id)
            return
        # Swarm orchestration
        if parts[0] == "agent" and len(parts) >= 2:
            await self._agent_command(text)
            return
        if parts[0] == "board" and len(parts) >= 2:
            await self._board_command(text)
            return
        if parts[0] == "fleet":
            if self.fleet_callback:
                result = await self.fleet_callback(text)
                self.state.logs.append(result)
                self.state.logs = self.state.logs[-50:]
            else:
                self.state.logs.append("fleet: not available (app not connected)")
                self.state.logs = self.state.logs[-50:]
            return
        if parts[0] == "remote":
            if self.remote_callback:
                result = await self.remote_callback(text)
                self.state.logs.append(result)
                self.state.logs = self.state.logs[-50:]
            else:
                self.state.logs.append("remote: not available (app not connected)")
                self.state.logs = self.state.logs[-50:]
            return
        if parts[0] == "swarm" and len(parts) >= 2:
            await self._swarm_command(text)
            return
        # Explicit "run <harness> <prompt>" — used by the agent's tool calls
        # and by users. 3+ parts: parts[0]=run, parts[1]=harness, rest=prompt.
        if parts[0] == "run" and len(parts) >= 3 and parts[1] in self.harnesses:
            await self._spawn(parts[1], parts[2])
            return
        if len(parts) == 2 and parts[0] in self.harnesses:
            await self._spawn(parts[0], parts[1])
            return
        # ---- plain-language layer: rules first, then LLM translate -------
        if not _interpreted:
            from .interpret import interpret
            canon = interpret(text)
            if canon is None and " " in text.strip() and \
                    self.cfg["workspaces"][self.state.active_ws]["id"] != "agent":
                import asyncio as _aio
                from .interpret import llm_translate
                try:
                    canon = await _aio.get_event_loop().run_in_executor(
                        None, llm_translate, text)
                except Exception:
                    canon = None
            if canon:
                self.state.logs.append(f"→ {canon}")
                self.state.logs = self.state.logs[-50:]
                await self._run_command(canon, _interpreted=True)
                return
        if self.cfg["workspaces"][self.state.active_ws]["id"] == "agent":
            # Agent workspace: route unknown text to inline LLM chat
            await self._chat(text)
        else:
            await self._spawn(self.state.active_harness, text)

    async def _run_compare(self, text: str) -> None:
        """Side-by-side multi-model comparison. Switches Agent ws to show it."""
        from .llm import chat_send_multi
        prompt = text.strip()
        if not prompt:
            return
        providers = ["fcm", "groq"]
        self.state.compare_result = {"prompt": prompt, "answers": {}, "done": False}
        # jump to Agent workspace so the side-by-side is visible
        ws_ids = [w["id"] for w in self.cfg["workspaces"]]
        if "agent" in ws_ids:
            self.state.active_ws = ws_ids.index("agent")
            self.state.focus = 0
        # run in executor to avoid blocking the loop on network
        loop = asyncio.get_event_loop()
        answers = await loop.run_in_executor(None, chat_send_multi, prompt, providers)
        self.state.compare_result = {"prompt": prompt, "answers": answers, "done": True}
        self.state.history.append(f"compare: {prompt[:40]}")

    def _set_mode(self, mode_id: str) -> None:
        """Switch the operational mode."""
        cfg = get_mode(mode_id)
        if cfg is None:
            return
        self.active_mode_cfg = cfg
        self.state.active_mode = mode_id
        self.state.history.append(f"mode: {mode_id}")
        # If stealth mode, auto-apply dim theme
        if cfg.dim_theme:
            dim = {"accent": "#3a5a6a", "ok": "#3a7a5a", "warn": "#5a5a3a",
                   "err": "#5a3a3a", "dim": "#2a2a2a"}
            self.cfg["theme"].update(dim)

    async def _swarm_command(self, text: str) -> None:
        """Handle 'swarm create|add|run|status|stop' commands."""
        parts = text.split()
        if len(parts) < 2:
            self.state.history.append("usage: swarm create <goal> | add <name> <goal> [deps] | run | status | stop")
            return
        sub = parts[1]
        rest = " ".join(parts[2:]) if len(parts) > 2 else ""
        if sub == "create" and rest:
            plan = self.swarm.decompose(rest)
            # Create initial agents from the plan
            a1 = self.swarm.add_agent("Agent-1", rest)
            self.swarm.set_status(a1.id, SwarmAgentStatus.WORKING)
            self.state.history.append(f"swarm created: {rest[:40]}")
        elif sub == "add" and len(parts) >= 4:
            name = parts[2]
            goal = " ".join(parts[3:])
            deps = []
            if " << " in goal:
                parts2 = goal.split(" << ", 1)
                goal = parts2[0]
                deps = [d.strip() for d in parts2[1].split(",")]
            a = self.swarm.add_agent(name, goal, deps=deps)
            self.state.history.append(f"swarm agent added: {name}")
        elif sub == "run":
            ready = self.swarm.agents_ready()
            if ready:
                for a in ready:
                    self.swarm.set_status(a.id, SwarmAgentStatus.WORKING)
                self.state.history.append(f"swarm: {len(ready)} agent(s) started")
            else:
                self.state.history.append("swarm: no agents ready (check deps)")
        elif sub == "status":
            self.state.swarm_dashboard = self.swarm.dashboard()
        elif sub == "stop":
            for a in self.swarm.agents.values():
                if a.status == SwarmAgentStatus.WORKING:
                    self.swarm.set_status(a.id, SwarmAgentStatus.CANCELLED)
            self.state.history.append("swarm: all agents stopped")
        else:
            self.state.history.append(f"unknown swarm subcommand: {sub}")

    async def _agent_command(self, text: str) -> None:
        parts = text.split()
        if len(parts) < 2:
            self.state.history.append("usage: agent create <name> [goal] | assign <name> <goal> | status | forget <name>")
            return
        sub = parts[1]
        rest = " ".join(parts[2:]) if len(parts) > 2 else ""
        if sub == "create" and rest:
            name = parts[2] if len(parts) > 2 else ""
            goal = " ".join(parts[3:]) if len(parts) > 3 else ""
            if not name:
                self.state.history.append("agent create: need a name")
                return
            a = self.agent_store.create(name, goal)
            self.state.history.append(f"agent created: {a.name} (id={a.id[:8]})")
            return
        if sub == "assign" and len(parts) >= 4:
            name = parts[2]
            goal = " ".join(parts[3:])
            a = self.agent_store.get_by_name(name)
            if a is None:
                self.state.history.append(f"agent '{name}' not found")
                return
            self.agent_store.set_goal(a.id, goal)
            # also try to assign any unassigned board cards
            for b in self.board_store.list_all():
                for c in b.cards:
                    if c.column == "backlog" and c.agent_id is None:
                        self.board_store.assign_card(b.id, c.id, a.id)
                        self.agent_store.assign_task(a.id, c.id)
                        self.state.history.append(f"card '{c.title}' assigned to {a.name}")
                        break
            self.state.history.append(f"agent '{name}' assigned goal: {goal[:40]}")
            return
        if sub == "status":
            agents = self.agent_store.list_all()
            if not agents:
                self.state.history.append("No agents.")
                return
            for a in agents:
                self.state.history.append(f"{a.name} ({a.status.value}) goal: {a.goal[:40]}")
            return
        if sub == "forget" and rest:
            a = self.agent_store.get_by_name(rest)
            if a is None:
                self.state.history.append(f"agent '{rest}' not found")
                return
            self.agent_store.delete(a.id)
            self.state.history.append(f"agent '{a.name}' removed")
            return
        if sub == "list":
            agents = self.agent_store.list_all()
            if not agents:
                self.state.history.append("No agents.")
                return
            for a in agents:
                mem_note = f" ({a.memory_entries} mem)" if a.memory_entries else ""
                self.state.history.append(f"  {a.name} [{a.status.value}]{mem_note} goal: {a.goal[:40]}")
            return
        self.state.history.append(f"unknown agent subcommand: {sub}")

    async def _board_command(self, text: str) -> None:
        parts = text.split()
        if len(parts) < 2:
            self.state.history.append("usage: board create <title> | add <title> <card> | move <id> <col> | assign <id> <agent> | list")
            return
        sub = parts[1]
        rest = " ".join(parts[2:]) if len(parts) > 2 else ""
        if sub == "create" and rest:
            b = self.board_store.create(rest)
            self.state.history.append(f"board created: '{b.title}' (id={b.id[:8]})")
            return
        if sub == "add" and len(parts) >= 4:
            board_title = parts[2]
            card_title = " ".join(parts[3:])
            b = self.board_store.get_by_title(board_title)
            if b is None:
                self.state.history.append(f"board '{board_title}' not found")
                return
            card = self.board_store.add_card(b.id, card_title)
            self.state.history.append(f"card added: '{card_title}' to '{board_title}'")
            return
        if sub == "move" and len(parts) >= 4:
            card_id = parts[2]
            to_col = parts[3]
            for b in self.board_store.list_all():
                card = b.get_card(card_id)
                if card:
                    self.board_store.move_card(b.id, card_id, to_col)
                    self.state.history.append(f"card '{card_id}' moved to '{to_col}'")
                    return
            self.state.history.append(f"card '{card_id}' not found")
            return
        if sub == "assign" and len(parts) >= 4:
            card_id = parts[2]
            agent_name = parts[3]
            agent = self.agent_store.get_by_name(agent_name)
            if agent is None:
                self.state.history.append(f"agent '{agent_name}' not found")
                return
            for b in self.board_store.list_all():
                card = b.get_card(card_id)
                if card:
                    self.board_store.assign_card(b.id, card_id, agent.id)
                    self.agent_store.assign_task(agent.id, card_id)
                    self.state.history.append(f"card '{card_id}' assigned to {agent_name}")
                    return
            self.state.history.append(f"card '{card_id}' not found")
            return
        if sub == "list":
            boards = self.board_store.list_all()
            if not boards:
                self.state.history.append("No boards.")
                return
            for b in boards:
                counts = {col: len(b.cards_in_column(col)) for col in b.columns}
                self.state.history.append(f"  {b.title} ({b.cards} cards: {counts})")
            return
        if sub == "cards" and rest:
            b = self.board_store.get_by_title(rest)
            if b is None:
                self.state.history.append(f"board '{rest}' not found")
                return
            for col in b.columns:
                cards = b.cards_in_column(col)
                if cards:
                    self.state.history.append(f"  [{col}]")
                    for c in cards:
                        agent_tag = f" @{c.agent_id[:8]}" if c.agent_id else ""
                        self.state.history.append(f"    {c.id} {c.title}{agent_tag}")
            return
        self.state.history.append(f"unknown board subcommand: {sub}")

    async def _chat(self, message: str) -> None:
        """Send a message to the inline LLM agent (tool-calling loop)."""
        import asyncio
        from .agent import ToolEnv
        from .llm import agent_run
        from .core import Intent, IntentType

        # capture the running loop so agent tools (called from the executor
        # thread inside agent_run) can safely publish intents back
        self._loop = asyncio.get_event_loop()

        # Build the agent's tool environment from the live cockpit.
        # Tool callables emit Intents on the bus (handled by the store/app).
        env = ToolEnv(
            run=lambda h, p: self._agent_run_tool(h, p),
            rerun=lambda: self._agent_rerun_tool(),
            compare=lambda q: self._agent_compare_tool(q),
            mem=lambda q: self._agent_mem_tool(q),
            note=lambda f: self._agent_note_tool(f),
            think=lambda q: self._agent_think_tool(q),
            state=lambda: self._agent_state_tool(),
            vault=lambda p, c: self._agent_vault_tool(p, c),
            swarm=lambda g: self._agent_swarm_tool(g),
            hermes=lambda t, b: self._agent_hermes_tool(t, b),
        )
        loop = asyncio.get_event_loop()
        reply = await loop.run_in_executor(None, agent_run, self.chat, env)
        await self.bus.publish("chat", {"role": "assistant", "content": reply})

    # ---- agent tool implementations (the cockpit's own capabilities) -----
    # These run from the executor thread (inside agent_run), so they must NOT
    # await. They schedule Intent publishes on the captured loop (self._loop).
    def _emit(self, intent) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(
                lambda i=intent: asyncio.ensure_future(self.bus.publish(TOPIC_INTENT, i)))

    def _agent_run_tool(self, harness: str, prompt: str) -> str:
        hid = harness.strip()
        if hid not in self.harnesses:
            return f"unknown harness '{hid}' (have: {', '.join(self.harnesses)})"
        self._emit(Intent.command(f"run {hid} {prompt}"))
        return f"scheduled {hid}: {prompt[:40]}"

    def _agent_rerun_tool(self) -> str:
        failed = [t for t in self.registry.tasks.values()
                  if t.state.value == "failed"]
        if not failed:
            return "no failed tasks to rerun"
        t = failed[-1]
        prompt = self._task_prompts.get(t.id, t.label)
        self._emit(Intent.command(f"run {t.harness} {prompt}"))
        return f"rerunning {t.id} ({t.harness})"

    def _agent_compare_tool(self, prompt: str) -> str:
        self._emit(Intent.compare(prompt))
        return f"comparing models for: {prompt[:60]}"

    def _agent_mem_tool(self, query: str) -> str:
        hits = self.memory.search(query, limit=3)
        if not hits:
            return f"no memory hits for '{query}'"
        return "\n".join(f"- {h.get('text','')[:80]}" for h in hits)

    def _agent_note_tool(self, fact: str) -> str:
        result = self.memory.add(fact)
        src = result.get("source", "memory")
        return f"noted ({src}): {fact[:60]}"

    def _agent_think_tool(self, question: str) -> str:
        result = self.memory.think(question)
        if result and result.get("answer"):
            answer = result["answer"][:200]
            gap = result.get("gap_analysis", "")
            return f"{answer}\n{gap}"
        return "think: gbrain not available"

    async def _provider_command(self, text: str) -> None:
        """Handle 'provider list|add|rm|use|presets' commands."""
        parts = text.split()
        sub = parts[1] if len(parts) > 1 else "list"
        if sub == "list":
            profiles = self.credentials.list()
            if not profiles:
                self.state.logs.append("No providers configured. 'provider presets' to see available.")
            else:
                for p in profiles:
                    mark = "★" if p.get("active") else " "
                    self.state.logs.append(
                        f"{mark} {p.get('label','?')} ({p.get('kind','?')}) "
                        f"→ {p.get('endpoint','')[:40]} "
                        f"key: {p.get('key_preview','')}")
            self.state.logs = self.state.logs[-50:]
            return
        if sub == "presets":
            for p in self.credentials.preset_list():
                note = " ✓" if p.get("configured") else ""
                self.state.logs.append(
                    f"  {p.get('icon','')} {p.get('name','?')} "
                    f"({p['kind']}){note}")
            self.state.logs.append("Use 'provider add <kind> <key>' to configure.")
            self.state.logs = self.state.logs[-50:]
            return
        if sub == "add" and len(parts) >= 4:
            kind = parts[2].lower()
            api_key = parts[3]
            endpoint = parts[4] if len(parts) >= 5 else ""
            from .credentials import PROVIDER_ALIASES
            resolved = PROVIDER_ALIASES.get(kind)
            if not resolved:
                self.state.logs.append(f"unknown provider '{kind}'. Try 'provider presets'")
                self.state.logs = self.state.logs[-50:]
                return
            p = self.credentials.add(resolved, api_key, endpoint=endpoint)
            self.state.logs.append(f"provider {p.label} ({resolved}) configured")
            self.state.logs = self.state.logs[-50:]
            return
        if sub == "rm" and len(parts) >= 3:
            kind = parts[2].lower()
            from .credentials import PROVIDER_ALIASES
            resolved = PROVIDER_ALIASES.get(kind)
            if resolved and self.credentials.remove(resolved):
                self.state.logs.append(f"provider {resolved} removed")
            else:
                self.state.logs.append(f"provider '{kind}' not found")
            self.state.logs = self.state.logs[-50:]
            return
        if sub == "use" and len(parts) >= 3:
            kind = parts[2].lower()
            from .credentials import PROVIDER_ALIASES
            resolved = PROVIDER_ALIASES.get(kind)
            if resolved:
                self.credentials.set_active(resolved)
                self.state.logs.append(f"active provider: {resolved}")
            else:
                self.state.logs.append(f"unknown provider '{kind}'")
            self.state.logs = self.state.logs[-50:]
            return
        self.state.logs.append(
            "usage: provider list | presets | add <kind> <key> [endpoint] | rm <kind> | use <kind>")
        self.state.logs = self.state.logs[-50:]

    def _agent_state_tool(self) -> str:
        tasks = list(self.registry.tasks.values())
        running = sum(1 for t in tasks if t.state.value == "running")
        failed = sum(1 for t in tasks if t.state.value == "failed")
        sugg = len(self.state.suggestions)
        st = self.state.stats.get("stats")
        tok = st.get("in", 0) + st.get("out", 0) if st else 0
        return (f"tasks={len(tasks)} running={running} failed={failed} "
                f"suggestions={sugg} tokens={tok} "
                f"ws={self.cfg['workspaces'][self.state.active_ws]['id']}")

    def _agent_vault_tool(self, path: str, content: str) -> str:
        try:
            from .notes import write_note
            written = write_note(path, content)
            return f"wrote {written}"
        except Exception as e:  # noqa: BLE001
            return f"vault error: {e}"

    def _agent_swarm_tool(self, goal: str) -> str:
        try:
            orch = self.swarm
            orch.decompose(goal)
            orch.add_agent("Agent A", f"work on: {goal}")
            self.state.swarm_dashboard = orch.dashboard()
            return f"swarm planned for: {goal[:60]}"
        except Exception as e:  # noqa: BLE001
            return f"swarm error: {e}"

    def _agent_hermes_tool(self, title: str, body: str) -> str:
        # Create a task on the Hermes kanban board (delegates real work to
        # another Hermes profile / agent). Best-effort: requires `hermes` CLI.
        try:
            import asyncio
            from .hermes.client import HermesClient
            client = HermesClient()
            # schedule the CLI call on the loop (this runs in executor thread)
            async def _create():
                summary = (body or title)[:400]
                return await client.run("kanban", "create",
                                        "--title", title,
                                        "--body", summary)
            fut = asyncio.get_event_loop().run_in_executor(
                None, lambda: asyncio.run(_create()))
            result = fut.result(timeout=30)
            return f"hermes task created: {result[:80]}"
        except FileNotFoundError:
            return "hermes CLI not found (is `hermes` installed?)"
        except Exception as e:  # noqa: BLE001
            return f"hermes error: {e}"

    # ---- bus subscriptions: keep state + persist in sync ----------------
    async def _on_task_event(self, msg: dict) -> None:
        self.store.save(self.registry.tasks)
        if msg.get("task") is None:
            return
        task = msg["task"]
        tid = task.id
        cur = task.state
        prev = self._prev_task_states.get(tid)
        # Advance the swarm. This is the whole reason a DAG can now finish:
        # an agent's task reaching a terminal state completes the agent, which
        # unblocks its dependents, which the next pump starts. Guarded so a
        # swarm problem cannot take down ordinary task bookkeeping.
        if getattr(self, "_swarm_runner", None) is not None and cur != prev:
            try:
                self._swarm_runner.on_task_state(
                    tid, cur.value,
                    output="\n".join(task.log[-20:]),
                    error=(task.log[-1] if task.log else ""))
            except Exception as e:  # noqa: BLE001
                self.state.logs.append(f"swarm: {type(e).__name__}: {str(e)[:120]}")
        if prev is not None and cur != prev:
            persona = Persona()
            resp = persona.respond(cur.value, task_id=tid, label=task.label)
            await self.bus.publish(TOPIC_VOICE, {"text": resp, "event": cur.value})
            # Track completed/failed tasks
            if cur.value in ("done", "failed", "cancelled"):
                self.state.task_history.append({
                    "id": tid, "label": task.label, "harness": task.harness,
                    "result": cur.value, "progress": task.progress,
                })
                self.state.task_history = self.state.task_history[-50:]
        self._prev_task_states[tid] = cur

    async def _on_stats(self, msg: dict) -> None:
        self.state.stats[msg["harness"]] = msg["metrics"]
        if msg["harness"] == "swarm":
            # Mark swarm as active so _current_items picks it up
            self.state.swarm_dashboard = msg["metrics"]

    async def _on_log(self, msg: dict) -> None:
        self.state.logs.append(f"[{msg['task_id']}] {msg['line']}")
        self.state.logs = self.state.logs[-200:]

    async def _on_mode(self, msg: dict) -> None:
        mode = msg.get("mode")
        if mode == "voice":
            self.state.voice_active = msg["active"]
        elif mode == "deck_app":
            self.state.deck_app = msg["active"]
        elif mode == "ring":
            # Colmi R02 telemetry (battery/heart_rate/spo2) trickles in one
            # field per notification — merge, don't overwrite the others.
            ring = self.state.stats.setdefault("ring", {})
            for k, v in msg.items():
                if k != "mode":
                    ring[k] = v

    async def _on_physis(self, msg: dict) -> None:
        if msg.get("action") == "snapshot":
            self.state.stats["physis"] = {
                "degraded": msg.get("degraded", False),
                "kind": msg.get("kind", "?"),
                "semantic": msg.get("semantic", False),
                "graph": msg.get("graph", {}),
            }
        elif msg.get("action") == "classify":
            # surface per-task physis labels (shown in Tasks workspace)
            t = self.registry.tasks.get(msg.get("task", ""))
            if t is not None and msg.get("label"):
                t.domain = msg["label"]
        elif msg.get("action") == "error":
            self.state.stats["physis"] = {
                "degraded": True, "kind": "offline",
                "semantic": False, "error": msg.get("detail", ""),
            }
    async def _on_hermes(self, msg: dict) -> None:
        action = msg.get("action", "")
        data = msg.get("data", {})
        if action == "kanban":
            self.state.hermes_kanban = data.get("tasks", [])
        elif action == "memory":
            self.state.hermes_memory = data.get("sections", [])
        elif action == "gateway":
            self.state.hermes_gateway = data

    async def _on_skill(self, msg: dict) -> None:
        if msg.get("action") == "list":
            self.state.skills = msg.get("data", [])

    async def _on_settings(self, msg: dict) -> None:
        if msg.get("action") == "providers":
            self.state.settings_providers.update(msg.get("data", {}))
