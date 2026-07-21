"""Tests for voice parse mapping (Cycle 9) + ACT intent handling."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aion.core import Intent, IntentType
from aion.input import VoiceInput


def test_voice_parse_rerun():
    v = VoiceInput()
    assert IntentType.RERUN == v.parse("rerun").type


def test_voice_parse_compare():
    v = VoiceInput()
    i = v.parse("compare which model is better")
    assert i.type == IntentType.COMPARE
    assert i.payload["text"] == "which model is better"


def test_voice_parse_act():
    v = VoiceInput()
    assert v.parse("act").type == IntentType.ACT
    assert v.parse("do it").type == IntentType.ACT


def test_voice_parse_tour():
    v = VoiceInput()
    assert v.parse("tour").payload["text"] == "tour"
    assert v.parse("walk me through").payload["text"] == "tour"


def test_voice_parse_run():
    v = VoiceInput()
    i = v.parse("run demo hello")
    assert i.type == IntentType.COMMAND
    assert i.payload["text"] == "demo hello"


# ── workspace navigation by voice ─────────────────────────────────────────────
from aion.input import build_ws_map

WORKSPACES = [
    {"id": "desktop", "title": "Desktop"},
    {"id": "models", "title": "Subsystems"},
    {"id": "tasks", "title": "Tasks"},
    {"id": "agent", "title": "Agent"},
    {"id": "vault", "title": "Vault"},
    {"id": "system", "title": "System"},
    {"id": "term", "title": "Term"},
    {"id": "settings", "title": "Settings"},
    {"id": "net", "title": "Fleet"},
]


def _voice():
    return VoiceInput(workspaces=WORKSPACES)


def test_ws_map_covers_every_workspace_by_id():
    m = build_ws_map(WORKSPACES)
    for i, w in enumerate(WORKSPACES):
        assert m[w["id"]] == i


def test_fleet_is_reachable_by_voice():
    """The whole reason for this change: the new workspace answers to voice."""
    v = _voice()
    for phrase in ("go to fleet", "show fleet", "open network", "fleet"):
        i = v.parse(phrase)
        assert i.type == IntentType.SWITCH_WORKSPACE, phrase
        assert i.payload["index"] == 8, phrase


def test_workspace_reachable_by_title_not_only_id():
    """id is 'models' but a person says its title, 'subsystems'."""
    v = _voice()
    assert v.parse("go to subsystems").payload["index"] == 1
    assert v.parse("show models").payload["index"] == 1


def test_desktop_reachable_via_alias():
    v = _voice()
    assert v.parse("go to home").payload["index"] == 0
    assert v.parse("dashboard").payload["index"] == 0


def test_bare_and_verb_forms_both_navigate():
    v = _voice()
    assert v.parse("settings").payload["index"] == 7
    assert v.parse("switch to settings").payload["index"] == 7


def test_unknown_target_falls_through_to_command():
    v = _voice()
    i = v.parse("go to the moon")
    assert i.type == IntentType.COMMAND


def test_empty_workspaces_map_is_empty():
    assert build_ws_map([]) == {}


# ── harness verbs (research / factory by voice) ───────────────────────────────
from aion.input import match_harness_verb


@pytest.mark.parametrize("spoken,expected", [
    ("research quantum computing", "research quantum computing"),
    ("deep research rust async", "research rust async"),
    ("look into the fleet bug", "research the fleet bug"),
    ("investigate memory leak", "research memory leak"),
    ("factory build the parser", "factory build the parser"),
    ("factory loop fix tests until green", "factory fix tests until green"),
    ("iterate on the readme", "factory the readme"),
    ("keep building the api", "factory the api"),
    ("loop on lint", "factory lint"),
])
def test_harness_verb_maps_to_canonical_command(spoken, expected):
    assert match_harness_verb(spoken) == expected


def test_bare_verb_without_query_does_not_match():
    """'research' alone must not spawn an empty research task."""
    assert match_harness_verb("research") is None
    assert match_harness_verb("factory") is None


def test_unrelated_speech_does_not_match():
    assert match_harness_verb("what time is it") is None


def test_voice_parse_routes_research(monkeypatch):
    v = VoiceInput(workspaces=WORKSPACES)
    i = v.parse("Research quantum error correction")   # note casing
    assert i.type == IntentType.COMMAND
    assert i.payload["text"] == "research quantum error correction"


def test_voice_parse_routes_factory():
    v = VoiceInput(workspaces=WORKSPACES)
    i = v.parse("keep building the auth module")
    assert i.type == IntentType.COMMAND
    assert i.payload["text"] == "factory the auth module"


def test_longer_verb_phrase_wins():
    """'deep research x' -> research x, not 'research' matching 'deep...'."""
    assert match_harness_verb("deep research x") == "research x"
