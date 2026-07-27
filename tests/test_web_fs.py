"""Graph-FM HTTP surface: routing, error codes, content types, sandboxing."""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))


@pytest.fixture()
def server(tmp_path, monkeypatch):
    """A live daemon on a loopback port with the sandbox pinned to tmp_path."""
    import threading
    from http.server import ThreadingHTTPServer

    monkeypatch.setenv("AION_FS_ROOT", str(tmp_path))
    monkeypatch.setenv("AION_FS_DIR", str(tmp_path))
    monkeypatch.setenv("AION_HOME", str(tmp_path / "aionhome"))
    import aion_web

    # A minimal fleet so the process-graph routes have something to report.
    inst = tmp_path / "aionhome" / "instances" / "solo"
    inst.mkdir(parents=True, exist_ok=True)
    (inst / "meta.json").write_text(json.dumps(
        {"id": "solo", "pid": os.getpid(), "hostname": "testbox",
         "active_harness": "demo", "running_count": 1}))
    (inst / "session.json").write_text(json.dumps([
        {"id": "t0001", "label": "Demo Harness: pump job", "harness": "demo",
         "state": "running", "progress": 0.5, "log": ["boiling the kettle"]}]))

    (tmp_path / "notes").mkdir(exist_ok=True)
    (tmp_path / "alpha.py").write_text("rocket thrust nozzle chamber\n" * 3)
    (tmp_path / "beta.py").write_text("rocket thrust nozzle ignition\n" * 3)
    (tmp_path / "gamma.md").write_text("tomato basil compost garden\n" * 3)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), aion_web.Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base, tmp_path
    httpd.shutdown()


def get(base, path):
    with urllib.request.urlopen(base + path, timeout=10) as r:
        return r.status, json.loads(r.read()), dict(r.headers)


def get_raw(base, path):
    with urllib.request.urlopen(base + path, timeout=10) as r:
        return r.status, r.read(), dict(r.headers)


def expect_error(base, path, code):
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(base + path, timeout=10)
    assert ei.value.code == code
    return json.loads(ei.value.read())


