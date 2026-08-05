"""fsgraph.py — the graph file manager engine.

Turns a directory into an *organic* graph: files as nodes, emergent topic
hubs as attractors, and file<->file similarity edges. Pure Python, zero
dependencies, fully testable without a filesystem fixture beyond tmp_path.

Why this exists
---------------
physis_pro ships a graph file manager (`src/bin/ui/graph_fm.html` +
`/api/v1/fs/*`) whose clustering runs on BGE embeddings inside a Rust
process. That is a *great* view of a directory and a terrible hard
dependency: aion has to boot on a laptop with no physis engine, no ONNX
runtime and no model download.

So this module reimplements the same **contract** with local maths:

    physis  : BGE vectors -> semiotic cells / k-means -> theme<->file graph
    fsgraph : TF-IDF over path+content tokens -> spherical k-means -> same graph

The response shape is deliberately byte-compatible with physis's
`FsGraphResponse` (`themes` / `files` / `edges` / `file_edges`), so one
frontend renders either backend and `source` just says which brain ran.
When the physis engine *is* up, `graph(..., prefer_physis=True)` proxies to
it and you get the better embeddings for free.

Security
--------
Every caller-supplied path goes through `resolve_in_root()`, which
canonicalises (resolving symlinks) and then proves containment in the scan
root. The web HUD is LAN-reachable, so a path escape here is a file-read
primitive for whoever else is on the WiFi.
"""
from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# ── tuning ───────────────────────────────────────────────────────────────────
MAX_FILES = 600          # hard cap: above this the graph stops being readable
CONTENT_HEAD = 4096      # bytes of each text file fed to the vectoriser

# Term weights — see `_terms()` for the full rationale. Content leads: what a
# file says outranks what it was named. Name/dir/ext stay non-zero because
# they are the only signal a binary has.
W_CONTENT = 4.0
W_NAME = 1.5
W_DIR = 1.0
W_EXT = 0.8
TOP_K_HUBS = 2           # hub edges kept per file
MIN_HUB_SCORE = 0.05     # below this a file is "unclustered", not forced in
SIM_TOP_K = 4            # nearest neighbours kept per file — the real control
# Absolute cosine floor. Deliberately low: SIM_TOP_K decides how dense the
# mesh is, and this only stops a corpus with nothing genuinely related from
# being wired together out of noise. It was 0.28 when name/dir/ext dominated
# the vectors; content-led vectors are far higher-dimensional, so the whole
# cosine distribution dropped (p99 ~0.21 on this repo's src/) and 0.28 sat
# above it — the similarity mesh silently vanished. Measured across four
# corpora, 0.10 is the knee: ~1-2.5 edges per file everywhere, saturating
# below it as the top-K cap takes over.
SIM_TAU = 0.10
KMEANS_ITERS = 24

# Extensions we will read the head of. Anything else is classified on its
# path/name alone — cheaper, and a 400MB .mkv has no tokens worth having.
TEXT_EXT = {
    ".md", ".txt", ".rst", ".org", ".py", ".js", ".ts", ".tsx", ".jsx", ".rs",
    ".go", ".c", ".h", ".cpp", ".hpp", ".java", ".kt", ".rb", ".sh", ".bash",
    ".zsh", ".fish", ".toml", ".yaml", ".yml", ".json", ".ini", ".cfg", ".conf",
    ".html", ".css", ".scss", ".sql", ".tex", ".csv", ".xml", ".lua", ".vim",
    ".dockerfile", ".makefile", ".gradle", ".swift", ".php", ".pl", ".r",
}

