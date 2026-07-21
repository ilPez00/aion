"""
tests for gbrain.py — MCP bridge, BrainStore fallback logic, env helpers.
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import aion.gbrain as gbrain
from aion.gbrain import BrainStore


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def test_gbrain_bin_none_when_not_installed(monkeypatch):
    monkeypatch.setattr(gbrain.shutil, "which", lambda b: None)
    assert gbrain._gbrain_bin() is None


def test_gbrain_bin_found_when_installed(monkeypatch):
    monkeypatch.setattr(gbrain.shutil, "which", lambda b: "/usr/local/bin/gbrain")
    assert gbrain._gbrain_bin() == "/usr/local/bin/gbrain"


def test_gbrain_token_from_env(monkeypatch):
    monkeypatch.delenv("GBRAIN_REMOTE_TOKEN", raising=False)
    monkeypatch.delenv("GBRAIN_TOKEN", raising=False)
    monkeypatch.setenv("GBRAIN_TOKEN", "secret123")
    assert gbrain._gbrain_token() == "secret123"


def test_gbrain_token_prefers_remote_token(monkeypatch):
    monkeypatch.setenv("GBRAIN_REMOTE_TOKEN", "remote-secret")
    monkeypatch.setenv("GBRAIN_TOKEN", "fallback-secret")
    assert gbrain._gbrain_token() == "remote-secret"


def test_gbrain_token_none_when_unset(monkeypatch):
    monkeypatch.delenv("GBRAIN_REMOTE_TOKEN", raising=False)
    monkeypatch.delenv("GBRAIN_TOKEN", raising=False)
    assert gbrain._gbrain_token() is None


def test_gbrain_remote_url_from_env(monkeypatch):
    monkeypatch.setenv("GBRAIN_REMOTE_URL", "http://gbrain.local:3000")
    url = gbrain._gbrain_remote_url()
    assert url == "http://gbrain.local:3000"


def test_gbrain_remote_url_prefers_config(monkeypatch):
    monkeypatch.delenv("GBRAIN_REMOTE_URL", raising=False)
    monkeypatch.setattr(gbrain, "load_config", lambda: {
        "gbrain": {"remote_url": "http://config.url"}
    })
    assert gbrain._gbrain_remote_url() == "http://config.url"


def test_gbrain_remote_url_none_when_unset(monkeypatch):
    monkeypatch.delenv("GBRAIN_REMOTE_URL", raising=False)
    monkeypatch.setattr(gbrain, "load_config", lambda: {})
    assert gbrain._gbrain_remote_url() is None


def test_available_false_when_no_binary(monkeypatch):
    monkeypatch.setattr(gbrain.shutil, "which", lambda b: None)
    monkeypatch.setattr(gbrain, "load_config", lambda: {})
    assert gbrain.available() is False


def test_identity_returns_error_when_unreachable(monkeypatch):
    monkeypatch.setattr(gbrain, "_mcp_call", lambda *a, **kw: None)
    result = gbrain.identity()
    assert "error" in result
    assert "unreachable" in result["error"]


def test_capture_returns_error_when_unreachable(monkeypatch):
    monkeypatch.setattr(gbrain, "_mcp_call", lambda *a, **kw: None)
    result = gbrain.capture("test note")
    assert result["ok"] is False
    assert "unreachable" in result["error"]


def test_search_returns_empty_when_unreachable(monkeypatch):
    monkeypatch.setattr(gbrain, "_mcp_call", lambda *a, **kw: None)
    assert gbrain.search("test") == []


def test_recall_returns_empty_when_unreachable(monkeypatch):
    monkeypatch.setattr(gbrain, "_mcp_call", lambda *a, **kw: None)
    assert gbrain.recall("test") == []


def test_think_returns_error_when_unreachable(monkeypatch):
    monkeypatch.setattr(gbrain, "_mcp_call", lambda *a, **kw: None)
    result = gbrain.think("question?")
    assert result["answer"] is None
    assert "unreachable" in result["error"]


def test_volunteer_context_returns_empty_when_unreachable(monkeypatch):
    monkeypatch.setattr(gbrain, "_mcp_call", lambda *a, **kw: None)
    assert gbrain.volunteer_context("hello") == []


def test_extract_facts_returns_error_when_unreachable(monkeypatch):
    monkeypatch.setattr(gbrain, "_mcp_call", lambda *a, **kw: None)
    result = gbrain.extract_facts("some turn text")
    assert "error" in result
    assert "unreachable" in result["error"]


# ---------------------------------------------------------------------------
# BrainStore tests
# ---------------------------------------------------------------------------

def test_brainstore_init_falls_back_to_memory():
    """Without gbrain, BrainStore delegates to MemoryStore."""
    bs = BrainStore()
    assert bs._fallback is not None
    assert bs._gbrain_ok is False


def test_brainstore_add_falls_back_to_memory(monkeypatch):
    monkeypatch.setattr(gbrain, "available", lambda: False)
    bs = BrainStore()
    result = bs.add("hello world")
    assert result["text"] == "hello world"


def test_brainstore_add_uses_gbrain_when_available(monkeypatch):
    monkeypatch.setattr(gbrain, "available", lambda: True)
    monkeypatch.setattr(gbrain, "capture", lambda text: {
        "ok": True, "slug": "inbox/1000"
    })
    bs = BrainStore()
    result = bs.add("gbrain memo")
    assert result["source"] == "gbrain"
    assert result["slug"] == "inbox/1000"


def test_brainstore_add_falls_back_on_gbrain_failure(monkeypatch):
    monkeypatch.setattr(gbrain, "available", lambda: True)
    monkeypatch.setattr(gbrain, "capture", lambda text: {
        "ok": False, "error": "brain full"
    })
    bs = BrainStore()
    result = bs.add("test")
    assert result["text"] == "test"


def test_brainstore_search_falls_back_to_memory(monkeypatch):
    monkeypatch.setattr(gbrain, "available", lambda: False)
    bs = BrainStore()
    bs.add("findable fact")
    results = bs.search("findable")
    assert len(results) >= 1
    assert results[0]["source"] == "memory"


def test_brainstore_search_uses_gbrain_when_available(monkeypatch):
    monkeypatch.setattr(gbrain, "available", lambda: True)
    monkeypatch.setattr(gbrain, "search", lambda query, limit=10: [
        {"title": "gbrain result", "slug": "doc/1", "score": 0.95}
    ])
    bs = BrainStore()
    results = bs.search("test")
    assert len(results) == 1
    assert results[0]["source"] == "gbrain"
    assert results[0]["text"] == "gbrain result"


def test_brainstore_search_falls_back_on_empty_gbrain(monkeypatch):
    monkeypatch.setattr(gbrain, "available", lambda: True)
    monkeypatch.setattr(gbrain, "search", lambda query, limit=10: [])
    bs = BrainStore()
    bs.add("fallback fact")
    results = bs.search("fallback")
    assert len(results) >= 1
    assert results[0]["source"] == "memory"


def test_brainstore_recall_falls_back_to_empty(monkeypatch):
    monkeypatch.setattr(gbrain, "available", lambda: False)
    bs = BrainStore()
    assert bs.recall("anything") == []


def test_brainstore_recall_uses_gbrain(monkeypatch):
    monkeypatch.setattr(gbrain, "available", lambda: True)
    monkeypatch.setattr(gbrain, "recall", lambda query, limit=10: [
        {"fact": "hot fact", "score": 0.99}
    ])
    bs = BrainStore()
    results = bs.recall("hot")
    assert len(results) == 1
    assert results[0]["fact"] == "hot fact"


def test_brainstore_think_falls_back_to_none(monkeypatch):
    monkeypatch.setattr(gbrain, "available", lambda: False)
    bs = BrainStore()
    assert bs.think("question?") is None


def test_brainstore_think_uses_gbrain(monkeypatch):
    monkeypatch.setattr(gbrain, "available", lambda: True)
    monkeypatch.setattr(gbrain, "think", lambda q: {
        "answer": "42", "citations": []
    })
    bs = BrainStore()
    result = bs.think("meaning of life?")
    assert result["answer"] == "42"


def test_brainstore_facts_delegates_to_memory():
    bs = BrainStore()
    assert bs.facts == bs._fallback.facts


def test_brainstore_items_delegates_to_memory():
    bs = BrainStore()
    assert bs.items() == bs._fallback.items()


def test_brainstore_forget_delegates_to_memory(monkeypatch):
    monkeypatch.setattr(gbrain, "available", lambda: False)
    bs = BrainStore()
    bs.add("forgettable")
    before = len(bs.items())
    assert bs.forget(1) is True
    assert len(bs.items()) == before - 1


def test_brainstore_health_check_caches_result(monkeypatch):
    """_check_gbrain should cache for 30s and not re-check."""
    calls = []
    monkeypatch.setattr(gbrain, "available", lambda: (calls.append(1), True)[1])
    bs = BrainStore()
    assert bs._check_gbrain() is True
    assert len(calls) == 1
    # second call within 30s window uses cache
    assert bs._check_gbrain() is True
    assert len(calls) == 1


def test_brainstore_health_check_respects_cooldown(monkeypatch):
    """After 30s, _check_gbrain should re-run the check."""
    real_time = time.time()
    # each cache-miss call uses 2 time.time() calls (check + store)
    fake_times = iter([real_time, real_time + 31, real_time + 62, real_time + 93])

    monkeypatch.setattr(gbrain, "available", lambda: True)
    monkeypatch.setattr(gbrain.time, "time", lambda: next(fake_times))
    bs = BrainStore()
    assert bs._check_gbrain() is True
    # second call is past 30s cooldown, re-checks
    assert bs._check_gbrain() is True
