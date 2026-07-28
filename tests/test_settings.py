"""Settings: schema, validation, persistence.

The browser is untrusted input. Everything a settings form can send is checked
here, on the server, against a declared schema — the widget's min/max is a
usability hint, never the enforcement. These tests are mostly about what must
NOT get through.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aion import settings as S  # noqa: E402


@pytest.fixture()
def cfg(tmp_path):
    """A config file to write into, so the repo's own is never touched."""
    p = tmp_path / "layout.json"
    p.write_text(json.dumps({
        "harnesses": [
            {"id": "demo", "name": "Demo", "type": "demo", "tier": "standard",
             "enabled": True, "vram_mb": 420, "max_steps": 20},
            {"id": "research", "name": "Research", "type": "research",
             "tier": "heavy", "enabled": False},
        ],
    }))
    return p


# ── schema ───────────────────────────────────────────────────────────────────
def test_schema_is_complete_and_serialisable():
    out = S.schema()
    assert len(out) >= 8
    json.dumps(out)                       # must survive the wire
    for section in out:
        assert section["id"] and section["label"] and section["fields"]
        for f in section["fields"]:
            assert f["key"] and f["label"] and f["type"]


def test_every_choice_field_has_choices():
    """A choice with no options renders an empty dropdown — unusable, and the
    validator would reject every value including the default."""
    for section in S.SECTIONS:
        for f in section.fields:
            if f.type == S.CHOICE:
                assert f.choices, f"{section.id}.{f.key} has no choices"
                assert f.default in f.choices, f"{section.id}.{f.key} default is not a choice"


def test_numeric_fields_have_sane_bounds():
    for section in S.SECTIONS:
        for f in section.fields:
            if f.type in (S.INT, S.FLOAT) and f.min is not None and f.max is not None:
                assert f.min < f.max, f"{section.id}.{f.key}"


def test_restart_required_is_declared_where_it_is_true():
    """These decide the state root and the listening socket at boot, so a save
    that appears to take effect immediately would be a lie."""
    fleet = S.SECTION_BY_ID["fleet"]
    restart = {f.key for f in fleet.fields if f.restart}
    assert {"instance", "listen"} <= restart


# ── validation ───────────────────────────────────────────────────────────────
def test_unknown_keys_are_dropped_not_merged():
    """An unknown key from a browser is a typo or a probe. Merging it would
    write attacker-chosen content into the config file."""
    out = S.validate("fleet", {"heartbeat_s": 5, "evil": "x"})
    assert out.applied == {"heartbeat_s": 5.0}
    assert out.rejected == {"evil": "unknown setting"}
    assert out.ok is False


def test_unknown_section_is_refused():
    assert "no section" in S.validate("nope", {"a": 1}).rejected["_section"]


def test_out_of_range_is_rejected_not_clamped():
    """Clamping gives the user a setting they did not choose and no signal
    that it happened."""
    out = S.validate("fleet", {"heartbeat_s": 0})
    assert out.applied == {}
    assert "minimum" in out.rejected["heartbeat_s"]
    out = S.validate("fleet", {"heartbeat_s": 10_000})
    assert "maximum" in out.rejected["heartbeat_s"]


def test_non_numeric_is_rejected():
    assert "not a number" in S.validate("fleet", {"heartbeat_s": "soon"}).rejected["heartbeat_s"]


def test_choice_must_be_one_of_the_choices():
    out = S.validate("fleet", {"listen": "everywhere"})
    assert "not one of" in out.rejected["listen"]
    assert S.validate("fleet", {"listen": "lan"}).applied == {"listen": "lan"}


@pytest.mark.parametrize("raw,want", [
    (True, True), ("true", True), ("on", True), ("1", True),
    (False, False), ("false", False), ("off", False), ("", False),
])
def test_booleans_accept_what_a_form_actually_sends(raw, want):
    assert S.validate("persona", {"voice_enabled": raw}).applied["voice_enabled"] is want


def test_nonsense_boolean_is_rejected():
    assert "not a boolean" in S.validate("persona", {"voice_enabled": "maybe"}).rejected["voice_enabled"]


