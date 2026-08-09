"""Tests for the FCM -> Groq -> OpenRouter fallback chain.

Mocks the backend senders so routing and warning handling are verified with
no live credentials and no network.

The chain has THREE backends and this file stubs all three. OmniRoute is
no longer part of the automatic fallback chain; it is available for explicit
invocation via `compare` or direct call.

A test whose result depends on whether a network service is up is not testing
what it says it tests. `_no_network` is autouse so a test added later cannot
reintroduce the hole by forgetting one backend.
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


# ── timeouts are not outages ─────────────────────────────────────────────────
# Ported from work found uncommitted on pansa. "Down" and "slow" are different
# states and only one is a reason to ask a different backend: a backend that
# timed out may still be working on the request, so advancing duplicates it,
# and three backends at 30s each is a minute and a half of frozen chat before
# the user learns anything at all.

def test_a_timeout_stops_the_chain(monkeypatch):
    monkeypatch.setattr(llm, "_fcm_chat", lambda m, timeout=30: "⏱️ FCM timed out")
    monkeypatch.setattr(llm, "_groq_chat",
                        lambda m, timeout=30: pytest.fail("chain advanced past a timeout"))
    out = llm.chat_send(ChatSession(), "hi")
    assert out.startswith("⏱️")
    assert "FCM" in out


def test_a_timeout_names_the_backend_that_was_slow():
    """The whole actionable content: which one to retry or turn off."""
    s = ChatSession()
    llm.chat_send(s, "hi")   # every backend stubbed '⚠️' by the autouse fixture
    assert "tried FCM, Groq, OpenRouter" in s.messages[-1].content


def test_an_outage_still_advances(monkeypatch):
    """The other direction, so "stop on timeout" cannot quietly become
    "stop on anything"."""
    monkeypatch.setattr(llm, "_fcm_chat", lambda m, timeout=30: "⚠️ FCM down")
    monkeypatch.setattr(llm, "_groq_chat", lambda m, timeout=30: "groq reply")
    assert llm.chat_send(ChatSession(), "hi") == "groq reply"


def test_a_timeout_is_not_a_successful_reply():
    assert llm._is_ok("⏱️ Groq timed out") is False
    assert llm._is_ok("⚠️ Groq down") is False
    assert llm._is_ok("a real answer") is True


# ── classifying the exception ────────────────────────────────────────────────

def test_a_bare_socket_timeout_is_a_timeout():
    import socket
    assert llm._is_timeout(socket.timeout("timed out")) is True
    assert llm._is_timeout(TimeoutError()) is True


def test_a_timeout_wrapped_in_urlerror_is_still_a_timeout():
    """urllib does not raise the socket timeout, it puts it in `.reason`.
    Checking only the outer type calls every timeout an outage — which is the
    bug this function exists to avoid, so it is the test that matters most."""
    import socket
    import urllib.error
    e = urllib.error.URLError(socket.timeout("timed out"))
    assert llm._is_timeout(e) is True
    assert llm._unreachable("Groq", e).startswith("⏱️")


def test_a_refused_connection_is_not_a_timeout():
    import urllib.error
    e = urllib.error.URLError(ConnectionRefusedError("refused"))
    assert llm._is_timeout(e) is False
    assert llm._unreachable("Groq", e).startswith("⚠️")


def test_etimedout_by_errno_counts():
    """Some stacks surface it as an OSError carrying the errno rather than as
    a socket.timeout instance."""
    import errno
    import urllib.error
    e = urllib.error.URLError(OSError(errno.ETIMEDOUT, "timed out"))
    assert llm._is_timeout(e) is True
