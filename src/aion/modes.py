"""
modes.py — aion's operational modes.

Each mode changes the persona, polling frequency, active workspaces,
and UI density. Like Iron Man's "Suit Modes" (Stealth, Combat, Research)
or Odysseus' different agent profiles.

Modes:
  default  — balanced HUD, all workspaces, normal polling
  focus    — minimal UI, fewer workspaces, reduced polling (save CPU)
  deep     — deep-work mode: taps web search + LLM heavy lifting
  monitor  — system-monitor mode: polls fast, shows sys/stats panels
  stealth  — dimmed UI, minimal output, hides sensitive data in public
  demo     — showcase mode: runs demo tasks, loops through workspaces
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any


class Mode(Enum):
    DEFAULT = "default"
    FOCUS = "focus"
    DEEP = "deep"
    MONITOR = "monitor"
    STEALTH = "stealth"
    DEMO = "demo"


MODE_NAMES = {m.value: m for m in Mode}


@dataclass
class ModeConfig:
    """Defines what changes when a mode is active."""
    id: str
    label: str
    icon: str
    description: str
    # Polling intervals (seconds) — None = use default
    stats_interval: float | None = None
    system_interval: float | None = None
    health_interval: float | None = None
    # Which workspaces are visible
    visible_workspaces: list[str] | None = None  # None = all
    # UI density
    show_right_rail: bool = True
    show_header_hud: bool = True
    # Behaviour
    auto_deep_search: bool = False      # route unknown commands to web search
    suppress_voice: bool = False
    dim_theme: bool = False
    # Persona override
    persona_tone: str | None = None     # "terse" | "normal" | "chatty"

    def as_dict(self) -> dict:
        return {
            "id": self.id, "label": self.label, "icon": self.icon,
            "description": self.description,
            "show_right_rail": self.show_right_rail,
            "show_header_hud": self.show_header_hud,
            "auto_deep_search": self.auto_deep_search,
            "suppress_voice": self.suppress_voice,
            "dim": self.dim_theme,
            "persona_tone": self.persona_tone,
        }


# Built-in modes
MODES: dict[str, ModeConfig] = {
    "default": ModeConfig(
        id="default", label="Default", icon="◉",
        description="Balanced HUD with all workspaces and normal polling.",
    ),
    "focus": ModeConfig(
        id="focus", label="Focus", icon="◎",
        description="Minimal UI — fewer panels, reduced polling, save CPU.",
        stats_interval=8.0,
        system_interval=10.0,
        health_interval=60.0,
        visible_workspaces=["models", "tasks", "agent"],
        show_right_rail=False,
        persona_tone="terse",
    ),
    "deep": ModeConfig(
        id="deep", label="Deep Work", icon="⬡",
        description="Deep-research mode: Web + LLM agents prioritized.",
        stats_interval=5.0,
        system_interval=4.0,
        health_interval=60.0,
        auto_deep_search=True,
        persona_tone="normal",
    ),
    "monitor": ModeConfig(
        id="monitor", label="Monitor", icon="◈",
        description="System monitor mode: fast polling, sys/stats panels front.",
        stats_interval=2.0,
        system_interval=1.5,
        health_interval=30.0,
        visible_workspaces=["sys", "tasks", "models", "term"],
        persona_tone="terse",
    ),
    "stealth": ModeConfig(
        id="stealth", label="Stealth", icon="◌",
        description="Dimmed UI, minimal output, hides sensitive data.",
        dim_theme=True,
        suppress_voice=True,
        show_header_hud=False,
        show_right_rail=False,
        visible_workspaces=["tasks", "agent"],
        persona_tone="terse",
    ),
    "demo": ModeConfig(
        id="demo", label="Demo", icon="◆",
        description="Showcase mode: auto-runs demo tasks, cycles workspaces.",
        stats_interval=3.0,
        system_interval=5.0,
        health_interval=120.0,
        auto_deep_search=False,
        show_right_rail=True,
        persona_tone="chatty",
    ),
}


def get_mode(mode_id: str) -> ModeConfig | None:
    return MODES.get(mode_id)


def list_modes() -> list[ModeConfig]:
    return list(MODES.values())


def mode_command(text: str) -> str | None:
    """Parse 'mode <name>' from a command. Returns mode id or None."""
    parts = text.strip().lower().split()
    if len(parts) == 2 and parts[0] in ("mode", "m") and parts[1] in MODES:
        return parts[1]
    return None
