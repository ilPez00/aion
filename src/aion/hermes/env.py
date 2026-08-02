"""Parse ~/.env into categorized provider dicts for the settings workspace."""

import os
import re
from pathlib import Path
from typing import Iterator


ENV_PATH = Path.home() / ".env"

PROVIDER_ALIASES: dict[str, str] = {
    "openai": "OpenAI", "anthropic": "Anthropic", "google": "Google",
    "groq": "Groq", "mistral": "Mistral", "cohere": "Cohere",
    "together": "Together", "deepinfra": "DeepInfra", "replicate": "Replicate",
    "huggingface": "Hugging Face", "stability": "Stability",
    "claude": "Anthropic", "gemini": "Google", "ollama": "Ollama",
    "openrouter": "OpenRouter", "deepseek": "DeepSeek",
    "xai": "xAI", "perplexity": "Perplexity", "github": "GitHub",
    "zhipu": "Zhipu", "baizarlink": "Baizarlink", "kluster": "Kluster",
}

CATEGORIES: dict[str, str] = {
    "api_key": "keys", "secret": "keys", "token": "keys",
    "endpoint": "endpoints", "host": "endpoints", "url": "endpoints",
    "proxy": "proxies", "model": "models",
}

PROVIDER_KEY_PATTERN = re.compile(
    r"(?P<provider>[A-Za-z_]+)_(?P<type>API_KEY|SECRET|TOKEN|ENDPOINT|HOST|URL|PROXY|MODEL)"
)


def classify_entry(key: str, value: str) -> tuple[str, str, str]:
    m = PROVIDER_KEY_PATTERN.match(key)
    if m:
        provider = m.group("provider").lower()
        display = PROVIDER_ALIASES.get(provider, provider.title())
        ktype = m.group("type").lower()
        category = CATEGORIES.get(ktype, "other")
        return (category, display, value)
    for alias, display in PROVIDER_ALIASES.items():
        if alias in key.lower():
            return ("other", display, value)
    return ("other", key, value)


def parse_provider_env() -> dict[str, dict]:
    providers: dict[str, dict] = {}
    if not ENV_PATH.exists():
        return providers
    text = ENV_PATH.read_text()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, raw = line.partition("=")
        key = key.strip()
        value = raw.strip().strip("\"'").strip("'")
        if not key or not value:
            continue
        category, display, val = classify_entry(key, value)
        if display not in providers:
            providers[display] = {"endpoint": "", "key_preview": "", "category": category}
        if "endpoint" in category or category == "endpoints":
            providers[display]["endpoint"] = val
        elif "key" in category or category == "keys":
            providers[display]["key_preview"] = val[:8] + "..." + val[-4:] if len(val) > 16 else val[:4] + "..."
    return providers


def iter_env_lines() -> Iterator[tuple[str, str, str]]:
    if not ENV_PATH.exists():
        return
    text = ENV_PATH.read_text()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, raw = line.partition("=")
        key = key.strip()
        value = raw.strip().strip("\"'").strip("'")
        if not key or not value:
            continue
        yield (key, value, classify_entry(key, value)[0])