def test_readonly_fields_cannot_be_written():
    """Paths decide what the process can reach. A LAN-reachable page must not
    be able to widen its own sandbox."""
    out = S.validate("paths", {"fs_root": "/"})
    assert out.applied == {}
    assert "read-only" in out.rejected["fs_root"]


def test_overlong_strings_are_rejected():
    assert "too long" in S.validate("deck", {"port": "x" * 600}).rejected["port"]


def test_restart_keys_are_reported_when_changed():
    out = S.validate("fleet", {"listen": "lan", "heartbeat_s": 5})
    assert out.restart_needed == ["listen"]


# ── secrets ──────────────────────────────────────────────────────────────────
def test_secrets_are_never_read_back(tmp_path, monkeypatch):
    """A screenshot of a settings page must not leak a webhook token."""
    monkeypatch.setattr(S, "_config", lambda: {"notify": {"url": "https://hooks/very-secret"}})
    assert S.read("notify")["notify"]["url"] == S.REDACTED


def test_absent_secret_reads_as_empty_not_redacted(monkeypatch):
    """Otherwise the form shows dots for a value that was never set."""
    monkeypatch.setattr(S, "_config", lambda: {"notify": {}})
    assert S.read("notify")["notify"]["url"] == ""


def test_redacted_value_sent_back_is_a_no_op():
    """The browser is given dots, so it returns dots on any save that did not
    touch the field. Storing that would overwrite the real secret with '••••'."""
    out = S.validate("notify", {"url": S.REDACTED, "on_gate": True})
    assert "url" not in out.applied
    assert "url" not in out.rejected      # unchanged is not an error
    assert out.applied == {"on_gate": True}
    assert out.ok is True


def test_a_real_secret_is_accepted():
    assert S.validate("notify", {"url": "https://example.test/hook"}).applied == {
        "url": "https://example.test/hook"}


# ── environment precedence ───────────────────────────────────────────────────
def test_environment_wins_over_the_file(monkeypatch):
    """The env is the per-launch signal and is what is actually in force, so
    showing the file's value would show something that is not true."""
    monkeypatch.setattr(S, "_config", lambda: {"fleet": {"instance": "fromfile"}})
    monkeypatch.setenv("AION_INSTANCE", "fromenv")
    assert S.read("fleet")["fleet"]["instance"] == "fromenv"
    monkeypatch.delenv("AION_INSTANCE")
    assert S.read("fleet")["fleet"]["instance"] == "fromfile"


def test_defaults_fill_in_for_an_empty_config(monkeypatch):
    monkeypatch.setattr(S, "_config", lambda: {})
    values = S.read()["persona"]
    assert values["verbosity"] == "normal" and values["voice_enabled"] is True


# ── persistence ──────────────────────────────────────────────────────────────
def test_write_persists_and_merges(cfg):
    S.write("persona", {"verbosity": "terse"}, path=cfg)
    S.write("persona", {"formality": "formal"}, path=cfg)
    data = json.loads(cfg.read_text())
    assert data["persona"] == {"verbosity": "terse", "formality": "formal"}
    assert "harnesses" in data, "writing one section dropped another"


def test_write_refuses_a_section_that_is_not_persisted(cfg):
    out = S.write("paths", {"vault": "/tmp"}, path=cfg)
    assert out.ok is False
    assert "paths" not in json.loads(cfg.read_text())


def test_browser_preferences_are_not_written_to_the_config(cfg):
    """Graph detail is per-device: writing it to a shared config would make
    one person's phone change what everyone else sees."""
    out = S.write("graph", {"detail": "sparse"}, path=cfg)
    assert out.ok is False
    assert "graph" not in json.loads(cfg.read_text())


def test_nothing_is_written_when_everything_is_rejected(cfg):
    before = cfg.read_text()
    S.write("fleet", {"heartbeat_s": -5}, path=cfg)
    assert cfg.read_text() == before


