"""Tests for voice parse mapping (Cycle 9) + ACT intent handling."""
import sys
from pathlib import Path

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
