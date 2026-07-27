"""Editor launcher and outbound notifier — both are sharp edges.

`opener` runs a program; `notify` sends data off the machine. Neither may do
anything the user did not explicitly configure.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from aion import notify, opener  # noqa: E402


# ── opener: allowlist ────────────────────────────────────────────────────
def test_editor_must_be_on_the_allowlist(monkeypatch):
    """An env var is not a licence to run an arbitrary binary."""
    monkeypatch.setenv("AION_EDITOR", "sh")
    with pytest.raises(opener.OpenError, match="not allowlisted"):
        opener.pick()


def test_allowlist_check_ignores_a_directory_prefix(monkeypatch):
    monkeypatch.setenv("AION_EDITOR", "/usr/bin/../../tmp/evil/sh")
    with pytest.raises(opener.OpenError, match="not allowlisted"):
        opener.pick()


def test_a_permitted_but_absent_editor_says_so(monkeypatch):
    monkeypatch.setenv("AION_EDITOR", "kate")
    monkeypatch.setattr(opener.shutil, "which", lambda n: None)
    with pytest.raises(opener.OpenError, match="not installed"):
        opener.pick()


def test_no_editor_anywhere_is_an_error_not_a_crash(monkeypatch):
    monkeypatch.delenv("AION_EDITOR", raising=False)
    monkeypatch.setattr(opener.shutil, "which", lambda n: None)
    with pytest.raises(opener.OpenError, match="no supported editor"):
        opener.pick()


def test_preference_order_is_honoured(monkeypatch):
    monkeypatch.delenv("AION_EDITOR", raising=False)
    monkeypatch.setattr(opener.shutil, "which",
                        lambda n: f"/usr/bin/{n}" if n in ("vim", "zed") else None)
    assert opener.pick().name == "zed"        # zed outranks vim in ALLOWLIST


def test_explicit_preference_beats_the_order(monkeypatch):
    monkeypatch.setattr(opener.shutil, "which", lambda n: f"/usr/bin/{n}")
    assert opener.pick("micro").name == "micro"


# ── opener: command building ─────────────────────────────────────────────
def test_command_is_argv_not_a_string():
    e = opener.Editor("zed", "/usr/bin/zed", True)
    assert opener.command_for("/tmp/a.txt", e) == ["/usr/bin/zed", "--", "/tmp/a.txt"]


def test_double_dash_protects_a_dash_filename():
    """A file called `-R` must open, not reconfigure the editor."""
    e = opener.Editor("vim", "/usr/bin/vim", False)
    argv = opener.command_for("/tmp/-R", e)
    assert argv[-2] == "--" and argv[-1] == "/tmp/-R"


def test_shell_metacharacters_stay_inert_in_a_filename():
    e = opener.Editor("zed", "/usr/bin/zed", True)
    nasty = "/tmp/a; rm -rf ~/.aion"
    assert opener.command_for(nasty, e)[-1] == nasty      # one argv element


def test_line_numbers_only_for_editors_that_understand_them():
    zed = opener.Editor("zed", "/usr/bin/zed", True)
    vim = opener.Editor("vim", "/usr/bin/vim", False)
    assert opener.command_for("/a.py", zed, line=12)[-1] == "/a.py:12"
    assert opener.command_for("/a.py", vim, line=12)[-1] == "/a.py"


def test_launch_refuses_an_empty_path():
    with pytest.raises(opener.OpenError):
        opener.launch("")


def test_gui_editors_are_detached():
    assert opener.Editor("zed", "/usr/bin/zed", True).detached is True
    assert opener.Editor("vim", "/usr/bin/vim", False).detached is False


# ── notify: off by default ───────────────────────────────────────────────
def test_disabled_with_no_webhook_configured(monkeypatch):
    monkeypatch.delenv("AION_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("AION_SLACK_WEBHOOK", raising=False)
    notify.reset()
    assert notify.enabled() is False
    assert notify.Notifier().send("hello", kind="failed") is False


def test_either_env_var_enables_it(monkeypatch):
    monkeypatch.delenv("AION_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("AION_SLACK_WEBHOOK", "https://hooks.example/abc")
    notify.reset()
    assert notify.enabled() is True


def test_nothing_is_sent_when_disabled(monkeypatch):
    """The safety property: importing this module cannot leak anything."""
    sent = []
    monkeypatch.setattr(notify.urllib.request, "urlopen",
                        lambda *a, **k: sent.append(a) or None)
    n = notify.Notifier(url="")
    n.task_event({"id": "t1", "label": "x", "state": "failed"})
    n.gate_event("rm -rf /")
    assert sent == []


# ── notify: which events ─────────────────────────────────────────────────
@pytest.mark.parametrize("state,expected", [
    ("failed", True), ("stalled", True), ("interrupted", True),
    ("done", False), ("running", False), ("pending", False),
])
def test_only_interesting_states_notify(state, expected):
    n = notify.Notifier(url="https://hooks.example/abc")
    assert n.should_send(state, f"k:{state}") is expected


def test_routine_success_is_hud_only():
    """Notifying on every completion trains people to ignore notifications."""
    n = notify.Notifier(url="https://hooks.example/abc")
    assert n.should_send("done", "k") is False


# ── notify: dedupe + rate limit ──────────────────────────────────────────
def test_the_same_event_is_not_repeated_within_the_window():
    n = notify.Notifier(url="https://hooks.example/abc", dedupe_s=300)
    n._last_sent["k"] = 1000.0
    assert n.should_send("failed", "k", now=1100.0) is False
    assert n.should_send("failed", "k", now=1400.0) is True


def test_distinct_events_are_not_deduped_against_each_other():
    n = notify.Notifier(url="https://hooks.example/abc")
    n._last_sent["a"] = 1000.0
    assert n.should_send("failed", "b", now=1001.0) is True


def test_suppressed_events_are_counted(monkeypatch):
    n = notify.Notifier(url="https://hooks.example/abc")
    n.send("x", kind="done")
    assert n.suppressed == 1 and n.sent == 0


# ── notify: failure handling ─────────────────────────────────────────────
def test_an_unreachable_endpoint_never_raises(monkeypatch, capsys):
    """This is called from harness loops — it must not kill a running agent."""
    def boom(*a, **k):
        raise notify.urllib.error.URLError("no route to host")
    monkeypatch.setattr(notify.urllib.request, "urlopen", boom)
    n = notify.Notifier(url="https://hooks.example/abcdefghijklmnop")
    assert n.send("x", kind="failed") is False
    assert "send failed" in capsys.readouterr().out


def test_a_failed_send_is_not_recorded_as_delivered(monkeypatch):
    """Otherwise dedupe would swallow the retry of an alert never received."""
    monkeypatch.setattr(notify.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    n = notify.Notifier(url="https://hooks.example/abc")
    n.send("x", kind="failed", key="k")
    assert "k" not in n._last_sent


def test_the_webhook_url_is_never_logged_in_full(monkeypatch, capsys):
    secret = "https://hooks.slack.com/services/T00/B00/XXXXSECRETXXXX"
    monkeypatch.setattr(notify.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    notify.Notifier(url=secret).send("x", kind="failed")
    assert "XXXXSECRETXXXX" not in capsys.readouterr().out


# ── notify: message shape ────────────────────────────────────────────────
def test_task_event_names_the_task_and_its_instance(monkeypatch):
    seen = {}

    class R:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake(req, timeout=None):
        seen["body"] = notify.json.loads(req.data)
        return R()

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake)
    n = notify.Notifier(url="https://hooks.example/abc", min_interval_s=0)
    assert n.task_event({"id": "t1", "label": "build parser",
                         "state": "failed", "instance": "main"}) is True
    assert "build parser" in seen["body"]["text"]
    assert "main" in seen["body"]["text"]


def test_gate_event_is_always_interesting():
    n = notify.Notifier(url="https://hooks.example/abc")
    assert n.should_send("gate", "g1") is True
