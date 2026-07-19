"""Context router — infers what the user is focused on and routes harnesses/data accordingly."""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ContextDomain(str, Enum):
    DESKTOP = "desktop"
    SYSTEM = "system"
    DEV = "dev"
    AGENT = "agent"
    HEALTH = "health"
    TASKS = "tasks"
    VAULT = "vault"


DOMAIN_LABELS: dict[str, str] = {
    "desktop": "HUB",
    "system": "SYSTEM",
    "dev": "DEV",
    "agent": "AGENT",
    "health": "LIFE",
    "tasks": "TASKS",
    "vault": "VAULT",
}

DOMAIN_ICONS: dict[str, str] = {
    "desktop": "⬡",
    "system": "🖥",
    "dev": "⚡",
    "agent": "✦",
    "health": "❤",
    "tasks": "▤",
    "vault": "📓",
}


@dataclass
class AppContext:
    domain: ContextDomain = ContextDomain.DESKTOP
    workspace: str = "desktop"
    detail: str = ""

    @property
    def label(self) -> str:
        return DOMAIN_LABELS.get(self.domain.value, self.domain.value.upper())

    @property
    def icon(self) -> str:
        return DOMAIN_ICONS.get(self.domain.value, "?")


class ContextRouter:
    """Infers current context from workspace and recent activity."""

    WS_DOMAIN: dict[str, ContextDomain] = {
        "desktop": ContextDomain.DESKTOP,
        "models": ContextDomain.SYSTEM,
        "tasks": ContextDomain.TASKS,
        "agent": ContextDomain.AGENT,
        "vault": ContextDomain.VAULT,
        "system": ContextDomain.SYSTEM,
        "term": ContextDomain.DEV,
        "settings": ContextDomain.DESKTOP,
    }

    HARNESS_DOMAIN: dict[str, str] = {
        "demo": "system",
        "shell": "dev",
        "cyclops": "agent",
        "remote": "system",
        "telemetry": "system",
        "stats": "desktop",
        "app": "desktop",
        "hermes": "agent",
        "skill": "agent",
        "projects": "dev",
        "term": "dev",
        "web": "dev",
        "opencode": "dev",
        "system": "system",
        "health": "health",
        "vault": "vault",
        "physis": "system",
        "agent_entity": "agent",
        "board": "tasks",
    }

    def resolve(self, store) -> AppContext:
        ws_id = store.cfg["workspaces"][store.state.active_ws]["id"]
        domain = self.WS_DOMAIN.get(ws_id, ContextDomain.DESKTOP)

        recent = getattr(store.state, "last_command", "")
        if recent:
            cmd = recent.lower().split()[0] if recent else ""
            cmd_map = {
                "agent": ContextDomain.AGENT,
                "swarm": ContextDomain.AGENT,
                "create": ContextDomain.AGENT,
                "board": ContextDomain.TASKS,
                "todo": ContextDomain.TASKS,
                "task": ContextDomain.TASKS,
                "health": ContextDomain.HEALTH,
                "mode": ContextDomain.DESKTOP,
                "search": ContextDomain.VAULT,
                "web": ContextDomain.DEV,
                "project": ContextDomain.DEV,
                "setup": ContextDomain.HEALTH,
                "observe": ContextDomain.SYSTEM,
                "tier": ContextDomain.SYSTEM,
            }
            cmd_domain = cmd_map.get(cmd)
            if cmd_domain:
                domain = cmd_domain

        return AppContext(domain=domain, workspace=ws_id)

    def active_harness_ids(self, store) -> list[str]:
        ctx = self.resolve(store)
        domain_val = ctx.domain.value
        active = store.state.active_harness
        harnesses = store.harnesses
        result = []
        for hid in harnesses:
            h_domain = self.HARNESS_DOMAIN.get(hid, "system")
            if h_domain == domain_val or hid == active:
                result.append(hid)
        return result

    def desktop_sections_for(self, store) -> list[str]:
        ctx = self.resolve(store)
        domain_val = ctx.domain.value
        base = ["STATUS", "ACTIVITY", "QUICK"]
        extras = {
            "system": ["SYSTEM"],
            "dev": ["PROJECTS", "SESSIONS"],
            "agent": ["AGENTS"],
            "health": ["DATA"],
            "tasks": ["SESSIONS"],
            "vault": ["DATA"],
            "desktop": ["LAUNCHER", "TODO"],
        }
        return base + extras.get(domain_val, [])

    def agent_mode_for(self, store) -> str | None:
        ag = store.state.stats.get("agent_entity")
        s = store.state.swarm_dashboard
        cr = store.state.compare_result

        recent = getattr(store.state, "last_command", "")
        cmd = recent.lower().split()[0] if recent else ""

        if cmd in ("swarm",) and s:
            return "swarm"
        if cmd in ("compare",) and cr and cr.get("answers"):
            return "compare"
        if cmd in ("agent", "create") and ag and ag.get("ok"):
            return "agents"

        if cr and cr.get("answers"):
            return "compare"
        if s:
            return "swarm"
        if ag and ag.get("ok"):
            return "agents"
        return None
