"""Graph file manager engine — clustering, sandboxing, file ops."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from aion import fsgraph  # noqa: E402


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    """A directory with two obvious topics + noise, so clustering has a job."""
    (tmp_path / "rocket").mkdir()
    (tmp_path / "garden").mkdir()
    for i in range(4):
        (tmp_path / "rocket" / f"engine_{i}.py").write_text(
            "thrust nozzle combustion chamber propellant ignition\n" * 4)
    for i in range(4):
        (tmp_path / "garden" / f"tomato_{i}.md").write_text(
            "tomato basil compost watering seedling harvest\n" * 4)
    (tmp_path / "readme.md").write_text("mixed notes")
    return tmp_path


# ── sandbox ──────────────────────────────────────────────────────────────────
def test_resolve_accepts_paths_inside_root(tmp_path: Path):
    (tmp_path / "a" / "b").mkdir(parents=True)
    got = fsgraph.resolve_in_root(str(tmp_path / "a" / "b"), tmp_path)
    assert got == (tmp_path / "a" / "b").resolve()


def test_resolve_rejects_escape_via_dotdot(tmp_path: Path):
    inner = tmp_path / "inner"
    inner.mkdir()
    with pytest.raises(fsgraph.FsGraphError):
        fsgraph.resolve_in_root(str(inner / ".." / ".."), inner)


def test_resolve_rejects_symlink_pointing_outside(tmp_path: Path):
    """Containment is checked AFTER resolve(), so a symlink cannot tunnel out."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("s3cret")
    root = tmp_path / "root"
    root.mkdir()
    try:
        (root / "escape").symlink_to(outside / "secret.txt")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(fsgraph.FsGraphError):
        fsgraph.resolve_in_root(str(root / "escape"), root)


def test_scan_root_defaults_to_home(monkeypatch):
    monkeypatch.delenv("AION_FS_ROOT", raising=False)
    assert fsgraph.scan_root() == Path(os.path.expanduser("~")).resolve()


# ── tokenising ───────────────────────────────────────────────────────────────
def test_tokenize_splits_camel_case_and_drops_stopwords():
    got = fsgraph.tokenize("parseHTTPResponse_main.py")
    assert "parse" in got
    assert "response" in got
    assert "main" not in got          # stopword
    assert "py" not in got            # stopword


def test_kind_of_classifies_by_extension(tmp_path: Path):
    (tmp_path / "x.py").write_text("")
    (tmp_path / "x.png").write_text("")
    (tmp_path / "x.qqq").write_text("")
    assert fsgraph.kind_of(tmp_path / "x.py") == "code"
    assert fsgraph.kind_of(tmp_path / "x.png") == "image"
    assert fsgraph.kind_of(tmp_path / "x.qqq") == "other"
    assert fsgraph.kind_of(tmp_path) == "dir"


# ── walk ─────────────────────────────────────────────────────────────────────
def test_walk_is_breadth_first_so_the_cap_keeps_the_top_of_the_tree(tmp_path: Path):
    deep = tmp_path / "deep"
    deep.mkdir()
    for i in range(20):
        (deep / f"buried_{i}.txt").write_text("x")
    (tmp_path / "top.txt").write_text("x")
    got = fsgraph.walk(tmp_path, depth=3, max_files=3)
    assert (tmp_path / "top.txt") in got   # shallow file survives the cap


def test_walk_skips_hidden_unless_asked(tmp_path: Path):
    (tmp_path / ".hidden").write_text("x")
    (tmp_path / "shown").write_text("x")
    assert len(fsgraph.walk(tmp_path)) == 1
    assert len(fsgraph.walk(tmp_path, include_hidden=True)) == 2


# ── graph ────────────────────────────────────────────────────────────────────
def test_graph_shape_matches_the_physis_contract(tree: Path):
    g = fsgraph.graph(str(tree), root=tree)
    assert set(g) >= {"root", "themes", "files", "edges", "file_edges", "source"}
    assert g["source"] == "local"
    assert len(g["files"]) == 9
    assert g["themes"], "clustering produced no hubs"
    for e in g["edges"]:
        assert set(e) == {"theme_id", "file_id", "score"}
    for e in g["file_edges"]:
        assert set(e) == {"source", "target", "score"}


def test_graph_clusters_related_files_together(tree: Path):
    """The two topics must not land in one hub — that is the whole feature."""
    g = fsgraph.graph(str(tree), root=tree, k=2)
    top = {}
    for e in sorted(g["edges"], key=lambda e: -e["score"]):
        top.setdefault(e["file_id"], e["theme_id"])
    by_path = {f["id"]: f["path"] for f in g["files"]}
    rockets = {top[i] for i, p in by_path.items() if "engine_" in p}
    tomatoes = {top[i] for i, p in by_path.items() if "tomato_" in p}
    assert len(rockets) == 1 and len(tomatoes) == 1
    assert rockets != tomatoes


