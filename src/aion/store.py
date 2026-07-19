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
import time
from dataclasses import dataclass, field
from typing import Any

from .core import (
    Bus, Intent, IntentType, TaskRegistry, SessionStore,
    Task, TaskState, TOPIC_VOICE, TOPIC_HERMES, TOPIC_SKILL, TOPIC_SETTINGS,
    TOPIC_INTENT, TOPIC_PHYSIS, load_config,
)
from .memory import MemoryStore
from .voice.persona import Persona
from .llm import ChatSession, format_conversation, chat_send
from .swarm import SwarmOrchestrator, AgentStatus as SwarmAgentStatus
from .modes import get_mode, list_modes, mode_command, MODES, ModeConfig
from .dashboard import collect_dashboard
from .physis import get_client as _get_physis  # coherence brain (soft-fails if down)



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
        self.memory = MemoryStore()
        from .todos import TodoStore
        self.todos = TodoStore()
        self._profile_scanning = False
        self.chat = ChatSession()
        self.swarm = SwarmOrchestrator(bus=self.bus)
        self.active_mode_cfg: ModeConfig = get_mode("default") or MODES["default"]
        self.state = ViewState(active_harness=self._first_harness())
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
            from .hermes.env import parse_provider_env
            self.state.settings_providers = dict(parse_provider_env())
            await self.bus.publish(TOPIC_SETTINGS, {
                "action": "providers",
                "data": self.state.settings_providers,
            })
        except Exception:
            pass

    async def _load_skills_data(self) -> None:
        try:
            from .hermes import SkillLoader
            skills = SkillLoader().list_all()
            data = [{"name": s.name, "description": s.description, "source": s.source}
                    for s in skills]
            await self.bus.publish(TOPIC_SKILL, {"action": "list", "data": data})
        except Exception:
            pass

    def _current_items(self) -> list[dict]:
        ws = self.cfg["workspaces"][self.state.active_ws]["id"]
        if ws == "desktop":
            d = collect_dashboard(self.state, self.cfg, todos=self.todos)
            self._maybe_rescan_profile(d.profile)
            return [{"type": "dashboard", "data": d.as_dict()}]
        if ws == "models":
            return [{"id": h, "name": self.harnesses[h].name,
                     "vram": self.harnesses[h].vram_mb,
                     "tier": self.harnesses[h].tier,
                     "running": self._running_for(h)} for h in self.harnesses]
        if ws == "tasks":
            return [t.as_dict() for t in self.registry.tasks.values()]
        if ws == "memory":
            return self.memory.items()
        if ws == "hermes":
            asyncio.create_task(self._load_hermes_data())
            return [{"id": t["id"], "title": t["title"], "status": t["status"],
                     "assignee": t.get("assignee", "")}
                    for t in self.state.hermes_kanban]
        if ws == "skills":
            asyncio.create_task(self._load_skills_data())
            return [{"id": s.get("name", s.get("id", "")), "name": s.get("name", ""),
                     "description": s.get("description", ""), "source": s.get("source", "")}
                    for s in self.state.skills]
        if ws == "projects":
            pj = self.state.stats.get("projects")
            if pj and pj.get("projects"):
                return list(pj["projects"])
            return []
        if ws == "settings":
            asyncio.create_task(self._load_settings_data())
            return [{"id": k, "endpoint": v.get("endpoint", ""),
                     "key_preview": v.get("key_preview", "")}
                    for k, v in self.state.settings_providers.items()]
        if ws == "vault":
            v = self.state.stats.get("vault")
            if v and v.get("ok"):
                return list(v.get("nodes", []))
            return [{"name": "(none)", "title": "vault not loaded",
                     "preview": v.get("error", "") if v else ""}]
        if ws == "sys":
            return [{"kind": "live"}]  # rendered specially from stats
        if ws == "agent":
            cr = self.state.compare_result
            if cr and cr.get("answers"):
                # side-by-side compare takes over the agent workspace
                return [{"type": "compare", "prompt": cr.get("prompt", ""),
                         "answers": cr["answers"], "done": cr.get("done", False)}]
            msgs = format_conversation(self.chat)
            return [{"type": "chat", "messages": msgs}] if msgs else []
        if ws == "swarm":
            s = self.state.swarm_dashboard
            if s:
                return [{"type": "dashboard", "data": s}]
            return [{"type": "empty", "label": "No active swarm. Use 'swarm create <goal>' to start."}]
        if ws == "physis":
            ph = self.state.stats.get("physis")
            if ph:
                return [{"type": "physis", "degraded": ph.get("degraded", False),
                         "kind": ph.get("kind", "?"),
                         "semantic": ph.get("semantic", False),
                         "graph": ph.get("graph", {})}]
            return [{"type": "empty", "label": "physis engine offline — start physis-pro-web (:19876)."}]
        return []

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
        if ws_id != "tasks" or not items:
            return None
        if self.state.focus >= len(items):
            return None
        return self.registry.tasks.get(items[self.state.focus]["id"])

    def _control(self, action: str) -> None:
        task = self._focused_task()
        if task is None:
            return
        h = self.harnesses.get(task.harness, self.harnesses.get(self.state.active_harness))
        if h is None:
            return
        if action == "pause" and task.state.value == "running":
            h.pause(task)
        elif action == "resume" and task.state.value == "running" and task.paused:
            h.resume(task)
        elif action == "cancel":
            h.cancel(task)
        elif action == "rerun" and task.state.value in ("interrupted", "cancelled", "failed"):
            asyncio.create_task(self._respawn(task))

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
        asyncio.create_task(h.run(task, prompt))

    async def _respawn(self, old: Task) -> None:
        h = self.harnesses.get(old.harness, self.harnesses.get(self.state.active_harness))
        if h is None:
            return
        task = self.registry.create(f"{h.name}: {old.label[:24]}", h.id)
        asyncio.create_task(h.run(task, old.label))

    async def _run_command(self, text: str, _interpreted: bool = False) -> None:
        self.state.history.append(text)
        parts = text.split(" ", 1)
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
            self.state.logs.extend([
                "Just type what you want — e.g.:",
                "  'open mail' · 'edit plan.md' · 'todo buy milk' · 'done 1'",
                "  'i use this for coding and writing' · 'scan' · 'watch this'",
                "  'go to vault' · press ? for keys",
            ])
            self.state.logs = self.state.logs[-50:]
            return
        if parts[0] == "note" and len(parts) == 2:
            self.memory.add(parts[1])
            return
        if parts[0] == "mem":
            self.memory.query = parts[1] if len(parts) == 2 else ""
            ws_ids = [w["id"] for w in self.cfg["workspaces"]]
            if "memory" in ws_ids:
                self.state.active_ws = ws_ids.index("memory")
                self.state.focus = 0
            return
        if parts[0] == "forget" and len(parts) == 2 and parts[1].strip().isdigit():
            self.memory.forget(int(parts[1]))
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
        try:
            from .hermes import HermesMemoryReader
            mem = HermesMemoryReader()
            hits = mem.search(query, limit=3) if hasattr(mem, "search") else []
            if not hits:
                return f"no memory hits for '{query}'"
            return "\n".join(f"- {h.title}: {h.snippet[:80]}" for h in hits)
        except Exception as e:  # noqa: BLE001
            return f"memory unavailable: {e}"

    def _agent_note_tool(self, fact: str) -> str:
        self.memory.add(fact)
        return f"noted: {fact[:60]}"

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
        if msg.get("mode") == "voice":
            self.state.voice_active = msg["active"]
        elif msg.get("mode") == "deck_app":
            self.state.deck_app = msg["active"]

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
