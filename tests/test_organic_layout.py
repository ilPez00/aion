"""Force-layout quality gate for scripts/static/organic.js.

The graph renderer is the only part of the HUD with real algorithmic content,
and "it looked fine when I opened it" is not a regression test. This drives
the actual module through node with a DOM shim (tests/organic_harness.js) and
asserts the properties that make the picture *mean* something:

  * no NaN coordinates (one divide-by-zero and the graph vanishes silently)
  * bounded span (an exploding layout renders as an empty screen once fitted)
  * hubs stay apart (collapsed hubs = overlapping, unreadable clusters)
  * files land nearest their OWN cluster — proximity has to encode membership,
    otherwise the visualisation is decoration
  * it settles fast enough to feel instant, and stops (idle CPU matters when
    this is pinned on a second monitor all day)

Skipped when node is unavailable; the Python engine tests still cover the
data, so this is quality-of-render, not correctness-of-scan.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aion import fsgraph  # noqa: E402

HARNESS = Path(__file__).parent / "organic_harness.js"
ORGANIC = ROOT / "scripts" / "static" / "organic.js"

# Mirrors LOD_HYSTERESIS in organic.js: a node already drawn keeps its slot
# until it is well past the budget, so the drawn count can sit slightly over.
LOD_HYSTERESIS = 1.18

pytestmark = pytest.mark.skipif(
    not shutil.which("node"), reason="node not installed; layout gate skipped")


def layout(payload: dict, reduced: bool = False) -> dict:
    env = {**os.environ, "ORGANIC_JS": str(ORGANIC),
           "REDUCED_MOTION": "1" if reduced else "0"}
    proc = subprocess.run(
        ["node", str(HARNESS)], input=json.dumps(payload), env=env,
        capture_output=True, text=True, timeout=90)
    assert proc.returncode == 0, f"harness failed:\n{proc.stderr[-2000:]}"
    return json.loads(proc.stdout)


@pytest.fixture(scope="module")
def repo_graph():
    """A real scan of this repo's source — the shape the HUD actually renders."""
    return fsgraph.graph(str(ROOT / "src"), depth=3, root=ROOT)


@pytest.fixture(scope="module")
def synthetic():
    """Four unambiguous clusters — layout has no excuse to mix these up."""
    themes = [{"id": i, "name": f"c{i}", "domain": "discovered",
               "mode": "", "category": None} for i in range(4)]
    files, edges, sims = [], [], []
    for c in range(4):
        ids = []
        for j in range(10):
            fid = c * 10 + j
            ids.append(fid)
            files.append({"id": fid, "path": f"/c{c}/f{j}", "title": f"c{c}f{j}",
                          "kind": "code", "size": 100, "mtime": 0, "depth": 1})
            edges.append({"theme_id": c, "file_id": fid, "score": 0.9})
        for a in ids:
            for b in ids:
                if a < b:
                    sims.append({"source": a, "target": b, "score": 0.8})
    return {"root": "/", "source": "local", "truncated": False,
            "themes": themes, "files": files, "edges": edges, "file_edges": sims}


# ── numerical health ─────────────────────────────────────────────────────
def test_layout_produces_only_finite_coordinates(repo_graph):
    assert layout(repo_graph)["finite"] is True


def test_layout_does_not_explode_or_collapse(repo_graph):
    span = layout(repo_graph)["span"]
    assert 120 < span < 20000, f"degenerate layout span {span}px"


def test_empty_graph_does_not_throw(repo_graph):
    assert layout(repo_graph)["emptyOk"] is True


def test_layout_survives_a_graph_with_no_edges():
    payload = {"root": "/", "source": "local", "truncated": False, "themes": [],
               "files": [{"id": i, "path": f"/f{i}", "title": f"f{i}", "kind": "other",
                          "size": 0, "mtime": 0, "depth": 0} for i in range(20)],
               "edges": [], "file_edges": []}
    out = layout(payload)
    assert out["finite"] is True and out["nodes"] == 20


# ── readability ──────────────────────────────────────────────────────────
def test_hubs_stay_visually_separated(repo_graph):
    """Overlapping hubs make every cluster read as one blob."""
    out = layout(repo_graph)
    assert out["hubs"] >= 2
    assert out["minHubGap"] >= 60, f"hubs collapsed to {out['minHubGap']}px apart"


def test_position_encodes_cluster_membership(repo_graph):
    """The core claim of the view: things near each other belong together."""
    out = layout(repo_graph)
    assert out["agreePct"] >= 70, (
        f"only {out['agreePct']}% of files land nearest their own cluster "
        f"({out['agree']}/{out['total']}) — proximity is not encoding membership")


def test_unambiguous_clusters_are_laid_out_almost_perfectly(synthetic):
    out = layout(synthetic)
    assert out["agreePct"] >= 90, f"clean 4-cluster case only reached {out['agreePct']}%"


# ── cost ─────────────────────────────────────────────────────────────────
def test_layout_settles_quickly(repo_graph):
    """Perceived-instant on a real directory; also proves alpha cools to a stop."""
    assert layout(repo_graph)["settleMs"] < 3000


def test_layout_scales_to_the_file_cap():
    """600 files is the scan cap — the spatial hash has to hold up there."""
    n = 600
    themes = [{"id": i, "name": f"c{i}", "domain": "discovered", "mode": "",
               "category": None} for i in range(12)]
    files = [{"id": i, "path": f"/d{i % 12}/f{i}", "title": f"f{i}", "kind": "code",
              "size": 10, "mtime": 0, "depth": 1} for i in range(n)]
    edges = [{"theme_id": i % 12, "file_id": i, "score": 0.7} for i in range(n)]
    sims = [{"source": i, "target": (i + 12) % n, "score": 0.6} for i in range(n)]
    out = layout({"root": "/", "source": "local", "truncated": True,
                  "themes": themes, "files": files, "edges": edges, "file_edges": sims})
    assert out["finite"] is True
    assert out["settleMs"] < 8000, f"600-node layout took {out['settleMs']}ms"