def test_graph_is_deterministic(tree: Path):
    """Clusters that reshuffle on refresh make the view unusable."""
    a = fsgraph.graph(str(tree), root=tree)
    b = fsgraph.graph(str(tree), root=tree)
    assert a["edges"] == b["edges"]
    assert [t["name"] for t in a["themes"]] == [t["name"] for t in b["themes"]]


# ── what "related" means: content leads ──────────────────────────────────────
TOPICS = {
    "rocket": "thrust nozzle combustion chamber propellant ignition turbopump",
    "garden": "tomato basil compost watering seedling harvest mulch trellis",
    "finance": "invoice ledger accrual amortise dividend liquidity payable",
    "music": "reverb timbre cadence arpeggio syncopation harmonic tremolo",
}


def _corpus_dir(tmp_path: Path) -> Path:
    """A clean subdirectory. `conftest.isolate_aion_home` puts a `home/` tree
    inside tmp_path, so scanning tmp_path directly picks up its files too."""
    d = tmp_path / "corpus"
    d.mkdir()
    return d


def _crosscut(root: Path, name_tokens: int, content_lines: int) -> dict:
    """A name x content grid where the two signals disagree by construction.

    One file per (name topic, content topic) pair, so a file's name-siblings
    and content-siblings are disjoint — "which does it cluster with" is then
    a real question rather than a tautology.
    """
    truth = {}
    keys = list(TOPICS)
    for ni, nk in enumerate(keys):
        for ci, ck in enumerate(keys):
            stem = "_".join(TOPICS[nk].split()[:name_tokens])
            p = root / f"{stem}_{ni}{ci}.md"
            p.write_text((TOPICS[ck] + "\n") * content_lines)
            truth[p.name] = ck
    return truth


def _nearest_agrees_with_content(root: Path, truth: dict) -> float:
    paths = sorted(root.iterdir())
    vecs = fsgraph._tfidf([fsgraph._terms(p, root) for p in paths])
    hits = 0
    for i, pi in enumerate(paths):
        best, bj = -2.0, None
        for j in range(len(paths)):
            if i != j:
                s = fsgraph.cosine(vecs[i], vecs[j])
                if s > best:
                    best, bj = s, j
        if truth[pi.name] == truth[paths[bj].name]:
            hits += 1
    return hits / len(paths)


@pytest.mark.parametrize("name_tokens,content_lines", [(2, 6), (4, 2), (6, 1)])
def test_content_outranks_filename(tmp_path: Path, name_tokens, content_lines):
    """A misleadingly-named file must cluster with what it actually says.

    The (6, 1) case is the one that matters: a verbose filename against a
    single line of body text. Under the original name-weighted config that
    case scored 0.00 — the name won outright.
    """
    root = _corpus_dir(tmp_path)
    truth = _crosscut(root, name_tokens, content_lines)
    assert _nearest_agrees_with_content(root, truth) == 1.0


def test_content_free_files_still_cluster_by_name(tmp_path: Path):
    """Images, media and archives have no readable body — name/dir/ext are the
    only signal they have, so those weights must never drop to zero."""
    root = _corpus_dir(tmp_path)
    for i in range(5):
        (root / f"rocket_engine_{i}.png").write_bytes(b"\x89PNG" + bytes(200))
        (root / f"garden_tomato_{i}.mp3").write_bytes(b"ID3" + bytes(200))
    g = fsgraph.graph(str(root), root=root, k=2)
    top = {}
    for e in sorted(g["edges"], key=lambda e: -e["score"]):
        top.setdefault(e["file_id"], e["theme_id"])
    name = {f["id"]: Path(f["path"]).name for f in g["files"]}
    buckets: dict[int, list[str]] = {}
    for fid, hub in top.items():
        buckets.setdefault(hub, []).append(
            "rocket" if "rocket" in name[fid] else "garden")
    purity = sum(max(v.count(x) for x in set(v)) for v in buckets.values())
    assert purity == sum(len(v) for v in buckets.values())


def test_content_weight_leads_the_others(tmp_path: Path):
    """Guards the intent, not just the effect: content is the top weight."""
    assert fsgraph.W_CONTENT > fsgraph.W_NAME > fsgraph.W_DIR > fsgraph.W_EXT > 0


