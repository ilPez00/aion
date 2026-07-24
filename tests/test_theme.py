"""Contrast invariants for the cockpit palette.

These lock the accessibility fix. `dim` shipped at #5a6b7b, which measured
2.17:1 on a selected row while carrying most of the app's actual content
(paths, previews, setting values, the manual). A palette edit that pushes any
meaningful text back under WCAG AA should fail here, not in someone's eyes.
"""
import json
from pathlib import Path

import pytest

from aion.ui import theme as th

REPO = Path(__file__).resolve().parents[1]


def test_every_text_token_passes_aa_on_every_surface():
    failures = th.audit()
    assert failures == [], (
        "text tokens below WCAG AA 4.5:1 — (token, surface, ratio): " + str(failures)
    )


def test_contrast_maths_matches_known_wcag_values():
    # black on white is the documented maximum, 21:1
    assert round(th.contrast("#000000", "#ffffff"), 1) == 21.0
    # a colour against itself is 1:1
    assert th.contrast("#5ad1ff", "#5ad1ff") == 1.0
    # ordering is symmetric
    assert th.contrast("#0a0f14", "#dbe6f0") == th.contrast("#dbe6f0", "#0a0f14")


def test_audit_catches_a_regression():
    # feeding the OLD dim back in must be reported as a failure, otherwise the
    # audit isn't actually testing anything
    failures = th.audit({"dim": "#5a6b7b"})
    assert any(name == "dim" for name, _, _ in failures)


def test_selected_row_is_visibly_distinct_from_the_panel():
    # in a keyboard-driven TUI the selection IS the cursor; the old pair
    # differed by 1.38:1, effectively invisible
    assert th.contrast(th.SEL, th.BG) >= 1.5
    assert th.contrast(th.PANEL, th.BG) >= 1.05


def test_decoration_token_is_not_used_as_text():
    # `faint` is deliberately below the text floor, so it must stay out of the
    # list of tokens the audit treats as text
    assert "faint" not in th.TEXT_TOKENS
    assert th.contrast(th.TOKENS["faint"], th.BG) >= th.AA_LARGE


def test_legacy_token_names_survive():
    # ~180 render call sites index theme["accent"] etc. directly; renaming a
    # key would blank out text across the cockpit rather than erroring loudly
    for legacy in ("accent", "ok", "warn", "err", "dim"):
        assert legacy in th.theme_dict()


def test_shipped_config_matches_the_audited_palette():
    # config/layout.json is what actually renders; if it drifts from theme.py
    # the audit above is testing a palette nobody sees
    cfg = json.loads((REPO / "config" / "layout.json").read_text())
    assert th.audit(cfg.get("theme", {})) == []


def test_app_css_uses_only_audited_surfaces():
    # the Textual CSS block duplicates the surface hexes (CSS can't import
    # Python). Any background/border literal it uses must be a real token, so a
    # palette change here can't silently reintroduce an un-audited colour.
    # Read the source rather than import app.py — that keeps this test running
    # without the (heavy, optional) `textual` dependency installed.
    import re

    src = (REPO / "src" / "aion" / "ui" / "app.py").read_text()
    css_blocks = re.findall(r'CSS\s*=\s*"""(.*?)"""', src, re.DOTALL)
    css_blocks += re.findall(r'DEFAULT_CSS\s*=\s*"(.*?)"', src)
    known = {v.lower() for v in th.TOKENS.values()}
    hexes = {h.lower() for block in css_blocks
             for h in re.findall(r"#[0-9a-fA-F]{6}", block)}
    assert hexes, "no CSS colour literals found — did app.py move?"
    stray = hexes - known
    assert stray == set(), f"app CSS uses hexes not in theme.TOKENS: {stray}"