# ── accessibility ────────────────────────────────────────────────────────
def test_reduced_motion_presents_a_settled_graph_immediately(repo_graph):
    """With reduced motion the graph must arrive solved, not mid-animation."""
    out = layout(repo_graph, reduced=True)
    assert out["finite"] is True and out["agreePct"] >= 60


def test_graph_describes_itself_for_screen_readers(repo_graph):
    """The canvas is a silent region otherwise — the description has to state
    both the shape of the data and every way to move through it."""
    d = layout(repo_graph)["describe"].lower()
    assert "nodes" in d and "clusters" in d and "links" in d
    for control in ("arrow keys", "tab", "enter"):
        assert control in d, f"description never mentions {control!r}"


# ── level of detail ──────────────────────────────────────────────────────────
# The renderer draws a fixed number of nodes per unit of screen area and defers
# the rest. These gate the two ways that goes wrong: showing so much it is
# still a wall of dots, or hiding so much the graph stops being navigable.


@pytest.fixture(scope="module")
def crowded():
    """A graph well past the budget — 12 clusters, 600 files.

    The repo's own source scans to ~87 nodes, which fits on screen whole, so
    it cannot exercise LOD at all. This is the size that made the feature
    necessary: a `~/dev` scan, a big vault, a fleet mid-run.
    """
    themes = [{"id": c, "name": f"cluster{c}", "domain": "discovered",
               "mode": "", "category": None} for c in range(12)]
    files, edges, sims = [], [], []
    for c in range(12):
        ids = []
        for j in range(50):
            fid = c * 50 + j
            ids.append(fid)
            files.append({"id": fid, "path": f"/c{c}/f{j}.py", "title": f"c{c}f{j}",
                          "kind": "code", "size": 100, "mtime": 0, "depth": 2})
            edges.append({"theme_id": c, "file_id": fid, "score": 0.9})
        for a, b in zip(ids, ids[1:]):
            sims.append({"source": a, "target": b, "score": 0.5})
    return {"root": "/synthetic", "source": "local", "truncated": False,
            "themes": themes, "files": files, "edges": edges, "file_edges": sims}


def test_zooming_out_actually_reduces_what_is_drawn(crowded):
    """Otherwise the feature does nothing and the screen stays a texture."""
    wide = layout(crowded)["lodSweep"][0]
    assert wide["inView"] > wide["budget"], "corpus too small to exercise LOD"
    assert wide["drawn"] < wide["inView"]


def test_zooming_in_reveals_a_larger_share_of_what_is_on_screen(crowded):
    """The user-facing promise: new elements appear as there is room for them.

    The raw drawn count is NOT the property — it falls at high zoom simply
    because less of the graph is in the viewport. What has to rise is the
    fraction of on-screen nodes that survive the budget.
    """
    sweep = layout(crowded)["lodSweep"]
    shares = [round(s["shown"], 3) for s in sweep]
    assert shares[-1] >= 0.999, f"never reaches full detail: {shares}"
    assert shares[-1] > shares[0], f"zooming in revealed nothing: {shares}"
    # Monotone, allowing a hair of slack for the hysteresis deadband.
    for a, b in zip(shares, shares[1:]):
        assert b >= a - 0.02, f"detail went backwards on zoom: {shares}"


def test_density_stays_within_budget(crowded):
    """Constant screen-space density is the whole mechanism; if the drawn count
    can exceed the budget the clutter comes straight back."""
    out = layout(crowded)
    # Hubs are exempt: they are the map, not detail on it. So the ceiling is
    # the budget, plus the hysteresis deadband, plus the hub count — and that
    # exemption has to stay small relative to the budget or it becomes the
    # clutter it was meant to prevent.
    ceiling = out["lodSweep"][0]["budget"] * LOD_HYSTERESIS + out["hubs"]
    assert out["hubs"] < out["lodSweep"][0]["budget"] * 0.25
    for s in out["lodSweep"]:
        assert s["drawn"] <= ceiling, f"{s['drawn']} drawn, ceiling {ceiling:.0f}"


def test_hubs_are_never_culled(crowded):
    """A hub is a cluster's name. Dropping one deletes a region of the map
    rather than a detail inside it."""
    for s in layout(crowded)["lodSweep"]:
        assert s["hubsCulled"] == 0


def test_selection_is_never_culled(crowded):
    """Search jumps to a node and selects it. If the budget then hides it, the
    HUD has answered a query with a blank patch of canvas."""
    assert layout(crowded)["selectionSurvives"] is True


def test_small_graphs_are_drawn_whole(synthetic):
    """44 nodes fit comfortably; LOD must not fire when there is nothing to
    thin out, or it becomes a tax on every small view."""
    out = layout(synthetic)
    fitted = out["lodSweep"][2]          # k = 1, i.e. as fitted
    assert fitted["drawn"] == fitted["inView"]


def test_hidden_count_is_announced(crowded):
    """The list view stays complete, and the description says so — a screen
    reader user must not be told the graph is smaller than it is."""
    out = layout(crowded)
    desc = out["describeText"]
    assert f"{out['nodes']} nodes" in desc
    assert "hidden at this zoom" in desc
    assert "list view" in desc