def test_similarity_mesh_stays_populated(tmp_path: Path):
    """The cosine floor has to track the vector distribution.

    A floor tuned for name-weighted vectors sits above the p99 of
    content-weighted ones, and the file<->file mesh silently empties — the
    graph still renders, just with no relationships in it.
    """
    root = _corpus_dir(tmp_path)
    for topic, words in TOPICS.items():
        for i in range(6):
            (root / f"{topic}_{i}.md").write_text((words + "\n") * 4)
    g = fsgraph.graph(str(root), root=root)
    per_file = len(g["file_edges"]) / len(g["files"])
    assert per_file >= 0.8, f"similarity mesh collapsed: {per_file:.2f} edges/file"


def test_graph_similarity_edges_link_siblings_not_strangers(tree: Path):
    g = fsgraph.graph(str(tree), root=tree)
    by_id = {f["id"]: f["path"] for f in g["files"]}
    pairs = [(by_id[e["source"]], by_id[e["target"]]) for e in g["file_edges"]]
    assert pairs, "no similarity edges at all"
    crossings = [(a, b) for a, b in pairs
                 if ("engine_" in a) != ("engine_" in b)
                 and "readme" not in a and "readme" not in b]
    assert not crossings, f"unrelated files linked: {crossings}"


def test_graph_marks_truncation(tmp_path: Path):
    for i in range(12):
        (tmp_path / f"f{i}.txt").write_text("x")
    g = fsgraph.graph(str(tmp_path), root=tmp_path, max_files=5)
    assert g["truncated"] is True
    assert len(g["files"]) == 5


def test_graph_on_empty_dir_returns_empty_payload(tmp_path: Path):
    g = fsgraph.graph(str(tmp_path), root=tmp_path)
    assert g["files"] == [] and g["themes"] == []


def test_graph_rejects_a_file_target(tmp_path: Path):
    f = tmp_path / "x.txt"
    f.write_text("x")
    with pytest.raises(fsgraph.FsGraphError):
        fsgraph.graph(str(f), root=tmp_path)


def test_graph_survives_an_unreadable_file(tmp_path: Path):
    """A permission error on one file must not kill the whole scan."""
    good = tmp_path / "good.md"
    good.write_text("hello world content")
    bad = tmp_path / "bad.md"
    bad.write_text("nope")
    bad.chmod(0o000)
    try:
        g = fsgraph.graph(str(tmp_path), root=tmp_path)
        assert len(g["files"]) == 2
    finally:
        bad.chmod(0o644)


# ── file ops ─────────────────────────────────────────────────────────────────
def test_preview_truncates_and_reports_size(tmp_path: Path):
    f = tmp_path / "big.txt"
    f.write_text("a" * 100)
    got = fsgraph.preview(str(f), limit=10, root=tmp_path)
    assert got["content"] == "a" * 10
    assert got["truncated"] is True and got["size"] == 100


def test_preview_refuses_outside_root(tmp_path: Path):
    inner = tmp_path / "inner"
    inner.mkdir()
    (tmp_path / "outer.txt").write_text("x")
    with pytest.raises(fsgraph.FsGraphError):
        fsgraph.preview(str(tmp_path / "outer.txt"), root=inner)


def test_preview_decodes_binary_lossily(tmp_path: Path):
    f = tmp_path / "b.bin"
    f.write_bytes(b"\xff\xfe\x00ok")
    assert "ok" in fsgraph.preview(str(f), root=tmp_path)["content"]


def test_move_renames_within_root(tmp_path: Path):
    src = tmp_path / "a.txt"
    src.write_text("x")
    got = fsgraph.move(str(src), str(tmp_path / "b.txt"), root=tmp_path)
    assert got["to"] == str(tmp_path / "b.txt")
    assert (tmp_path / "b.txt").exists() and not src.exists()


def test_move_refuses_to_overwrite(tmp_path: Path):
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    with pytest.raises(fsgraph.FsGraphError):
        fsgraph.move(str(tmp_path / "a.txt"), str(tmp_path / "b.txt"), root=tmp_path)
    assert (tmp_path / "b.txt").read_text() == "b"


def test_move_refuses_destination_outside_root(tmp_path: Path):
    inner = tmp_path / "inner"
    inner.mkdir()
    src = inner / "a.txt"
    src.write_text("x")
    with pytest.raises(fsgraph.FsGraphError):
        fsgraph.move(str(src), str(tmp_path / "escaped.txt"), root=inner)
    assert src.exists()


def test_move_refuses_dotdot_in_destination(tmp_path: Path):
    """A non-existent destination cannot be resolve()d — its parent is."""
    inner = tmp_path / "inner"
    inner.mkdir()
    src = inner / "a.txt"
    src.write_text("x")
    with pytest.raises(fsgraph.FsGraphError):
        fsgraph.move(str(src), str(inner / ".." / "out.txt"), root=inner)
    assert src.exists()
