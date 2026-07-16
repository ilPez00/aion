"""Tests for the FCM -> Groq -> OpenRouter fallback chain (Cycle B).

Mocks the three backend senders so we verify routing + warning handling
without needing live credentials (all three are currently down from this host).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aion import llm
from aion.llm import ChatSession


def test_fallback_uses_first_ok_backend(monkeypatch):
    # FCM dead, Groq ok -> Groq wins
    monkeypatch.setattr(llm, "_fcm_chat", lambda m, timeout=30: "⚠️ FCM down")
    monkeypatch.setattr(llm, "_groq_chat", lambda m, timeout=30: "hello from groq")
    monkeypatch.setattr(llm, "_openrouter_chat", lambda m, timeout=30: "never reached")
    s = ChatSession()
    out = llm.chat_send(s, "hi")
    assert out == "hello from groq"
    assert s.messages[-1].content == "hello from groq"


def test_fallback_skips_to_openrouter_when_fcm_groq_down(monkeypatch):
    monkeypatch.setattr(llm, "_fcm_chat", lambda m, timeout=30: "⚠️ FCM down")
    monkeypatch.setattr(llm, "_groq_chat", lambda m, timeout=30: "⚠️ Groq 403")
    monkeypatch.setattr(llm, "_openrouter_chat", lambda m, timeout=30: "ora says hi")
    out = llm.chat_send(ChatSession(), "hi")
    assert out == "ora says hi"


def test_fallback_reports_all_down(monkeypatch):
    monkeypatch.setattr(llm, "_fcm_chat", lambda m, timeout=30: "⚠️ FCM down")
    monkeypatch.setattr(llm, "_groq_chat", lambda m, timeout=30: "⚠️ Groq 403")
    monkeypatch.setattr(llm, "_openrouter_chat", lambda m, timeout=30: "⚠️ HTTP 401")
    out = llm.chat_send(ChatSession(), "hi")
    assert out.startswith("⚠️ LLM unavailable (tried FCM, Groq, OpenRouter)")


def test_chat_send_multi_supports_openrouter(monkeypatch):
    monkeypatch.setattr(llm, "_fcm_chat", lambda m, timeout=30: "⚠️ FCM down")
    monkeypatch.setattr(llm, "_groq_chat", lambda m, timeout=30: "groq reply")
    monkeypatch.setattr(llm, "_openrouter_chat", lambda m, timeout=30: "ora reply")
    out = llm.chat_send_multi("q", ["fcm", "groq", "openrouter"])
    assert out["groq"].startswith("groq reply")
    assert out["openrouter"].startswith("ora reply")
    assert out["fcm"].startswith("⚠️")


def test_is_ok_rejects_warnings():
    assert llm._is_ok("real text") is True
    assert llm._is_ok("⚠️ down") is False
    assert llm._is_ok(None) is False
    assert llm._is_ok("") is False
