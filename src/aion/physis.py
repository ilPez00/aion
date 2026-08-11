"""physis.py — thin client for the physis_pro coherence engine.

physis_pro is Gio's Rust engine (dev/physis_pro), running as an HTTP
server (default http://127.0.0.1:19876, env PHYSIS_PORT). It exposes
the coherence/classify brain that aion's swarm needs:

    POST /api/v1/classify            text -> semiotic-grid cell scores
    POST /api/v1/coherence/register  {input} -> node id (text is embedded)
    POST /api/v1/holarchy/edge       {source, target} -> relation between ids
    POST /api/v1/holarchy/ingest     {data} -> sensory ring (ThoughtCapture)
    POST /api/v1/reconstruct         {input} -> nearest nodes by content
    GET  /api/v1/embedder            health (bge-base self-test)

aion uses this as its BRAIN: every task is classified (what domain of
work) and recorded as a node so physis's "dream" loop can propose
corrective actions across the agent fleet.

Coherence is DERIVED, not asserted. The engine scores a node by its mean
cosine to its 5 nearest neighbours (`core.rs update_coherence`), clamped
at >= 0 -- there is no wire field for "this failed" and negative scores
are unrepresentable. So an outcome survives only as *words in the text we
embed*: `record_outcome` writes "blocked" / "idle" / "flowing" into the
label, which lands failed runs near each other in vector space. That is
weaker than a signed ledger but it is what the engine actually stores.

Mirrors the style of llm.py (urllib, no extra deps).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any


DEFAULT_BASE = "http://127.0.0.1:19876"
X_USER = "aion"  # distinct tenant so aion's nodes don't collide with Praxis


@dataclass
class CellScore:
    domain: str
    mode: str
    score: float
    entries: list[str] = field(default_factory=list)


@dataclass
class Recalled:
    """A prior node the wiki already holds, ranked by meaning."""
    label: str
    similarity: float
    coherence: float

    @property
    def outcome(self) -> str:
        """flowing / blocked / idle if this node came from `record_outcome`."""
        head = self.label.split(" — ", 1)[0]
        return head if head in ("flowing", "blocked", "idle") else ""


@dataclass
class PhysisResult:
    degraded: bool = False
    kind: str = "unknown"
    cells: list[CellScore] = field(default_factory=list)

    @property
    def top(self) -> CellScore | None:
        return max(self.cells, key=lambda c: c.score) if self.cells else None

    def label(self) -> str:
        t = self.top
        return f"{t.domain}/{t.mode}" if t else "unknown"


class PhysisClient:
    """Minimal, dependency-free client. Fails soft: if physis is down,
    callers get degraded=False but empty results and a log line, never an
    exception that breaks the aion UI loop."""

    def __init__(self, base: str | None = None, user: str = X_USER,
                 token: str = "", timeout: int = 8):
        import os
        self.base = (base or os.environ.get("PHYSIS_URL", DEFAULT_BASE)).rstrip("/")
        self.user = os.environ.get("PHYSIS_USER", user)
        self.token = token or os.environ.get("PHYSIS_API_TOKEN", "")
        self.timeout = timeout

    # -- low-level -----------------------------------------------------
    def _req(self, method: str, path: str, payload: dict | None = None) -> dict | None:
        url = f"{self.base}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("X-Physis-User", self.user)
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return {"error": f"http {e.code}", "detail": e.read().decode()[:200]}
        except (urllib.error.URLError, OSError, ValueError) as e:
            return {"error": str(e)[:160]}

    # -- public API ----------------------------------------------------
    def embedder_health(self) -> dict:
        return self._req("GET", "/api/v1/embedder") or {}

    def classify(self, text: str) -> PhysisResult:
        raw = self._req("POST", "/api/v1/classify", {"text": text}) or {}
        res = PhysisResult(
            degraded=raw.get("degraded", False),
            kind=raw.get("kind", "unknown"),
        )
        for r in raw.get("results", []):
            res.cells.append(CellScore(
                domain=r.get("domain", "?"),
                mode=r.get("mode", "?"),
                score=float(r.get("score", 0.0)),
                entries=list(r.get("entries", [])),
            ))
        return res

    def register_text(self, text: str) -> str | None:
        """Embed `text` as a coherence node, return its id (None on failure).

        The engine dedupes on the exact label, so re-registering the same text
        returns the id it already has -- which is what makes a label usable as
        a stable key for `edge`.
        """
        raw = self._req("POST", "/api/v1/coherence/register", {"input": text}) or {}
        return raw.get("node_id")

    def edge(self, source_id: str, target_id: str, weight: float = 1.0) -> dict | None:
        """Relate two coherence nodes by *id* (404s on an unknown id)."""
        return self._req("POST", "/api/v1/holarchy/edge",
                         {"source": source_id, "target": target_id, "weight": weight})

    def register(self, node: str, score: float, edge_to: str | None = None) -> dict | None:
        """Record an outcome as a node, optionally edged to its domain.

        `score` is not sent -- the wire has no field for it (see module docstring).
        It is rendered into the embedded text instead, so the sign is not lost,
        only moved from a number into the vector's neighbourhood.
        """
        state = "flowing" if score > 0.2 else "blocked" if score < -0.2 else "idle"
        label = f"{state} — {edge_to} — {node}" if edge_to else f"{state} — {node}"
        node_id = self.register_text(label)
        if not node_id:
            return {"error": "register failed"}
        result: dict[str, Any] = {"node_id": node_id, "label": label}
        if edge_to:
            # The domain label is a node in its own right; dedupe makes this
            # idempotent, so every task in a domain edges to the same node.
            domain_id = self.register_text(edge_to)
            if domain_id:
                result["edge"] = self.edge(node_id, domain_id)
        return result

    def ingest(self, node: str, edges: list[str] | None = None) -> dict | None:
        """Push a line into the sensory ring as a ThoughtCapture.

        Despite the route name this is *not* a graph write -- the handler pushes
        raw bytes into the ingest ring. Edges are folded into the text; use
        `edge()` for real graph relations.
        """
        data = f"{node} <- {', '.join(edges)}" if edges else node
        return self._req("POST", "/api/v1/holarchy/ingest", {"data": data})

    def reconstruct(self, text: str) -> dict | None:
        """Nearest nodes to `text` + an LLM interpretation. POST, not GET."""
        return self._req("POST", "/api/v1/reconstruct", {"input": text})

    def recall(self, text: str, k: int = 5) -> list[Recalled]:
        """What the wiki already holds near `text`, most similar first.

        This is the read half of the loop: aion has been writing outcomes for
        a while, so a new task can be shown the prior work it resembles --
        including whether that work flowed or blocked. Drops the neighbours'
        raw embeddings (768 floats each) since only the labels are useful here.
        """
        raw = self.reconstruct(text) or {}
        out: list[Recalled] = []
        for n in raw.get("neighbors", [])[:k]:
            label = n.get("label")
            if not label:  # unlabeled nodes have no readable handle
                continue
            out.append(Recalled(
                label=str(label),
                similarity=float(n.get("cosine_similarity", 0.0)),
                coherence=float(n.get("coherence_score", 0.0)),
            ))
        return out


# Module-level singleton so Store/harnesses share one client + tenant.
_client: PhysisClient | None = None


def get_client() -> PhysisClient:
    global _client
    if _client is None:
        _client = PhysisClient()
    return _client


# ── convenience: coherence scoring + outcome recording ───────────────────────
# Both soft-fail (physis down -> neutral / no-op). Safe to call from a worker
# thread: PhysisClient is blocking urllib and touches no asyncio/registry state.
def score_text(text: str) -> float:
    """Coherence proxy in [-1, 1]: how strongly physis recognises this output.

    The top semiotic cell's score is how well the text matches a known domain
    of work; a degraded classify (physis down / empty) scores 0.0 (idle). Used
    as per-iteration telemetry, never as a stop signal.
    """
    if not text.strip():
        return 0.0
    res = get_client().classify(text[:4000])
    if res.degraded or res.top is None:
        return 0.0
    return max(-1.0, min(1.0, res.top.score))


def recall_prior(text: str, k: int = 3) -> list[Recalled]:
    """Prior work resembling `text` (empty list if physis is down)."""
    if not text.strip():
        return []
    try:
        return get_client().recall(text[:4000], k)
    except Exception:  # noqa: BLE001  (the brain is optional, never fatal)
        return []


def record_outcome(node: str, coherence: float, domain: str | None = None) -> None:
    """Persist a loop's result as a node labelled flowing / idle / blocked."""
    try:
        get_client().register(node, coherence, edge_to=domain)
    except Exception:  # noqa: BLE001  (the brain is optional, never fatal)
        pass
