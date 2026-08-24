"""life.py — the real-life HUD: money, fitness, social, computer as one flow.

The cockpit already shows the machine (system/sysinfo) and the work
(tasks/board). What it does not show is the rest of Gio's life, which is the
point of a "real-life HUD": four domains, one glance, each with a 0..1 score
that feeds a single flow visualizer.

Sources, in order of honesty:

  money    ~/.aion/money.md — a pipe-table ledger the user (or an agent)
           appends to. Parsed here; missing file degrades, never blocks.
  fitness  ~/.aion/health.json — the file HealthHarness already reads.
           Reusing it means one source of truth for the body.
  social   praxis /dashboard/summary via PraxisClient's injectable transport —
           check-in streak, active bets, goal-tree progress. OFF unless
           AION_PRAXIS_URL/KEY/USER are set (same env praxis.py reads).
  computer the live stats snapshot the SystemHarness already publishes.

Pure by construction: collectors take paths and a transport callable, so the
whole module tests with tmp files and a fake — no network, no clock. The
harness wrapper (LifeHarness in harnesses.py) is the only I/O-bearing part.

Design rule inherited from physis.py/praxis.py: every domain ALWAYS reports,
even when its source is gone (`ok: False` + reason). A life HUD with a hole
in one panel is information ("money tracking stopped"); a crashed poller is
neither use nor ornament.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable

__all__ = [
    "DOMAIN_ORDER", "LifeConfig", "collect_life", "domain_score",
    "money_from_text", "fitness_from_json", "social_from_summary",
]

# Render order of the flow: machine -> body -> people -> money.
DOMAIN_ORDER = ("computer", "fitness", "social", "money")

Transport = Callable[[str, str, dict | None], tuple[int, Any]]

STEP_GOAL_DEFAULT = 8000


# ── config ────────────────────────────────────────────────────────────────────

@dataclass
class LifeConfig:
    """Where each domain's truth lives. Env-overridable, test-friendly."""
    money_path: str = ""
    health_path: str = ""
    # praxis fields mirror PraxisConfig so both read the same env vars.
    praxis_url: str = ""
    praxis_key: str = ""
    praxis_user: str = ""

    @property
    def praxis_enabled(self) -> bool:
        return bool(self.praxis_url and self.praxis_key)

    @classmethod
    def from_env(cls, env: dict | None = None,
                 cfg: dict | None = None) -> "LifeConfig":
        """Build from process env + optional layout.json `extra` dict."""
        env = dict(env if env is not None else os.environ)
        raw = (cfg or {})
        from .paths import data_dir
        base = str(data_dir())
        return cls(
            money_path=str(env.get("AION_LIFE_MONEY_FILE")
                           or raw.get("money_path")
                           or os.path.join(base, "money.md")),
            health_path=str(env.get("AION_LIFE_HEALTH_FILE")
                            or raw.get("health_path") or ""),
            praxis_url=str(env.get("AION_PRAXIS_URL") or raw.get("praxis_url") or ""),
            praxis_key=str(env.get("AION_PRAXIS_KEY") or raw.get("praxis_key") or ""),
            praxis_user=str(env.get("AION_PRAXIS_USER") or raw.get("praxis_user") or ""),
        )


# ── individual domain parsers (pure) ─────────────────────────────────────────

def money_from_text(text: str) -> dict:
    """Parse the pipe ledger: `- date | kind | note | amount | status`.

    kind/amount drive the totals: payments and expenses land on paid_total
    when status says paid; invoices stay open until a matching payment row.
    """
    out = {"ok": True, "entries": [], "paid_total": 0.0,
           "open_total": 0.0, "target_mrr": 0.0}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(("target_mrr:", "#")) or not line:
            for tok in line.replace(":", " ").split():
                try:
                    out["target_mrr"] = float(tok)
                except ValueError:
                    continue
            continue
        if not line.startswith("-"):
            continue
        parts = [p.strip() for p in line.lstrip("-").split("|")]
        if len(parts) < 4:
            continue
        try:
            amount = float(parts[3])
        except ValueError:
            continue
        entry = {"date": parts[0], "kind": parts[1], "note": parts[2],
                 "amount": amount, "status": parts[4] if len(parts) > 4 else ""}
        out["entries"].append(entry)
        if entry["status"] == "paid":
            out["paid_total"] += amount
        elif entry["status"] == "sent":
            out["open_total"] += amount
    return out


def fitness_from_json(data: dict) -> dict:
    """Normalize whatever health.json holds into the few fields we render."""
    steps = int(data.get("steps") or 0)
    return {
        "ok": bool(data),
        "steps": steps,
        "step_goal": int(data.get("step_goal") or STEP_GOAL_DEFAULT),
        "sleep_h": float(data.get("sleep_h") or data.get("sleep_hours") or 0),
        "resting_hr": data.get("resting_hr"),
        "heart_rate": data.get("heart_rate"),
        "spo2": data.get("spo2"),
    }


