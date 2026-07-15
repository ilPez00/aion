"""Tests for llm.py multi-model comparison helper."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aion import llm


def test_chat_send_multi_calls_providers(monkeypatch):
    # provider-keyed backends
    monkeypatch.setattr(llm, "_fcm_chat", lambda msgs, timeout=30: "REPLY_FCM")
    monkeypatch.setattr(llm, "_groq_chat", lambda msgs, timeout=30: "REPLY_GROQ")
    out = llm.chat_send_multi("hi", ["fcm", "groq"])
    assert out == {"fcm": "REPLY_FCM", "groq": "REPLY_GROQ"}


def test_chat_send_multi_marks_missing(monkeypatch):
    monkeypatch.setattr(llm, "_fcm_chat", lambda msgs, timeout=30: None)
    monkeypatch.setattr(llm, "_groq_chat", lambda msgs, timeout=30: "OK")
    out = llm.chat_send_multi("q", ["fcm", "groq"])
    assert out["fcm"].startswith("⚠")
    assert out["groq"] == "OK"


def test_chat_send_multi_caps_length(monkeypatch):
    monkeypatch.setattr(llm, "_fcm_chat", lambda msgs, timeout=30: "X" * 999)
    out = llm.chat_send_multi("q", ["fcm"])
    assert len(out["fcm"]) <= 420