def post(base, path, payload):
    req = urllib.request.Request(
        base + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


# ── graph ────────────────────────────────────────────────────────────────
def test_graph_route_returns_the_physis_contract(server):
    base, tmp = server
    status, g, _ = get(base, f"/api/fs/graph?dir={tmp}")
    assert status == 200
    assert set(g) >= {"root", "themes", "files", "edges", "file_edges"}
    assert {f["title"] for f in g["files"]} >= {"alpha.py", "beta.py", "gamma.md"}


def test_graph_rejects_a_path_outside_the_sandbox(server):
    base, _ = server
    body = expect_error(base, "/api/fs/graph?dir=/etc", 403)
    assert "outside scan root" in body["error"]


def test_graph_clamps_a_junk_depth_instead_of_crashing(server):
    base, tmp = server
    status, g, _ = get(base, f"/api/fs/graph?dir={tmp}&depth=banana")
    assert status == 200 and g["files"]


def test_graph_clamps_an_absurd_limit(server):
    base, tmp = server
    status, g, _ = get(base, f"/api/fs/graph?dir={tmp}&limit=999999")
    assert status == 200


# ── preview ──────────────────────────────────────────────────────────────
def test_file_preview_reads_inside_the_sandbox(server):
    base, tmp = server
    status, j, _ = get(base, f"/api/fs/file?path={tmp / 'gamma.md'}")
    assert status == 200 and "tomato" in j["content"]


def test_file_preview_refuses_to_leave_the_sandbox(server):
    base, _ = server
    expect_error(base, "/api/fs/file?path=/etc/passwd", 403)


def test_file_preview_on_a_missing_path_is_a_400_not_a_500(server):
    base, tmp = server
    expect_error(base, f"/api/fs/file?path={tmp / 'nope.txt'}", 400)


# ── listing twin ─────────────────────────────────────────────────────────
def test_listing_is_the_same_scan_projected_flat(server):
    base, tmp = server
    _, g, _ = get(base, f"/api/fs/graph?dir={tmp}")
    _, lst, _ = get(base, f"/api/fs/listing?dir={tmp}")
    assert len(lst["rows"]) == len(g["files"])
    assert all({"hub", "title", "nearest"} <= set(r) for r in lst["rows"])


# ── roots ────────────────────────────────────────────────────────────────
def test_roots_never_advertises_a_path_outside_the_sandbox(server):
    base, tmp = server
    _, j, _ = get(base, "/api/fs/roots")
    assert j["root"] == str(tmp)
    for r in j["roots"]:
        assert r["path"].startswith(str(tmp))


# ── move ─────────────────────────────────────────────────────────────────
def test_move_renames_inside_the_sandbox(server):
    base, tmp = server
    code, j = post(base, "/api/fs/move",
                   {"from": str(tmp / "alpha.py"), "to": str(tmp / "renamed.py")})
    assert code == 200 and (tmp / "renamed.py").exists()
    assert j["to"] == str(tmp / "renamed.py")


def test_move_refuses_to_write_outside_the_sandbox(server):
    base, tmp = server
    code, j = post(base, "/api/fs/move",
                   {"from": str(tmp / "beta.py"), "to": "/tmp/aion_escape.py"})
    assert code == 403 and "outside scan root" in j["error"]
    assert (tmp / "beta.py").exists()
    assert not os.path.exists("/tmp/aion_escape.py")


def test_move_refuses_to_clobber(server):
    base, tmp = server
    code, j = post(base, "/api/fs/move",
                   {"from": str(tmp / "beta.py"), "to": str(tmp / "gamma.md")})
    assert code == 400 and "exists" in j["error"]
    assert (tmp / "gamma.md").read_text().startswith("tomato")


# ── static assets ────────────────────────────────────────────────────────
@pytest.mark.parametrize("path,ctype", [
    ("/static/hud.css", "text/css"),
    ("/static/hud.js", "text/javascript"),
    ("/static/organic.js", "text/javascript"),
])
def test_static_assets_are_served_with_a_usable_content_type(server, path, ctype):
    """A stylesheet sent as octet-stream is dropped; a script may be refused."""
    base, _ = server
    status, body, headers = get_raw(base, path)
    assert status == 200 and body
    assert headers["Content-Type"].startswith(ctype)
    assert headers.get("X-Content-Type-Options") == "nosniff"


# ── process graph ────────────────────────────────────────────────────────
def test_agents_route_reports_the_fleet(server):
    base, _ = server
    status, a, _ = get(base, "/api/agents")
    assert status == 200
    assert a["summary"]["instances"] == 1
    assert a["summary"]["live_instances"] == 1
    assert any(t["label"].startswith("Demo Harness") for t in a["tasks"])


def test_agents_route_can_hide_finished_work(server):
    base, _ = server
    _, a, _ = get(base, "/api/agents?finished=0")
    assert all(t["state"] in ("running", "pending") for t in a["tasks"])


def test_agents_route_never_500s_on_a_broken_fleet(server, tmp_path):
    """A corrupt checkpoint must degrade to an empty graph, not an error page."""
    (tmp_path / "aionhome" / "instances" / "solo" / "session.json").write_text("{{{")
    base, _ = server
    status, a, _ = get(base, "/api/agents")
    assert status == 200 and a["tasks"] == []


# ── unified search ───────────────────────────────────────────────────────
def test_search_spans_every_corpus(server, tmp_path):
    base, _ = server
    _, r, _ = get(base, f"/api/search/all?q=pump&dir={tmp_path}")
    types = {h["type"] for h in r["results"]}
    assert "task" in types, "process graph not searched"


def test_search_finds_files_by_name(server, tmp_path):
    base, _ = server
    _, r, _ = get(base, f"/api/search/all?q=gamma&dir={tmp_path}")
    assert any(h["type"] == "file" and h["label"] == "gamma.md" for h in r["results"])


def test_search_reaches_into_file_contents(server, tmp_path):
    """The whole point of a search box you trust: find it by what it says."""
    base, _ = server
    _, r, _ = get(base, f"/api/search/all?q=basil&dir={tmp_path}")
    hits = [h for h in r["results"] if h["type"] == "file"]
    assert hits and any("contains" in h["sub"] for h in hits)


def test_search_finds_modules_so_navigation_is_searchable(server):
    base, _ = server
    _, r, _ = get(base, "/api/search/all?q=latex")
    assert any(h["type"] == "module" and h["module"] == "latex" for h in r["results"])


def test_every_search_hit_carries_jump_coordinates(server, tmp_path):
    base, _ = server
    _, r, _ = get(base, f"/api/search/all?q=demo&dir={tmp_path}")
    assert r["results"]
    for h in r["results"]:
        assert h["label"] and h["module"]


def test_empty_search_returns_no_results(server):
    base, _ = server
    _, r, _ = get(base, "/api/search/all?q=%20")
    assert r["results"] == []


def test_search_results_are_capped(server, tmp_path):
    base, _ = server
    _, r, _ = get(base, f"/api/search/all?q=a&dir={tmp_path}")
    assert len(r["results"]) <= 60


def test_search_survives_a_bad_scan_dir(server):
    """A stale `dir` from a deep link must not take the whole palette down."""
    base, _ = server
    status, r, _ = get(base, "/api/search/all?q=vault&dir=/nonexistent/xyz")
    assert status == 200
    # the file corpus is unreachable, but every other corpus still answers
    assert any(h["type"] == "module" for h in r["results"])
    assert all(h["type"] != "file" for h in r["results"])


def test_static_assets_carry_the_live_channel_wiring(server):
    """The HUD must actually open the socket the daemon serves."""
    base, _ = server
    _, js, _ = get_raw(base, "/static/hud.js")
    js = js.decode()
    assert "/ws/events" in js
    assert "connectEvents" in js and "applyAgentEvent" in js
    # port is derived from the HTTP port, matching WS_PORT in aion_web.py
    assert "location.port" in js


def test_index_references_every_asset_the_worker_precaches(server):
    """The shell must not ship a <link>/<script> the offline cache misses."""
    base, _ = server
    _, html, _ = get_raw(base, "/")
    html = html.decode()
    _, sw, _ = get_raw(base, "/sw.js")
    sw = sw.decode()
    for asset in ("/static/hud.css", "/static/organic.js", "/static/hud.js"):
        assert asset in html, f"{asset} not referenced by index.html"
        assert asset in sw, f"{asset} not precached by sw.js"