# Coarse kind, used for node glyphs + the list fallback view.
KIND_BY_EXT = {
    "code": {".py", ".js", ".ts", ".tsx", ".jsx", ".rs", ".go", ".c", ".h",
             ".cpp", ".hpp", ".java", ".kt", ".rb", ".sh", ".bash", ".zsh",
             ".lua", ".swift", ".php", ".pl", ".r", ".vim", ".sql"},
    "doc": {".md", ".txt", ".rst", ".org", ".tex", ".pdf", ".doc", ".docx",
            ".odt", ".epub"},
    "config": {".toml", ".yaml", ".yml", ".json", ".ini", ".cfg", ".conf",
               ".env", ".lock"},
    "image": {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp", ".ico",
              ".tif", ".tiff"},
    "media": {".mp3", ".wav", ".flac", ".ogg", ".mp4", ".mkv", ".mov", ".webm",
              ".avi", ".m4a"},
    "archive": {".zip", ".tar", ".gz", ".xz", ".zst", ".bz2", ".7z", ".rar"},
    "data": {".csv", ".tsv", ".parquet", ".db", ".sqlite", ".npy", ".pkl"},
}

# Tokens that appear in nearly every path and therefore separate nothing.
STOP = {
    "src", "lib", "bin", "usr", "home", "tmp", "var", "the", "and", "for",
    "with", "from", "this", "that", "new", "old", "test", "tests", "main",
    "index", "init", "py", "js", "ts", "md", "txt", "json", "self", "def",
    "import", "return", "none", "true", "false", "null", "class", "function",
    "const", "let", "www", "com", "http", "https", "org",
}

# Build/vendor dirs. Not hidden, so the hidden-files toggle misses them, and
# they are the loudest thing in a real repo: scanning aion's own src/ let
# __pycache__ take two of eight hubs and name one of them "cpython · pyc".
IGNORE_DIRS = {
    "__pycache__", "node_modules", ".git", ".venv", "venv", "target", "dist",
    "build", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".next", ".tox",
    "site-packages", ".egg-info", "vendor", "Cargo.lock",
}
IGNORE_EXT = {".pyc", ".pyo", ".so", ".o", ".a", ".class", ".lock", ".map"}

_SPLIT = re.compile(r"[^A-Za-z0-9]+")
# Two boundaries, because one is not enough: `parseHTTP` splits on lower->upper,
# but `HTTPResponse` only splits on upper->upper-lower. Without the second,
# acronym-prefixed identifiers stay welded into one useless token.
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


# ── data ─────────────────────────────────────────────────────────────────────
@dataclass
class FileNode:
    id: int
    path: str
    title: str
    kind: str
    size: int
    mtime: float
    depth: int
    vec: dict[str, float] = field(default_factory=dict, repr=False)


@dataclass
class Hub:
    """An emergent topic cluster — physis calls these `themes`."""
    id: int
    name: str
    domain: str = "discovered"
    mode: str = ""
    category: str | None = None
    centroid: dict[str, float] = field(default_factory=dict, repr=False)


class FsGraphError(ValueError):
    """Bad or out-of-sandbox path. Callers map this to 400/403."""


# ── sandbox ──────────────────────────────────────────────────────────────────
def scan_root() -> Path:
    """The one directory the graph FM may ever touch.

    Defaults to $HOME. Set `AION_FS_ROOT=/` to open the whole machine — an
    explicit, auditable opt-in rather than a silently permissive default.
    """
    return Path(os.environ.get("AION_FS_ROOT", os.path.expanduser("~"))).resolve()


def resolve_in_root(raw: str, root: Path | None = None) -> Path:
    """Canonicalise `raw` and prove it lives under the scan root.

    `Path.resolve()` follows symlinks *before* the containment check, so a
    symlink inside the root pointing at /etc/shadow resolves to /etc/shadow
    and is rejected — checking the pre-resolution string would not catch it.
    """
    root = (root or scan_root()).resolve()
    try:
        p = Path(raw).expanduser().resolve()
    except (OSError, RuntimeError) as e:      # RuntimeError: symlink loop
        raise FsGraphError(f"cannot resolve path: {e}") from e
    if p != root and root not in p.parents:
        raise FsGraphError(f"path outside scan root ({root})")
    return p


