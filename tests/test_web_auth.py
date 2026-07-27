"""The web HUD's token gate.

scripts/aion_web.py serves the filesystem, runs latexmk and drives the agent.
Bound off loopback that is a shell for anyone on the WiFi, so non-loopback
binds require the shared fleet secret. These tests pin the gate itself; they
never bind a port.

aion_web is a script, not part of the package, so it is loaded by path.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

WEB_PY = Path(__file__).resolve().parents[1] / "scripts" / "aion_web.py"


@pytest.fixture(scope="module")
def web():
    spec = importlib.util.spec_from_file_location("aion_web_undertest", WEB_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _handler(web, headers: dict):
    """A Handler with no socket -- only the auth path is exercised."""
    h = web.Handler.__new__(web.Handler)
    h.headers = headers
    return h


# ---- the gate ------------------------------------------------------------

def test_loopback_needs_no_token(web, monkeypatch):
    monkeypatch.setattr(web, "AUTH_REQUIRED", False)
    monkeypatch.setattr(web, "TOKEN", "s3cret")
    ok, cookie = _handler(web, {})._authorized({})
    assert ok and not cookie


def test_lan_rejects_missing_token(web, monkeypatch):
    monkeypatch.setattr(web, "AUTH_REQUIRED", True)
    monkeypatch.setattr(web, "TOKEN", "s3cret")
    ok, _ = _handler(web, {})._authorized({})
    assert not ok


def test_lan_rejects_wrong_token(web, monkeypatch):
    monkeypatch.setattr(web, "AUTH_REQUIRED", True)
    monkeypatch.setattr(web, "TOKEN", "s3cret")
    ok, _ = _handler(web, {"X-Aion-Token": "guess"})._authorized({})
    assert not ok


def test_header_token_accepted(web, monkeypatch):
    monkeypatch.setattr(web, "AUTH_REQUIRED", True)
    monkeypatch.setattr(web, "TOKEN", "s3cret")
    ok, cookie = _handler(web, {"X-Aion-Token": "s3cret"})._authorized({})
    assert ok and not cookie  # header callers get no cookie


def test_query_token_accepted_and_sets_cookie(web, monkeypatch):
    """The phone's first hit carries ?token=; the reply plants the cookie."""
    monkeypatch.setattr(web, "AUTH_REQUIRED", True)
    monkeypatch.setattr(web, "TOKEN", "s3cret")
    ok, cookie = _handler(web, {})._authorized({"token": ["s3cret"]})
    assert ok and cookie


def test_cookie_token_accepted(web, monkeypatch):
    monkeypatch.setattr(web, "AUTH_REQUIRED", True)
    monkeypatch.setattr(web, "TOKEN", "s3cret")
    hdrs = {"Cookie": "theme=dark; aion_token=s3cret; other=1"}
    ok, cookie = _handler(web, hdrs)._authorized({})
    assert ok and not cookie


def test_empty_token_never_authorizes_empty_presentation(web, monkeypatch):
    """A blank TOKEN must not turn every empty request into a valid one.

    hmac.compare_digest("", "") is True, so the gate has to short-circuit on
    a missing secret instead of comparing. It disables auth (loopback-style)
    rather than accepting everyone as authenticated -- and main() refuses to
    bind off-loopback without a token at all, so this state is unreachable
    on a LAN.
    """
    monkeypatch.setattr(web, "AUTH_REQUIRED", True)
    monkeypatch.setattr(web, "TOKEN", "")
    ok, cookie = _handler(web, {})._authorized({})
    assert ok and not cookie


# ---- the bind decision ----------------------------------------------------

@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", ""])
def test_loopback_hosts(web, host):
    assert web._loopback(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.20"])
def test_non_loopback_hosts(web, host):
    assert not web._loopback(host)
