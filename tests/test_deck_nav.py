"""Navigation alignment test: every workspace responds to deck-style Intents.

Deck navigation model (from DECK.md):
  Joy2 up/down   -> Intent.NAVIGATE("up"/"down")
  Joy2 left/right -> Intent.SWITCH_WORKSPACE(delta=+-1)
  A (Enter)       -> Intent.ACTIVATE()
  B (Escape)      -> Intent.BACK()

Every workspace must:
  1. Return focusable items from _current_items()
  2. Navigate up/down without crash
  3. Follow the same Intent contracts
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from aion.core import (
    Bus, Intent, IntentType, TaskRegistry, TOPIC_INTENT, load_config,
)
from aion.store import Store
from aion.harnesses import build_harnesses


async def _nav_workspaces():
    cfg = load_config()
    bus = Bus()
    registry = TaskRegistry(bus)
    harnesses = build_harnesses(cfg["harnesses"], bus, registry)
    store = Store(cfg, bus, harnesses=harnesses)
    ws_ids = [w["id"] for w in store.cfg["workspaces"]]
    for ws_name in ["desktop", "models", "tasks", "agent", "vault",
                    "system", "term", "settings"]:
        assert ws_name in ws_ids, f"workspace '{ws_name}' not in config"
        idx = ws_ids.index(ws_name)
        store.handle(Intent(IntentType.SWITCH_WORKSPACE, {"index": idx}))
        assert store.state.active_ws == idx
        items = store._current_items()
        if items:
            for _ in range(min(len(items), 5)):
                store.handle(Intent(IntentType.NAVIGATE, {"dir": "down"}))
            for _ in range(min(len(items), 5)):
                store.handle(Intent(IntentType.NAVIGATE, {"dir": "up"}))
        store.handle(Intent(IntentType.ACTIVATE))
        store.handle(Intent(IntentType.BACK))


def test_all_workspaces_respond_to_nav():
    """Navigate through every workspace with deck-style Intents -- no crash."""
    asyncio.run(_nav_workspaces())


def test_workspace_switch_via_delta():
    """Left/right switch workspaces -- deck Joy2 left/right mapping."""
    cfg = load_config()
    bus = Bus()
    registry = TaskRegistry(bus)
    harnesses = build_harnesses(cfg["harnesses"], bus, registry)
    store = Store(cfg, bus, harnesses=harnesses)
    total = len(store.cfg["workspaces"])
    start = store.state.active_ws
    store.handle(Intent(IntentType.SWITCH_WORKSPACE, {"delta": 1}))
    assert store.state.active_ws == (start + 1) % total
    store.handle(Intent(IntentType.SWITCH_WORKSPACE, {"delta": -1}))
    assert store.state.active_ws == start


def test_deck_keyboard_parity():
    """Keyboard map and deck emit same Intent types."""
    from aion.input import KeyboardMap
    cfg = load_config()
    keymap = KeyboardMap(cfg["keybindings"])
    deck_up = Intent.navigate("up")
    kb_up = keymap.resolve("up")
    assert kb_up is not None and kb_up.type == deck_up.type
    deck_a = Intent.activate()
    kb_enter = keymap.resolve("enter")
    assert kb_enter is not None and kb_enter.type == deck_a.type
    deck_b = Intent.back()
    kb_esc = keymap.resolve("escape")
    assert kb_esc is not None and kb_esc.type == deck_b.type
