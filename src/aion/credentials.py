"""
credentials.py — structured provider credential store.

Inspired by cc-switch's provider preset model. Replaces flat ~/.env parsing
with typed provider records, known endpoint presets, and per-app credential
shapes (Anthropic vs OpenAI vs Google vs Hermes).

Key concepts:
  ProviderProfile — a named set of credentials for one provider (API key + endpoint)
  ProviderPreset  — a known provider definition (name, default endpoint, key field)
  CredentialStore — owns all profiles, reads/writes ~/.aion/credentials.json

Backward compatible: reads legacy ~/.env entries as unnamed profiles.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Literal


ProviderKind = Literal[
    "anthropic", "openai", "google", "groq", "mistral", "together",
    "deepseek", "xai", "openrouter", "perplexity", "github",
    "deepinfra", "replicate", "huggingface", "ollama", "zhipu",
    "hermes", "fcm", "generic",
]

KEY_FIELD: dict[ProviderKind, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GOOGLE_API_KEY",
    "groq": "GROQ_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "together": "TOGETHER_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "xai": "XAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "perplexity": "PERPLEXITY_API_KEY",
    "github": "GITHUB_TOKEN",
    "deepinfra": "DEEPINFRA_API_KEY",
    "replicate": "REPLICATE_API_TOKEN",
    "huggingface": "HUGGINGFACE_TOKEN",
    "ollama": "",  # local, no key
    "zhipu": "ZHIPU_API_KEY",
    "hermes": "HERMES_API_KEY",
    "fcm": "",
    "generic": "API_KEY",
}

DEFAULT_ENDPOINTS: dict[ProviderKind, str] = {
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com/v1",
    "google": "https://generativelanguage.googleapis.com/v1beta",
    "groq": "https://api.groq.com/openai/v1",
    "mistral": "https://api.mistral.ai/v1",
    "together": "https://api.together.xyz/v1",
    "deepseek": "https://api.deepseek.com",
    "xai": "https://api.x.ai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "perplexity": "https://api.perplexity.ai",
    "deepinfra": "https://api.deepinfra.com/v1/openai",
    "ollama": "http://localhost:11434",
    "fcm": "http://localhost:19280/v1",
    "hermes": "",
    "generic": "",
}

PROVIDER_PRESETS: dict[ProviderKind, dict] = {
    "anthropic": {"name": "Anthropic", "icon": "◈", "color": "#d4a574"},
    "openai": {"name": "OpenAI", "icon": "◇", "color": "#75a9d4"},
    "google": {"name": "Google", "icon": "◆", "color": "#8ab4f8"},
    "groq": {"name": "Groq", "icon": "▣", "color": "#f97316"},
    "mistral": {"name": "Mistral", "icon": "◇", "color": "#7c3aed"},
    "together": {"name": "Together", "icon": "▣", "color": "#6366f1"},
    "deepseek": {"name": "DeepSeek", "icon": "◈", "color": "#4f46e5"},
    "xai": {"name": "xAI", "icon": "✕", "color": "#1da1f2"},
    "openrouter": {"name": "OpenRouter", "icon": "◈", "color": "#c084fc"},
    "perplexity": {"name": "Perplexity", "icon": "◈", "color": "#22c55e"},
    "github": {"name": "GitHub", "icon": "◆", "color": "#f0f6fc"},
    "deepinfra": {"name": "DeepInfra", "icon": "▣", "color": "#a855f7"},
    "ollama": {"name": "Ollama", "icon": "◈", "color": "#f59e0b"},
    "fcm": {"name": "FCM", "icon": "◇", "color": "#10b981"},
    "hermes": {"name": "Hermes", "icon": "✦", "color": "#ec4899"},
    "generic": {"name": "Generic", "icon": "○", "color": "#9ca3af"},
}

PROVIDER_ALIASES: dict[str, ProviderKind] = {
    alias: kind
    for kind, meta in PROVIDER_PRESETS.items()
    for alias in [kind, meta["name"].lower()]
}

KNOWN_API_KEY_REGEX = re.compile(
    r"(?P<provider>[A-Za-z_]+)_(?P<type>API_KEY|SECRET|TOKEN)"
)


@dataclass
class ProviderProfile:
    kind: ProviderKind
    label: str = ""
    api_key: str = ""
    endpoint: str = ""
    api_format: str = ""  # "anthropic" | "openai" | "google" | ""
    active: bool = False
    created_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.label:
            self.label = PROVIDER_PRESETS.get(self.kind, {}).get("name", self.kind.title())
        if not self.endpoint:
            self.endpoint = DEFAULT_ENDPOINTS.get(self.kind, "")

    def key_preview(self) -> str:
        k = self.api_key
        if not k:
            return ""
        return k[:8] + "…" + k[-4:] if len(k) > 16 else k[:4] + "…"

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "label": self.label,
            "key_preview": self.key_preview(),
            "endpoint": self.endpoint,
            "api_format": self.api_format,
            "active": self.active,
        }


@dataclass
class CredentialStore:
    """Structured credential store, backed by ~/.aion/credentials.json.

    Also reads legacy ~/.env for backward compat (imported as unnamed profiles).
    """

    path: Path = field(default_factory=lambda: Path.home() / ".aion" / "credentials.json")
    profiles: list[ProviderProfile] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._load()

    # -- persistence -------------------------------------------------------

    def _load(self) -> None:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                for p in data.get("profiles", []):
                    self.profiles.append(ProviderProfile(**p))
            except Exception:
                pass
        self._import_env()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "profiles": [
                {
                    "kind": p.kind,
                    "label": p.label,
                    "api_key": p.api_key,
                    "endpoint": p.endpoint,
                    "api_format": p.api_format,
                    "active": p.active,
                    "created_at": p.created_at,
                }
                for p in self.profiles
            ]
        }
        self.path.write_text(json.dumps(data, indent=2))

    # -- .env import ------------------------------------------------------

    def _import_env(self) -> None:
        """Import legacy ~/.env entries as unnamed profiles."""
        env_path = Path.home() / ".env"
        if not env_path.exists():
            return
        existing_kinds = {p.kind for p in self.profiles}
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, raw = line.partition("=")
            key = key.strip()
            value = raw.strip().strip("\"'").strip("'")
            if not key or not value:
                continue
            m = KNOWN_API_KEY_REGEX.match(key)
            if m:
                provider = m.group("provider").lower()
                kind = PROVIDER_ALIASES.get(provider)
                if kind and kind not in existing_kinds:
                    self.profiles.append(ProviderProfile(
                        kind=kind,
                        api_key=value,
                    ))
                    existing_kinds.add(kind)

    # -- operations --------------------------------------------------------

    def list(self) -> list[dict]:
        return [p.as_dict() for p in self.profiles]

    def get(self, kind: ProviderKind) -> ProviderProfile | None:
        for p in self.profiles:
            if p.kind == kind:
                return p
        return None

    def add(self, kind: ProviderKind, api_key: str,
            endpoint: str = "", label: str = "") -> ProviderProfile:
        existing = self.get(kind)
        if existing:
            existing.api_key = api_key
            if endpoint:
                existing.endpoint = endpoint
            if label:
                existing.label = label
            self.save()
            return existing
        profile = ProviderProfile(
            kind=kind,
            api_key=api_key,
            endpoint=endpoint or DEFAULT_ENDPOINTS.get(kind, ""),
            label=label or PROVIDER_PRESETS.get(kind, {}).get("name", kind.title()),
            created_at=__import__("time").time(),
        )
        self.profiles.append(profile)
        self.save()
        return profile

    def remove(self, kind: ProviderKind) -> bool:
        for i, p in enumerate(self.profiles):
            if p.kind == kind:
                self.profiles.pop(i)
                self.save()
                return True
        return False

    def set_active(self, kind: ProviderKind) -> None:
        for p in self.profiles:
            p.active = (p.kind == kind)
        self.save()

    def resolve_api_key(self, kind: ProviderKind) -> str:
        p = self.get(kind)
        if p and p.api_key:
            return p.api_key
        env_key = KEY_FIELD.get(kind, "")
        if env_key:
            return os.environ.get(env_key, "")
        return ""

    def resolve_endpoint(self, kind: ProviderKind) -> str:
        p = self.get(kind)
        if p and p.endpoint:
            return p.endpoint
        return DEFAULT_ENDPOINTS.get(kind, "")

    def get_active(self) -> ProviderProfile | None:
        for p in self.profiles:
            if p.active:
                return p
        return None

    def preset_list(self) -> list[dict]:
        """Return all known provider presets (even if not configured)."""
        return [
            {
                "kind": kind,
                **meta,
                "configured": self.get(kind) is not None,
                "default_endpoint": DEFAULT_ENDPOINTS.get(kind, ""),
                "key_field": KEY_FIELD.get(kind, ""),
            }
            for kind, meta in PROVIDER_PRESETS.items()
        ]
