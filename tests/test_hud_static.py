"""Static invariants of the web HUD's HTML/CSS/JS.

These are the bugs a Python test suite could not see and a browser found in
minutes. They are cheap, they run everywhere, and each one encodes a mistake
that actually shipped:

  * `hidden` outranked by a `display:` rule — the command palette, its
    full-screen backdrop, the gate bar, the focus badge, the routing dialog,
    the voice readout and the Agents toolbar all rendered permanently.
  * a module loader re-parenting the other module's root out of the document,
    so opening Chat permanently broke LaTeX and vice versa.
  * a cache-first service worker serving the frontend it first saw, forever.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "scripts" / "static"

HTML = (STATIC / "index.html").read_text()
CSS_RAW = (STATIC / "hud.css").read_text()
# Comments here explain these very rules and quote them verbatim, so a naive
# search finds the prose instead of the declaration.
CSS = re.sub(r"/\*.*?\*/", "", CSS_RAW, flags=re.S)
HUD_JS = (STATIC / "hud.js").read_text()
SW_JS = (STATIC / "sw.js").read_text()


# ── the `hidden` attribute has to mean hidden ────────────────────────────────
def test_a_global_rule_makes_hidden_win():
    """`[hidden]` is attribute specificity (0,0,1,0). ANY `#id { display: … }`
    outranks it, and six of them did."""
    assert re.search(r"\[hidden\]\s*\{[^}]*display:\s*none\s*!important", CSS)


def test_no_element_toggled_by_hidden_is_left_to_specificity():
    """Every id that JS toggles via `.hidden` must either be covered by the
    global rule or carry its own `[hidden]` guard — never rely on the UA rule
    winning against an id selector."""
    toggled = set(re.findall(r"\$\('([\w-]+)'\)\.hidden\s*=", HUD_JS))
    toggled |= set(re.findall(r"getElementById\('([\w-]+)'\)\.hidden\s*=", HUD_JS))
    assert toggled, "nothing toggles hidden — did the selector change?"
    # The global rule covers all of them; this asserts it is still global
    # (unscoped) rather than having been narrowed to a few ids.
    rule = re.search(r"(^|\n)\s*\[hidden\]\s*\{", CSS)
    assert rule, "the [hidden] rule is no longer a bare global selector"


def test_hidden_beats_an_inline_display_style():
    """#chat-root carries `style="display:flex"`. Only !important can beat an
    inline style, which is why the global rule has it."""
    assert 'id="chat-root"' in HTML and "display:flex" in HTML
    rule = re.search(r"(?<![\w\]])\[hidden\]\s*\{[^}]*\}", CSS)
    assert rule and "!important" in rule.group(0)


# ── module roots must not be re-parented ─────────────────────────────────────
def test_panel_modules_are_toggled_not_reparented():
    """`replaceChildren(a)` detaches b, so the next getElementById(b) is null.
    Opening Chat used to remove LaTeX from the document permanently."""
    assert "replaceChildren($('chat-root'))" not in HUD_JS
    assert "replaceChildren($('latex-root'))" not in HUD_JS


def test_both_panel_roots_live_in_the_markup():
    for id_ in ("chat-root", "latex-root", "panel-view"):
        assert f'id="{id_}"' in HTML


# ── service worker must not pin the frontend ─────────────────────────────────
def test_service_worker_is_network_first():
    """Cache-first meant an installed HUD served the JS/CSS it first saw until
    somebody remembered to bump a constant by hand."""
    fetch_handler = SW_JS.split("addEventListener('fetch'", 1)[1]
    net = fetch_handler.find("fetch(e.request)")
    cache = fetch_handler.find("caches.match")
    assert net != -1 and cache != -1
    assert net < cache, "cache is consulted before the network"


def test_service_worker_never_caches_the_api():
    """Live data served from a cache is worse than no data: it looks current."""
    guard = re.search(r"startsWith\('/api/'\)\)\s*return", SW_JS)
    assert guard, "no early return for /api/ in the fetch handler"


def test_service_worker_only_stores_successful_responses():
    """Caching a 404 makes the offline fallback serve the 404."""
    assert "r.ok" in SW_JS


# ── the daemon must let a reload see new bytes ───────────────────────────────
def test_static_assets_are_served_revalidating():
    web = (ROOT / "scripts" / "aion_web.py").read_text()
    static_block = web.split('if p.startswith("/static/")', 1)[1][:1200]
    assert '"Cache-Control": "no-cache"' in static_block


def test_the_service_worker_script_itself_is_not_cached():
    """A stale service worker cannot be replaced by the newer one it serves."""
    web = (ROOT / "scripts" / "aion_web.py").read_text()
    assert 'extra = {"Cache-Control": "no-cache"}' in web


# ── callbacks the renderer promises ──────────────────────────────────────────
def test_every_graph_callback_is_read_from_opts():
    """`this.onLod` was called but never assigned, so the deferred-node badge
    never appeared. Anything the renderer invokes must be wired from opts."""
    organic = (STATIC / "organic.js").read_text()
    called = set(re.findall(r"this\.(on[A-Z]\w*)\s*\(", organic))
    assigned = set(re.findall(r"this\.(on[A-Z]\w*)\s*=\s*opts\.", organic))
    missing = called - assigned
    assert not missing, f"invoked but never wired from opts: {sorted(missing)}"


@pytest.mark.parametrize("cb", ["onSelect", "onHover", "onFocusChange", "onDrop", "onLod"])
def test_named_callbacks_are_wired(cb):
    organic = (STATIC / "organic.js").read_text()
    assert re.search(rf"this\.{cb}\s*=\s*opts\.{cb}", organic)


def test_hud_passes_the_lod_callback_when_constructing_the_graph():
    ctor = HUD_JS.split("new OrganicGraph(", 1)[1][:400]
    assert "onLod" in ctor