def social_from_summary(summary: dict) -> dict:
    """Shape praxis' dashboard summary into social signals."""
    nodes = ((summary.get("goalTree") or {}).get("nodes")) or []
    progresses = [float(n.get("progress") or 0) for n in nodes
                  if isinstance(n, dict)]
    avg = sum(progresses) / len(progresses) if progresses else 0.0
    return {
        "ok": True,
        "checked_in": bool(summary.get("checkedIn")),
        "active_bets": len(summary.get("activeBets") or []),
        "goals": len(progresses),
        "goals_avg_progress": round(avg, 3),
    }


# ── the collector ─────────────────────────────────────────────────────────────

def collect_life(cfg: LifeConfig,
                 transport: Transport | None = None,
                 sys_stats: dict | None = None) -> dict:
    """Snapshot all four domains. Never raises; every domain always present."""
    domains: dict[str, dict] = {}

    # computer — the one domain that is always alive when aion itself is.
    s = dict(sys_stats or {})
    domains["computer"] = {
        "ok": bool(s),
        "cpu_pct": s.get("cpu_pct", 0),
        "ram_pct": s.get("ram_pct", 0),
        "tasks_running": s.get("tasks_running", 0),
    }

    # fitness — shared health.json (HealthHarness writes/reads the same file).
    try:
        path = cfg.health_path
        if path and os.path.exists(path):
            with open(path) as f:
                domains["fitness"] = fitness_from_json(json.load(f))
        else:
            domains["fitness"] = {"ok": False, "reason": f"no {path or 'health file'}",
                                  "steps": 0, "step_goal": STEP_GOAL_DEFAULT}
    except Exception as e:  # noqa: BLE001
        domains["fitness"] = {"ok": False, "reason": str(e)[:80],
                              "steps": 0, "step_goal": STEP_GOAL_DEFAULT}

    # social — praxis summary through the injected transport.
    if not cfg.praxis_enabled:
        domains["social"] = {"ok": False, "reason": "praxis not configured"}
    else:
        try:
            t = transport or _default_transport(cfg)
            status, data = t("GET", "/api/dashboard/summary", None)
            if status == 200:
                domains["social"] = social_from_summary(data or {})
            else:
                domains["social"] = {"ok": False, "reason": f"HTTP {status}"}
        except Exception as e:  # noqa: BLE001
            domains["social"] = {"ok": False, "reason": str(e)[:80]}

    # money — local ledger file.
    try:
        if cfg.money_path and os.path.exists(cfg.money_path):
            with open(cfg.money_path) as f:
                domains["money"] = money_from_text(f.read())
        else:
            domains["money"] = {"ok": False,
                                "reason": f"no {cfg.money_path or 'ledger'}",
                                "entries": [], "paid_total": 0.0,
                                "open_total": 0.0, "target_mrr": 0.0}
    except Exception as e:  # noqa: BLE001
        domains["money"] = {"ok": False, "reason": str(e)[:80], "entries": [],
                            "paid_total": 0.0, "open_total": 0.0,
                            "target_mrr": 0.0}

    return {"domains": domains}


def _default_transport(cfg: LifeConfig) -> Transport:
    """urllib transport against the praxis backend (mirrors praxis.py)."""
    import urllib.request

    def transport(method: str, path: str, body: dict | None = None):
        url = cfg.praxis_url.rstrip("/") + path
        req = urllib.request.Request(url, method=method)
        req.add_header("Authorization", f"Bearer {cfg.praxis_key}")
        if body is not None:
            req.add_header("Content-Type", "application/json")
            req.data = json.dumps(body).encode()
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read().decode() or "{}")

    return transport


# ── scoring (the number the visualizer renders) ──────────────────────────────

def _clamp(v: float) -> float:
    return max(0.0, min(1.0, v))


def domain_score(snap: dict) -> list[tuple[str, float]]:
    """Per-domain 0..1 score, in DOMAIN_ORDER. Missing keys score 0."""
    d = snap.get("domains", {})
    scores: list[tuple[str, float]] = []
    for name in DOMAIN_ORDER:
        m = d.get(name, {}) or {}
        if not m.get("ok"):
            scores.append((name, 0.0))
            continue
        if name == "computer":
            # Calm-tech: an idle box is healthy; only sustained heat costs.
            load = (float(m.get("cpu_pct") or 0) / 100.0
                    + float(m.get("ram_pct") or 0) / 100.0) / 2.0
            scores.append((name, _clamp(1.0 - max(0.0, load - 0.70) / 0.30)))
        elif name == "fitness":
            goal = max(1, int(m.get("step_goal") or STEP_GOAL_DEFAULT))
            scores.append((name, _clamp(float(m.get("steps") or 0) / goal)))
        elif name == "social":
            # Half for showing up today, half for how the goal tree is going.
            v = 0.5 if m.get("checked_in") else 0.0
            if m.get("goals"):
                v += 0.5 * _clamp(float(m.get("goals_avg_progress") or 0))
            scores.append((name, _clamp(v)))
        elif name == "money":
            target = float(m.get("target_mrr") or 0)
            paid = float(m.get("paid_total") or 0)
            scores.append((name, _clamp(paid / target) if target > 0
                           else _clamp(min(paid / 2000.0, 1.0))))
    return scores
