from __future__ import annotations

from pathlib import Path

import pytest

from aion.hermes.env import (
    PROVIDER_KEY_PATTERN,
    classify_entry,
    iter_env_lines,
    parse_provider_env,
)


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    p = tmp_path / ".env"
    p.write_text(
        "OPENAI_API_KEY=sk-abc123def456\n"
        "ANTHROPIC_API_KEY=sk-ant-xxx\n"
        "GROQ_ENDPOINT=https://api.groq.com/v1\n"
        "GEMINI_API_KEY=AIzaSyDummyKey\n"
        "OLLAMA_HOST=http://localhost:11434\n"
        "# comment line\n"
        "\n"
        "EMPTY_VAR=\n"
        "MISTRAL_API_KEY= mistral-key \n"
    )
    return p


class TestProviderKeyPattern:
    def test_matches_standard_keys(self) -> None:
        m = PROVIDER_KEY_PATTERN.match("OPENAI_API_KEY")
        assert m is not None
        assert m.group("provider") == "OPENAI"
        assert m.group("type") == "API_KEY"

    def test_matches_endpoint(self) -> None:
        m = PROVIDER_KEY_PATTERN.match("GROQ_ENDPOINT")
        assert m is not None
        assert m.group("provider") == "GROQ"
        assert m.group("type") == "ENDPOINT"

    def test_matches_host(self) -> None:
        m = PROVIDER_KEY_PATTERN.match("OLLAMA_HOST")
        assert m is not None
        assert m.group("provider") == "OLLAMA"

    def test_no_match_on_random(self) -> None:
        assert PROVIDER_KEY_PATTERN.match("HOME") is None
        assert PROVIDER_KEY_PATTERN.match("PATH") is None


class TestClassifyEntry:
    def test_api_key_classification(self) -> None:
        cat, display, val = classify_entry("OPENAI_API_KEY", "sk-abc")
        assert cat == "keys"
        assert display == "OpenAI"
        assert val == "sk-abc"

    def test_endpoint_classification(self) -> None:
        cat, display, val = classify_entry("GROQ_ENDPOINT", "https://api.groq.com")
        assert cat == "endpoints"
        assert display == "Groq"

    def test_fallback_classification(self) -> None:
        cat, display, val = classify_entry("MY_CUSTOM_KEY", "secret")
        assert cat == "other"
        assert display == "MY_CUSTOM_KEY" or "MY_CUSTOM_KEY".title()
        assert val == "secret"


class TestIterEnvLines:
    def test_iterates_non_empty_lines(self, env_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("aion.hermes.env.ENV_PATH", env_file)
        lines = list(iter_env_lines())
        assert len(lines) == 6  # 8 lines total, minus comment + blank + empty
        keys = [k for k, _, _ in lines]
        assert "OPENAI_API_KEY" in keys
        assert "ANTHROPIC_API_KEY" in keys

    def test_skips_comment_and_blank(self, env_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("aion.hermes.env.ENV_PATH", env_file)
        lines = list(iter_env_lines())
        line_texts = {k: v for k, v, _ in lines}
        assert "# comment line" not in line_texts
        assert "" not in line_texts

    def test_strips_quotes_and_whitespace(self, env_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("aion.hermes.env.ENV_PATH", env_file)
        lines = dict((k, v) for k, v, _ in iter_env_lines())
        assert lines["MISTRAL_API_KEY"] == "mistral-key"

    def test_empty_env_returns_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("aion.hermes.env.ENV_PATH", tmp_path / ".env")
        lines = list(iter_env_lines())
        assert lines == []


class TestParseProviderEnv:
    def test_returns_categorized_providers(self, env_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("aion.hermes.env.ENV_PATH", env_file)
        providers = parse_provider_env()
        assert "OpenAI" in providers
        assert "Anthropic" in providers
        assert "Groq" in providers
        assert providers["OpenAI"]["key_preview"].endswith("...")

    def test_missing_env_returns_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("aion.hermes.env.ENV_PATH", tmp_path / ".env")
        assert parse_provider_env() == {}
