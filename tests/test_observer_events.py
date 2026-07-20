"""Agent-event detection in the Observer (P2: ported from tuicommander)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aion.observer import Observer  # noqa: E402


def _obs(screen: str) -> Observer:
    o = Observer()
    o.attach("claude")
    o.tick(screen)
    return o


def test_rate_limit_with_retry_after():
    o = _obs('{"type":"rate_limit_error"}\nRetry-After: 42')
    assert o.alert_kind == "rate"
    assert 38 <= o.rate_countdown <= 42
    assert "retry" in o.alert_line


def test_rate_limit_providers():
    for s in ("overloaded_error", "RESOURCE_EXHAUSTED",
              "HTTP 429 Too Many Requests", "TPM limit reached"):
        assert _obs(s).alert_kind == "rate", s


def test_usage_meter():
    o = _obs("You've used 85% of your weekly limit")
    assert o.alert_kind == "usage"
    assert o.usage_pct == 85
    assert "weekly" in o.alert_line


def test_usage_exhausted_with_reset():
    o = _obs("You are out of usage · resets 8pm (Europe/Madrid)")
    assert o.alert_kind == "exhausted"
    assert "8pm" in o.alert_line


def test_waiting_for_input():
    for s in ("Overwrite file? (y/N)",
              "Do you want to proceed with these changes?",
              "Select:\n❯ 1. Yes\n  2. No\nEnter to select",
              "◆  Do you allow this tool call?"):
        assert _obs(s).attention is True, s


def test_clean_output_no_alert():
    o = _obs("compiling module foo\nlinking binary\ndone")
    assert o.alert_kind == ""
    assert o.attention is False


def test_detach_clears_alerts():
    o = _obs("rate_limit_error\nRetry-After: 30")
    o.detach()
    assert o.alert_kind == ""
    assert o.rate_reset_at == 0.0
    assert o.attention is False
