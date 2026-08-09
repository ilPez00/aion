"""Tests for the Groq -> OpenRouter -> OmniRoute -> FCM fallback chain.

Mocks the backend senders so routing and warning handling are verified with
no live credentials and no network.

The chain has FOUR backends and this file used to mock three. OmniRoute was
tried FIRST, so on any machine where it answered, these tests took its reply
instead of the mocked fallbacks -- and made a real LLM call while doing it.
They passed here only because OmniRoute happens to be down on this host, and
failed the moment the suite ran on another machine in the fleet:

    assert '<tool state></tool>'.startswith('⚠️ LLM unavailable ...')

A test whose result depends on whether a network service is up is not testing
what it says it tests. `_no_network` is autouse so a test added later cannot
reintroduce the hole by forgetting one backend. Local AI (OmniRoute/FCM) is
now LAST in the chain and never auto-triggered by a slow-model timeout.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aion import llm
from aion.llm import ChatSession


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Every backend stubbed as down. Individual tests override what they mean
    to exercise; nothing here is left able to reach a real provider."""
    for fn in ("_omniroute_chat", "_fcm_chat", "_groq_chat", "_openrouter_chat"):
        monkeypatch.setattr(llm, fn,
                            lambda m, timeout=30, _f=fn: f"⚠️ {_f} stubbed down")


def test_fallback_uses_first_ok_backend(monkeypatch):
    # FCM dead, Groq ok -> Groq wins
    monkeypatch.setattr(llm, "_fcm_chat", lambda m, timeout=30: "⚠️ FCM down")
    monkeypatch.setattr(llm, "_groq_chat", lambda m, timeout=30: "hello from groq")
    monkeypatch.setattr(llm, "_openrouter_chat", lambda m, timeout=30: "never reached")
    monkeypatch.setattr(llm, "_omniroute_chat", lambda m, timeout=30: "never reached")
    s = ChatSession()
    out = llm.chat_send(s, "hi")
    assert out == "hello from groq"
    assert s.messages[-1].content == "hello from groq"


def test_fallback_skips_to_openrouter_when_groq_down(monkeypatch):
    monkeypatch.setattr(llm, "_groq_chat", lambda m, timeout=30: "⚠️ Groq 403")
    monkeypatch.setattr(llm, "_openrouter_chat", lambda m, timeout=30: "ora says hi")
    out = llm.chat_send(ChatSession(), "hi")
    assert out == "ora says hi"


def test_local_omniroute_used_only_after_cloud_fails(monkeypatch):
    # Cloud dead -> OmniRoute (local) answers, and is tried AFTER both cloud.
    calls: list[str] = []
    monkeypatch.setattr(llm, "_groq_chat", lambda m, timeout=30: calls.append("groq") or "⚠️ Groq 403")
    monkeypatch.setattr(llm, "_openrouter_chat", lambda m, timeout=30: calls.append("or") or "⚠️ HTTP 401")
    monkeypatch.setattr(llm, "_omniroute_chat", lambda m, timeout=30: calls.append("omni") or "omni says hi")
    monkeypatch.setattr(llm, "_fcm_chat", lambda m, timeout=30: calls.append("fcm") or "never reached")
    out = llm.chat_send(ChatSession(), "hi")
    assert out == "omni says hi"
    assert calls == ["groq", "or", "omni"]


def test_timeout_does_not_fall_through_to_local(monkeypatch):
    # Groq is slow (timeout sentinel) -> we must NOT try local AI.
    monkeypatch.setattr(llm, "_groq_chat", lambda m, timeout=30: "⏱️ Groq timed out (model too slow)")
    monkeypatch.setattr(llm, "_openrouter_chat", lambda m, timeout=30: "never reached")
    monkeypatch.setattr(llm, "_omniroute_chat", lambda m, timeout=30: "never reached")
    monkeypatch.setattr(llm, "_fcm_chat", lambda m, timeout=30: "never reached")
    out = llm.chat_send(ChatSession(), "hi")
    assert out.startswith("⏱️ model too slow (Groq)")
    assert "never reached" not in out


def test_fallback_reports_all_down(monkeypatch):
    monkeypatch.setattr(llm, "_groq_chat", lambda m, timeout=30: "⚠️ Groq 403")
    monkeypatch.setattr(llm, "_openrouter_chat", lambda m, timeout=30: "⚠️ HTTP 401")
    monkeypatch.setattr(llm, "_omniroute_chat", lambda m, timeout=30: "⚠️ OmniRoute down")
    monkeypatch.setattr(llm, "_fcm_chat", lambda m, timeout=30: "⚠️ FCM down")
    out = llm.chat_send(ChatSession(), "hi")
    assert out.startswith("⚠️ LLM unavailable (tried Groq, OpenRouter, OmniRoute, FCM)")


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
    assert llm._is_ok("⏱️ timed out") is False
    assert llm._is_ok(None) is False
    assert llm._is_ok("") is False