def test_partial_save_reports_both_halves(cfg):
    out = S.write("fleet", {"listen": "lan", "heartbeat_s": 0}, path=cfg)
    assert out.applied == {"listen": "lan"}
    assert "heartbeat_s" in out.rejected
    assert json.loads(cfg.read_text())["fleet"] == {"listen": "lan"}


# ── harnesses ────────────────────────────────────────────────────────────────
def test_harnesses_are_listed_with_their_knobs(monkeypatch, cfg):
    monkeypatch.setattr(S, "_config", lambda: json.loads(cfg.read_text()))
    out = {h["id"]: h for h in S.harnesses()}
    assert out["demo"]["enabled"] is True and out["demo"]["vram_mb"] == 420
    assert out["research"]["enabled"] is False


def test_set_harness_changes_only_that_harness(cfg):
    S.set_harness("demo", {"enabled": False}, path=cfg)
    items = {h["id"]: h for h in json.loads(cfg.read_text())["harnesses"]}
    assert items["demo"]["enabled"] is False
    assert items["demo"]["vram_mb"] == 420, "unrelated fields were dropped"
    assert items["research"]["tier"] == "heavy", "another harness was touched"


def test_set_harness_can_add_an_approval_gate(cfg):
    """Turning this on inserts a human into the loop for that harness — the
    kind of change that should be one click, not a config-file edit."""
    S.set_harness("demo", {"requires_approval": True}, path=cfg)
    items = {h["id"]: h for h in json.loads(cfg.read_text())["harnesses"]}
    assert items["demo"]["requires_approval"] is True


def test_set_harness_rejects_unknown_fields(cfg):
    out = S.set_harness("demo", {"type": "shell"}, path=cfg)
    assert out["ok"] is False and "type" in out["rejected"]
    assert json.loads(cfg.read_text())["harnesses"][0]["type"] == "demo"


def test_set_harness_validates_ranges(cfg):
    out = S.set_harness("demo", {"max_steps": 0}, path=cfg)
    assert out["ok"] is False and "max_steps" in out["rejected"]


def test_set_harness_rejects_an_unknown_tier(cfg):
    assert S.set_harness("demo", {"tier": "cosmic"}, path=cfg)["ok"] is False


def test_set_harness_on_a_missing_harness(cfg):
    out = S.set_harness("ghost", {"enabled": True}, path=cfg)
    assert out["ok"] is False and "no harness" in out["rejected"]["_id"]


def test_snapshot_carries_schema_values_and_harnesses():
    snap = S.snapshot()
    for key in ("schema", "values", "harnesses", "providers", "skills"):
        assert key in snap
    json.dumps(snap)


def test_snapshot_never_contains_a_provider_key():
    """Presence only. The HUD is reachable from the LAN."""
    for p in S.snapshot()["providers"]:
        assert set(p) <= {"id", "name", "env", "present", "description", "models"}
        assert "value" not in p and "key" not in p


# ── file hygiene ─────────────────────────────────────────────────────────────
def test_saving_a_setting_does_not_mangle_the_rest_of_the_file(tmp_path):
    """json.dump escapes non-ASCII by default, so saving one field rewrote all
    of config/layout.json turning the workspace icons into \\u2b21 — and did
    the same to accented words in todos and memory facts. These files are read
    and edited by people."""
    p = tmp_path / "layout.json"
    p.write_text(json.dumps(
        {"workspaces": [{"id": "desktop", "icon": "⬡", "title": "Città"}]},
        ensure_ascii=False), encoding="utf-8")
    S.write("persona", {"verbosity": "terse"}, path=p)
    raw = p.read_text(encoding="utf-8")
    assert "⬡" in raw and "Città" in raw
    assert "\\u" not in raw


def test_saving_preserves_unrelated_sections(tmp_path):
    p = tmp_path / "layout.json"
    p.write_text(json.dumps({"theme": {"accent": "#5ad1ff"}, "app_name": "aion"}))
    S.write("deck", {"enabled": False}, path=p)
    data = json.loads(p.read_text())
    assert data["theme"] == {"accent": "#5ad1ff"} and data["app_name"] == "aion"
    assert data["deck"] == {"enabled": False}
