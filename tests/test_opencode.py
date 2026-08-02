from __future__ import annotations

import asyncio
import pytest

from aion.hermes.opencode import OpenCodeConfig, OpenCodeClient, strip_ansi


class TestStripAnsi:
    def test_strips_simple_escape(self) -> None:
        assert strip_ansi("\x1b[31mred\x1b[0m") == "red"

    def test_strips_complex_escape(self) -> None:
        assert strip_ansi("\x1b[38;5;42mcolor\x1b[0m") == "color"

    def test_plain_text_unharmed(self) -> None:
        assert strip_ansi("hello world") == "hello world"

    def test_empty_string(self) -> None:
        assert strip_ansi("") == ""


class TestOpenCodeConfig:
    def test_defaults(self) -> None:
        cfg = OpenCodeConfig()
        assert cfg.model == "deepseek/deepseek-v4-pro"
        assert cfg.binary_path == "opencode"
        assert cfg.auto_approve is False
        assert cfg.timeout == 300.0

    def test_custom_values(self) -> None:
        cfg = OpenCodeConfig(model="gpt-4", auto_approve=True, timeout=60.0)
        assert cfg.model == "gpt-4"
        assert cfg.auto_approve is True
        assert cfg.timeout == 60.0


@pytest.mark.asyncio
async def test_is_available_returns_false_on_missing_binary() -> None:
    async def fake_create(*args, **kwargs):
        raise FileNotFoundError("not found")
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    client = OpenCodeClient(OpenCodeConfig(binary_path="nonexistent"))
    assert await client.is_available() is False


@pytest.mark.asyncio
async def test_run_raises_on_missing_binary() -> None:
    async def fake_create(*args, **kwargs):
        raise FileNotFoundError("not found")
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    client = OpenCodeClient(OpenCodeConfig(binary_path="nonexistent"))
    with pytest.raises(RuntimeError, match="opencode binary not found"):
        async for line in client.run("test"):
            pass
