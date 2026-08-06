"""Tests for llm.py multi-model comparison helper.

Same hermeticity guard as `test_llm_fallback`: the backend chain has four
senders and these tests stub two of them by name. Anything they do not stub
can reach a real provider on a machine where it is up, which is a test whose
result depends on the weather.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aion import llm


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    for fn in ("_omniroute_chat", "_fcm_chat", "_groq_chat", "_openrouter_chat"):
        monkeypatch.setattr(llm, fn,
                            lambda m, timeout=30, _f=fn: f"⚠️ {_f} stubbed down")


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
