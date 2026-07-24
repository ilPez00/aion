"""ui/theme.py — the cockpit's design tokens, in one auditable place.

Why this module exists
----------------------
Colours used to live as bare hex strings inside `config/layout.json` and as
literals inside the Textual CSS block in `app.py`. Two problems fell out of
that:

1. Nothing checked contrast. `dim` (#5a6b7b) measured 3.5:1 against the app
   background and **2.2:1** against a selected row — well under the WCAG AA
   floor of 4.5:1 — and `dim` is the single most-used colour in the cockpit
   (63 call sites), carrying real content: file paths, note previews, ETAs,
   setting values, the whole manual. The app's information was the least
   legible thing on screen.
2. Surfaces were indistinguishable. The rail, centre and right panels sat at
   #0e161e over a #0c1116 screen — a 1.04:1 difference, i.e. none. There was
   no visual structure to read the layout by.

So tokens live here, `SURFACES` records which backgrounds a foreground token
may legally land on, and `audit()` proves the pairs. `tests/test_theme.py`
runs that audit, so a future palette tweak that dims text back into the mud
fails CI instead of shipping.

Contrast maths is WCAG 2.1 relative luminance. Terminals do not composite or
antialias text the way browsers do, so the ratios here are if anything
conservative — which is the direction we want to err.
"""
from __future__ import annotations

# --------------------------------------------------------------------------
# Surfaces — the backgrounds text can land on, darkest to lightest.
#
# Kept in one cool blue-slate family so the cockpit reads as a single
# instrument rather than a pile of panels, but separated enough to be seen:
# `panel` lifts off `bg`, and `sel` is a deliberate jump so the focused row
# is unmistakable. In a keyboard-driven TUI the focus indicator IS the
# cursor; if it's subtle, the app is unusable.
# --------------------------------------------------------------------------
BG = "#0a0f14"       # app background — the void behind everything
PANEL = "#131d26"    # rail / right-hand panel fill, lifts off BG
SEL = "#1d3a4d"      # selected/focused row — the loudest surface
BORDER = "#25384a"   # panel edges at rest
BORDER_HI = "#5ad1ff"  # panel edge when that panel owns the keyboard

# Every foreground token must stay legible on all three of these.
SURFACES = (BG, PANEL, SEL)

# --------------------------------------------------------------------------
# Foreground tokens.
#
# The five legacy names (accent/ok/warn/err/dim) are preserved verbatim in
# meaning so the ~180 existing call sites keep working untouched; only their
# values move. New names are additive.
# --------------------------------------------------------------------------
TOKENS = {
    # semantic state — hue carries meaning, but never hue ALONE (every state
    # is also glyph-marked, so red/green colour blindness doesn't lose data)
    "accent": "#5ad1ff",   # active, selected, interactive, "you are here"
    "ok": "#7CFFB2",       # done, healthy, connected
    "warn": "#FFD479",     # running, pending, needs-attention
    "err": "#FF8A8A",      # failed, blocked, denied
    #                        ^ was #FF6B6B: too hot on the selected row
    # text ramp — primary content down to de-emphasised content.
    "fg": "#dbe6f0",       # primary text
    "dim": "#9aabbb",      # secondary text. Was #5a6b7b (2.2:1 on a selected
    #                        row). This is the single highest-impact change in
    #                        the whole redesign — it lifts 63 call sites of
    #                        real content from illegible to AA-compliant.
    "faint": "#6b7d8d",    # TRUE decoration only: rules, separators, the
    #                        unfilled half of a progress bar. Never text.
    #                        Exempt from the 4.5:1 rule by WCAG 1.4.3, which
    #                        scopes the requirement to text conveying meaning.
    # surfaces, exposed so render code can reference them by name
    "bg": BG,
    "panel": PANEL,
    "sel": SEL,
    "border": BORDER,
    "border_hi": BORDER_HI,
}

# Foreground tokens that carry meaning and therefore owe 4.5:1 everywhere.
TEXT_TOKENS = ("accent", "ok", "warn", "err", "fg", "dim")

AA_TEXT = 4.5   # WCAG 2.1 AA, normal-size text
AA_LARGE = 3.0  # WCAG 2.1 AA, large/bold text — used for decoration floor


# --------------------------------------------------------------------------
# Contrast maths (WCAG 2.1)
# --------------------------------------------------------------------------
def _channel(value: float) -> float:
    """sRGB gamma decode for one 0..1 channel."""
    return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4


def luminance(hex_colour: str) -> float:
    """WCAG relative luminance of a #rrggbb colour."""
    h = hex_colour.lstrip("#")
    r, g, b = (_channel(int(h[i:i + 2], 16) / 255) for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    """Contrast ratio between two colours, 1.0 (identical) to 21.0 (max)."""
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def audit(theme: dict | None = None) -> list[tuple[str, str, float]]:
    """Return every (token, surface, ratio) that fails the AA text floor.

    Empty list == the palette is legible on every surface it can land on.
    Used by the test suite; also handy from a REPL when tuning colours.
    """
    t = {**TOKENS, **(theme or {})}
    failures = []
    for name in TEXT_TOKENS:
        colour = t.get(name)
        if not colour:
            continue
        for surface in SURFACES:
            ratio = contrast(colour, surface)
            if ratio < AA_TEXT:
                failures.append((name, surface, round(ratio, 2)))
    return failures


def theme_dict() -> dict:
    """The palette as the config/`cfg['theme']` shape the cockpit expects."""
    return dict(TOKENS)
