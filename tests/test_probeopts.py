"""The probe budget knob: defaults, overrides, and refusing to be broken by a typo.

These exist because a bad value here does not raise at the call site — it either
stalls the HUD or makes a live node look dead, so the parsing has to be dull and
observable.
"""
from __future__ import annotations

from aion import probeopts


def test_defaults_when_env_is_unset(monkeypatch):
    monkeypatch.delenv("AION_PROBE_CONNECT_TIMEOUT", raising=False)
    monkeypatch.delenv("AION_PROBE_TIMEOUT", raising=False)
    assert probeopts.connect_timeout() == probeopts.DEFAULT_CONNECT_TIMEOUT
    assert probeopts.budget() == probeopts.DEFAULT_BUDGET


def test_env_overrides_are_read_at_call_time(monkeypatch):
    monkeypatch.setenv("AION_PROBE_CONNECT_TIMEOUT", "1")
    monkeypatch.setenv("AION_PROBE_TIMEOUT", "2")
    assert probeopts.connect_timeout() == 1
    assert probeopts.budget() == 2
    # read at call time, not import time: a later change is honoured
    monkeypatch.setenv("AION_PROBE_TIMEOUT", "9")
    assert probeopts.budget() == 9


def test_garbage_in_env_falls_back_instead_of_raising(monkeypatch):
    """A typo in an env var must not take the HUD down with it."""
    for bad in ("", "   ", "soon", "1m", "nan", "0", "-4", "0.5"):
        monkeypatch.setenv("AION_PROBE_TIMEOUT", bad)
        assert probeopts.budget() == probeopts.DEFAULT_BUDGET, bad


def test_explicit_default_is_respected(monkeypatch):
    monkeypatch.delenv("AION_PROBE_TIMEOUT", raising=False)
    assert probeopts.budget(30) == 30
    monkeypatch.setenv("AION_PROBE_TIMEOUT", "3")
    assert probeopts.budget(30) == 3


def test_ssh_opts_carry_the_budget_and_stay_non_interactive(monkeypatch):
    monkeypatch.setenv("AION_PROBE_CONNECT_TIMEOUT", "2")
    opts = probeopts.ssh_opts()
    assert "ConnectTimeout=2" in opts
    # BatchMode is not decoration: a probe that can prompt is a probe that hangs
    assert "BatchMode=yes" in opts
    assert "ServerAliveInterval=15" not in opts
    assert "ServerAliveInterval=15" in probeopts.ssh_opts(keepalive=15)


def test_collectors_pass_the_budget_to_ssh(tmp_path, monkeypatch):
    """The knob is only real if the transports carry it to the actual call.

    Tested through a fake `ssh` on PATH rather than by patching `subprocess`:
    the modules import it where they use it, so a module-attribute patch would
    silently test nothing.
    """
    import os

    monkeypatch.setenv("AION_PROBE_CONNECT_TIMEOUT", "1")
    monkeypatch.setenv("AION_PROBE_TIMEOUT", "2")

    log = tmp_path / "ssh.log"
    fake = tmp_path / "ssh"
    fake.write_text(
        "#!/bin/sh\n"
        "printf 'CALL\\n' >> " + str(log) + "\n"
        "for a in \"$@\"; do printf 'ARG|%s\\n' \"$a\" >> " + str(log) + "; done\n"
        "exit 255\n"
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")

    from aion import meshmon, meshsrv, sentinelx

    meshmon.probe_node("pansa")
    meshsrv.probe_service("colibri", host="omo-ts")
    sentinelx.probe_host("pansa", "pansa-ts")

    lines = log.read_text().splitlines()
    calls = lines.count("CALL")
    args = [l[4:] for l in lines if l.startswith("ARG|")]
    assert calls >= 3, lines
    # each invocation carries the budgeted connect timeout and stays non-interactive
    assert args.count("ConnectTimeout=1") >= calls, lines
    assert args.count("BatchMode=yes") >= calls, lines
