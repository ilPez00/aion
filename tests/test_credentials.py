"""
tests for credentials.py — provider profile CRUD, env import, resolution.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aion.credentials import (
    CredentialStore,
    ProviderProfile,
    PROVIDER_PRESETS,
    PROVIDER_ALIASES,
    DEFAULT_ENDPOINTS,
    KEY_FIELD,
)


def test_provider_profile_defaults():
    p = ProviderProfile(kind="openai")
    assert p.label == "OpenAI"
    assert p.endpoint == "https://api.openai.com/v1"
    assert p.api_key == ""
    assert p.active is False


def test_provider_profile_key_preview_short():
    p = ProviderProfile(kind="generic", api_key="abc123")
    # keys <= 16 chars: first 4 + "…"
    assert p.key_preview() == "abc1…"


def test_provider_profile_key_preview_long():
    p = ProviderProfile(kind="openai", api_key="sk-" + "x" * 40)
    prev = p.key_preview()
    assert prev.startswith("sk-xxxxx")
    assert prev.endswith("xxxx")
    assert len(prev) < 20


def test_provider_profile_key_preview_empty():
    p = ProviderProfile(kind="ollama")
    assert p.key_preview() == ""


def test_provider_profile_as_dict():
    p = ProviderProfile(kind="groq", api_key="gsk_abc", active=True)
    d = p.as_dict()
    assert d["kind"] == "groq"
    assert d["label"] == "Groq"
    assert d["active"] is True
    assert "key_preview" in d
    assert "api_key" not in d  # never leak full key


def test_credential_store_empty_save_load(tmp_path):
    path = tmp_path / "creds.json"
    store = CredentialStore(path=path)
    assert store.list() == []
    # save/load cycle
    store.save()
    store2 = CredentialStore(path=path)
    assert store2.list() == []


def test_credential_store_add_and_list(tmp_path):
    path = tmp_path / "creds.json"
    store = CredentialStore(path=path)
    store.add("openai", "sk-test123")
    profiles = store.list()
    assert len(profiles) == 1
    assert profiles[0]["kind"] == "openai"
    assert profiles[0]["key_preview"] == "sk-t…"


def test_credential_store_add_updates_existing(tmp_path):
    path = tmp_path / "creds.json"
    store = CredentialStore(path=path)
    store.add("openai", "sk-old")
    store.add("openai", "sk-new")
    profiles = store.list()
    assert len(profiles) == 1
    assert profiles[0]["key_preview"] == "sk-n…"


def test_credential_store_remove(tmp_path):
    path = tmp_path / "creds.json"
    store = CredentialStore(path=path)
    store.add("openai", "sk-test")
    assert store.remove("openai") is True
    assert store.list() == []
    assert store.remove("nonexistent") is False


def test_credential_store_set_active(tmp_path):
    path = tmp_path / "creds.json"
    store = CredentialStore(path=path)
    store.add("openai", "sk-openai")
    store.add("anthropic", "sk-ant")
    store.set_active("anthropic")
    active = store.get_active()
    assert active is not None
    assert active.kind == "anthropic"
    # verify openai is not active
    for p in store.profiles:
        if p.kind == "openai":
            assert p.active is False


def test_credential_store_get(tmp_path):
    path = tmp_path / "creds.json"
    store = CredentialStore(path=path)
    store.add("groq", "gsk-test")
    p = store.get("groq")
    assert p is not None
    assert p.api_key == "gsk-test"
    assert store.get("nonexistent") is None


def test_resolve_api_key_from_profile(tmp_path):
    path = tmp_path / "creds.json"
    store = CredentialStore(path=path)
    store.add("openai", "sk-profile-key")
    assert store.resolve_api_key("openai") == "sk-profile-key"


def test_resolve_api_key_fallback_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")
    path = tmp_path / "creds.json"
    store = CredentialStore(path=path)
    assert store.resolve_api_key("anthropic") == "sk-ant-env"


def test_resolve_api_key_unknown(tmp_path):
    path = tmp_path / "creds.json"
    store = CredentialStore(path=path)
    assert store.resolve_api_key("nonexistent") == ""


def test_resolve_endpoint_from_profile(tmp_path):
    path = tmp_path / "creds.json"
    store = CredentialStore(path=path)
    store.add("groq", "gsk-test", endpoint="https://custom.groq.test")
    assert store.resolve_endpoint("groq") == "https://custom.groq.test"


def test_resolve_endpoint_default(tmp_path):
    path = tmp_path / "creds.json"
    store = CredentialStore(path=path)
    assert store.resolve_endpoint("deepseek") == "https://api.deepseek.com"
    assert store.resolve_endpoint("ollama") == "http://localhost:11434"


def test_preset_list(tmp_path):
    path = tmp_path / "creds.json"
    store = CredentialStore(path=path)
    presets = store.preset_list()
    kinds = {p["kind"] for p in presets}
    assert "openai" in kinds
    assert "anthropic" in kinds
    for p in presets:
        assert "name" in p
        assert "configured" in p
        assert p["configured"] is False


def test_preset_list_configured(tmp_path):
    path = tmp_path / "creds.json"
    store = CredentialStore(path=path)
    store.add("openai", "sk-test")
    presets = store.preset_list()
    openai = [p for p in presets if p["kind"] == "openai"][0]
    assert openai["configured"] is True


def test_provider_aliases():
    assert PROVIDER_ALIASES["openai"] == "openai"
    assert PROVIDER_ALIASES["anthropic"] == "anthropic"
    assert PROVIDER_ALIASES["deepseek"] == "deepseek"
    assert PROVIDER_ALIASES["openai"] == "openai"
    assert PROVIDER_ALIASES["deepinfra"] == "deepinfra"


def test_all_presets_have_defaults():
    """Every preset should have a default endpoint or explicitly be empty."""
    for kind in PROVIDER_PRESETS:
        assert kind in DEFAULT_ENDPOINTS, f"{kind} missing from DEFAULT_ENDPOINTS"
        assert kind in KEY_FIELD, f"{kind} missing from KEY_FIELD"


def test_import_env(tmp_path):
    """Legacy ~/.env entries should be imported as unnamed profiles."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "OPENAI_API_KEY=sk-from-env\n"
        "ANTHROPIC_API_KEY=sk-ant-env\n"
        "# comment\n"
        "GROQ_API_KEY=gsk-env\n"
    )
    path = tmp_path / "creds.json"
    store = CredentialStore(path=path)
    # Simulate import by pointing at test env
    store.path = path
    # Manually trigger import — _import_env reads from Path.home() by default
    # so we mock by monkeypatching
    import aion.credentials as mod
    original = mod.Path.home
    mod.Path.home = lambda: tmp_path
    try:
        store._import_env()
        kinds = {p.kind for p in store.profiles}
        assert "openai" in kinds
        assert "anthropic" in kinds
        assert "groq" in kinds
    finally:
        mod.Path.home = original


def test_import_env_skips_duplicates(tmp_path):
    """Already-configured providers should not be overwritten by .env."""
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=sk-from-env\n")
    path = tmp_path / "creds.json"
    store = CredentialStore(path=path)
    store.add("openai", "sk-manual")
    import aion.credentials as mod
    original = mod.Path.home
    mod.Path.home = lambda: tmp_path
    try:
        store._import_env()
        p = store.get("openai")
        assert p is not None
        assert p.api_key == "sk-manual"  # preserve manual entry
    finally:
        mod.Path.home = original


def test_all_aliases_resolve():
    """Every preset name and its human-readable name should resolve."""
    for kind, meta in PROVIDER_PRESETS.items():
        assert PROVIDER_ALIASES[kind] == kind
        assert PROVIDER_ALIASES[meta["name"].lower()] == kind