# ── tokenising ───────────────────────────────────────────────────────────────
def tokenize(text: str) -> list[str]:
    """Lowercased alnum tokens, camelCase split, stopwords and noise dropped."""
    out: list[str] = []
    for raw in _SPLIT.split(_CAMEL.sub(" ", text)):
        t = raw.lower()
        if len(t) < 3 or len(t) > 24 or t in STOP or t.isdigit():
            continue
        out.append(t)
    return out


def kind_of(path: Path) -> str:
    if path.is_dir():
        return "dir"
    ext = path.suffix.lower()
    for kind, exts in KIND_BY_EXT.items():
        if ext in exts:
            return kind
    return "other"


def _read_head(path: Path) -> str:
    if path.suffix.lower() not in TEXT_EXT and path.name.lower() not in (
            "makefile", "dockerfile", "readme"):
        return ""
    try:
        with open(path, "rb") as fh:
            return fh.read(CONTENT_HEAD).decode("utf-8", "ignore")
    except OSError:
        return ""


def _terms(path: Path, root: Path) -> dict[str, float]:
    """Weighted term counts for one file.

    These weights *are* the definition of "related" in this module, so they
    are named constants rather than magic numbers buried in the calls.
    Content leads: what a file says outranks what it was named, so a
    misleadingly-named file still clusters with its actual subject.

    The counterweight is that content is by far the most *plentiful* signal
    — a 4KB head yields hundreds of tokens against a filename's two or three.
    Two mechanisms stop sheer volume from deciding membership:

      * `_tfidf` applies sublinear tf, so a token repeated 200 times in one
        file counts a little more than one repeated twice, not 100x more.
      * every vector is L2-normalised, so a long file and a short one carry
        the same total weight into the cosine — length changes direction,
        never magnitude.

    Even so, name/dir/ext are not pushed to zero: for the many files with no
    readable content at all (images, media, archives, binaries) they are the
    *only* signal, and a weight of zero would collapse every such file into
    one undifferentiated blob. Tuned on a corpus where filename and content
    deliberately disagree — see `tests/test_fsgraph.py::test_content_outranks_*`.
    """
    bag: dict[str, float] = {}

    def add(tokens, w):
        if w <= 0:
            return
        for t in tokens:
            bag[t] = bag.get(t, 0.0) + w

    add(tokenize(path.stem), W_NAME)
    try:
        rel = path.relative_to(root)
        add(tokenize("/".join(rel.parts[:-1])), W_DIR)
    except ValueError:
        pass
    ext = path.suffix.lower().lstrip(".")
    if ext and W_EXT > 0:
        bag[f"ext:{ext}"] = bag.get(f"ext:{ext}", 0.0) + W_EXT
    add(tokenize(_read_head(path)), W_CONTENT)
    return bag


# ── vector maths (sparse dicts; corpora here are hundreds, not millions) ─────
def _l2(v: dict[str, float]) -> dict[str, float]:
    n = math.sqrt(sum(x * x for x in v.values()))
    return {k: x / n for k, x in v.items()} if n else {}


def cosine(a: dict[str, float], b: dict[str, float]) -> float:
    """Cosine of two L2-normalised sparse vectors (iterate the smaller one)."""
    if len(a) > len(b):
        a, b = b, a
    return sum(x * b.get(k, 0.0) for k, x in a.items())


def _tfidf(bags: list[dict[str, float]]) -> list[dict[str, float]]:
    n = len(bags)
    df: dict[str, int] = {}
    for bag in bags:
        for t in bag:
            df[t] = df.get(t, 0) + 1
    out = []
    for bag in bags:
        # sublinear tf damps a token repeated 200 times in one file
        vec = {t: (1 + math.log(c)) * math.log((1 + n) / (1 + df[t]))
               for t, c in bag.items()}
        out.append(_l2(vec))
    return out


def _kmeans(vecs: list[dict[str, float]], k: int,
            iters: int = KMEANS_ITERS) -> list[dict[str, float]]:
    """Spherical k-means, deterministic.

    Seeded k-means++ using a fixed stride rather than an RNG, so the same
    directory always produces the same graph. A file manager whose clusters
    reshuffle on every refresh is not a file manager.
    """
    if not vecs or k < 1:
        return []
    k = min(k, len(vecs))
    stride = max(1, len(vecs) // k)
    centroids = [dict(vecs[i * stride]) for i in range(k)]
    for _ in range(iters):
        buckets: list[list[int]] = [[] for _ in range(k)]
        for i, v in enumerate(vecs):
            best, bi = -2.0, 0
            for ci, c in enumerate(centroids):
                s = cosine(v, c)
                if s > best:
                    best, bi = s, ci
            buckets[bi].append(i)
        moved = False
        for ci, members in enumerate(buckets):
            if not members:
                continue
            acc: dict[str, float] = {}
            for i in members:
                for t, x in vecs[i].items():
                    acc[t] = acc.get(t, 0.0) + x
            new = _l2(acc)
            if cosine(new, centroids[ci]) < 0.9999:
                moved = True
            centroids[ci] = new
        if not moved:
            break
    return centroids


def _label(centroid: dict[str, float], used: set[str]) -> str:
    """Name a hub after its top distinctive terms, avoiding duplicate names."""
    ranked = sorted(centroid.items(), key=lambda kv: -kv[1])
    picked = []
    for term, _ in ranked:
        term = term.split(":", 1)[-1]        # 'ext:rs' -> 'rs'
        if term in used or term in picked:
            continue
        picked.append(term)
        if len(picked) == 2:
            break
    if not picked:
        return "misc"
    used.update(picked)
    return " · ".join(picked)


# ── scanning ─────────────────────────────────────────────────────────────────
def walk(root: Path, *, depth: int = 3, max_files: int = MAX_FILES,
         include_hidden: bool = False) -> list[Path]:
    """Breadth-first walk, so a shallow overview survives the file cap.

    os.walk is depth-first: on a directory whose first child holds 10k files
    it would spend the whole budget there and never show you the siblings.
    BFS spends it evenly and degrades into "the top of the tree", which is
    the useful truncation.
    """
    found: list[Path] = []
    frontier = [(root, 0)]
    while frontier and len(found) < max_files:
        nxt: list[tuple[Path, int]] = []
        for d, lvl in frontier:
            try:
                entries = sorted(os.scandir(d), key=lambda e: e.name)
            except OSError:
                continue
            for e in entries:
                if not include_hidden and e.name.startswith("."):
                    continue
                try:
                    if e.is_dir(follow_symlinks=False):
                        if lvl < depth and e.name not in IGNORE_DIRS:
                            nxt.append((Path(e.path), lvl + 1))
                    elif e.is_file(follow_symlinks=False):
                        if os.path.splitext(e.name)[1].lower() in IGNORE_EXT:
                            continue
                        found.append(Path(e.path))
                        if len(found) >= max_files:
                            return found
                except OSError:
                    continue
        frontier = nxt
    return found


def graph(directory: str, *, depth: int = 3, max_files: int = MAX_FILES,
          k: int | None = None, include_hidden: bool = False,
          root: Path | None = None) -> dict:
    """Scan `directory` into the physis-compatible graph payload.

    Returns keys: root, source, truncated, themes, files, edges, file_edges.
    """
    base = resolve_in_root(directory, root)
    if not base.is_dir():
        raise FsGraphError(f"not a directory: {base}")

    paths = walk(base, depth=depth, max_files=max_files,
                 include_hidden=include_hidden)
    if not paths:
        return {"root": str(base), "source": "local", "truncated": False,
                "themes": [], "files": [], "edges": [], "file_edges": []}

    nodes: list[FileNode] = []
    bags: list[dict[str, float]] = []
    for i, p in enumerate(paths):
        try:
            st = p.stat()
            size, mtime = st.st_size, st.st_mtime
        except OSError:
            size, mtime = 0, 0.0
        nodes.append(FileNode(id=i, path=str(p), title=p.name, kind=kind_of(p),
                              size=size, mtime=mtime,
                              depth=len(p.relative_to(base).parts) - 1))
        bags.append(_terms(p, base))

    vecs = _tfidf(bags)
    for n, v in zip(nodes, vecs):
        n.vec = v

    # auto-k ~= sqrt(n), clamped so the legend stays readable
    kk = k or max(2, min(12, int(math.sqrt(len(nodes)))))
    centroids = _kmeans(vecs, kk)

    used: set[str] = set()
    hubs = [Hub(id=ci, name=_label(c, used), centroid=c)
            for ci, c in enumerate(centroids)]

    edges = []
    for n in nodes:
        scored = sorted(((cosine(n.vec, h.centroid), h.id) for h in hubs),
                        reverse=True)[:TOP_K_HUBS]
        for score, hid in scored:
            if score >= MIN_HUB_SCORE:
                edges.append({"theme_id": hid, "file_id": n.id,
                              "score": round(score, 4)})

    file_edges = _similarity_edges(nodes)

    return {
        "root": str(base),
        "source": "local",
        "truncated": len(paths) >= max_files,
        "themes": [{"id": h.id, "name": h.name, "domain": h.domain,
                    "mode": h.mode, "category": h.category} for h in hubs],
        "files": [{"id": n.id, "path": n.path, "title": n.title,
                   "kind": n.kind, "size": n.size, "mtime": n.mtime,
                   "depth": n.depth} for n in nodes],
        "edges": edges,
        "file_edges": file_edges,
    }


def _similarity_edges(nodes: list[FileNode]) -> list[dict]:
    """Top-K mutual-ish nearest neighbours above SIM_TAU, deduplicated.

    O(n^2) cosine over <=600 sparse vectors is a few million dict lookups —
    tens of milliseconds, and it keeps the module dependency-free. If the
    cap ever rises past a few thousand, invert to a token->files index first.
    """
    out: dict[tuple[int, int], float] = {}
    for a in nodes:
        best: list[tuple[float, int]] = []
        for b in nodes:
            if a.id == b.id:
                continue
            s = cosine(a.vec, b.vec)
            if s >= SIM_TAU:
                best.append((s, b.id))
        best.sort(reverse=True)
        for s, bid in best[:SIM_TOP_K]:
            key = (min(a.id, bid), max(a.id, bid))
            if s > out.get(key, 0.0):
                out[key] = s
    return [{"source": a, "target": b, "score": round(s, 4)}
            for (a, b), s in sorted(out.items())]


# ── file ops (both sandboxed) ────────────────────────────────────────────────
def preview(path: str, *, limit: int = 16384, root: Path | None = None) -> dict:
    """First `limit` bytes of a file, decoded lossily."""
    p = resolve_in_root(path, root)
    if not p.is_file():
        raise FsGraphError(f"not a file: {p}")
    size = p.stat().st_size
    with open(p, "rb") as fh:
        raw = fh.read(limit)
    return {"path": str(p), "size": size, "truncated": size > limit,
            "content": raw.decode("utf-8", "replace")}


def move(src: str, dest: str, *, root: Path | None = None) -> dict:
    """Rename/move within the sandbox. Never overwrites an existing file.

    The destination does not exist yet, so it cannot be `resolve()`d and
    containment-checked directly — its *parent* is checked instead, then the
    name is re-joined. Resolving a non-existent path would silently succeed
    on `..` segments.
    """
    root = (root or scan_root()).resolve()
    s = resolve_in_root(src, root)
    d_raw = Path(dest).expanduser()
    parent = resolve_in_root(str(d_raw.parent), root)
    d = parent / d_raw.name
    if d.exists():
        raise FsGraphError(f"destination exists: {d}")
    if not parent.is_dir():
        raise FsGraphError(f"destination directory missing: {parent}")
    s.rename(d)
    return {"from": str(s), "to": str(d)}
