"""life_panel.py — the real-life HUD surface: money · fitness · social · machine.

Lifted out of the app the same way sys_panel was: everything it needs is the
life snapshot from stats ("life" harness), the theme tokens and an animation
tick — so it renders from a pure function and every failure mode is a table
case, not a crash.
"""
from __future__ import annotations

from .visualizers import flow_pipeline

# Domain scores come from aion.life.domain_score; re-import lazily so the
# panel stays import-light for tests that only need rendering.
from ..life import DOMAIN_ORDER, domain_score


def _fmt_eur(v: float) -> str:
    return f"€{v:,.0f}".replace(",", ".")


def life_panel(snap: dict, theme: dict, tick: int = 0) -> str:
    """Render the life workspace. Never raises on missing/partial data."""
    domains = (snap or {}).get("domains", {})
    if not domains:
        return f"[{theme['dim']}]no life data yet — waiting for first poll[/]"

    scores = domain_score({"domains": domains})
    out: list[str] = [f"[{theme['accent']}]◈ LIFE FLOW[/] "
                      f"[{theme['dim']}](machine · body · people · money)[/]",
                      flow_pipeline(scores, tick)]

    # ── money detail ──────────────────────────────────────────────────────
    m = domains.get("money", {})
    if m.get("ok"):
        line = (f"[{theme['fg']}]€ money[/]  paid [{theme['ok']}]"
                f"{_fmt_eur(float(m.get('paid_total') or 0))}[/]"
                f" · open [{theme['warn']}]"
                f"{_fmt_eur(float(m.get('open_total') or 0))}[/]")
        target = float(m.get("target_mrr") or 0)
        if target > 0:
            pct = min(100.0, float(m.get("paid_total") or 0) / target * 100)
            line += f" · target {_fmt_eur(target)} [{theme['accent']}]{pct:.0f}%[/]"
        out.append(line)
        latest = (m.get("entries") or [])[-1:]
        for e in latest:
            out.append(f"[{theme['dim']}]  └ {e.get('note', '')} "
                       f"{_fmt_eur(float(e.get('amount') or 0))}[/]")
    else:
        out.append(f"[{theme['err']}]€ money[/] [{theme['dim']}]"
                   f"{m.get('reason', 'offline')}[/]")

    # ── fitness detail ────────────────────────────────────────────────────
    f_ = domains.get("fitness", {})
    if f_.get("ok"):
        steps = int(f_.get("steps") or 0)
        goal = int(f_.get("step_goal") or 8000)
        hr = f_.get("heart_rate") or f_.get("resting_hr")
        bits = [f"[{theme['fg']}]♥ body[/] steps [{theme['ok']}]{steps:,}[/]"
                .replace(",", ".") + f"/{goal:,}".replace(",", ".")]
        sleep = float(f_.get("sleep_h") or 0)
        if sleep:
            bits.append(f"sleep {sleep:.1f}h")
        if hr:
            bits.append(f"hr {hr}")
        out.append(" · ".join(bits))
    else:
        out.append(f"[{theme['err']}]♥ body[/] [{theme['dim']}]"
                   f"{f_.get('reason', 'offline')}[/]")

    # ── social detail ─────────────────────────────────────────────────────
    s = domains.get("social", {})
    if s.get("ok"):
        mark = "●" if s.get("checked_in") else "○"
        col = theme["ok"] if s.get("checked_in") else theme["warn"]
        line = (f"[{theme['fg']}]☺ people[/] [{col}]{mark}[/] check-in"
                f" · bets {s.get('active_bets', 0)}"
                f" · goals {s.get('goals', 0)}")
        if s.get("goals"):
            avg = float(s.get("goals_avg_progress") or 0) * 100
            line += f" [{theme['accent']}]{avg:.0f}%[/]"
        out.append(line)
    else:
        out.append(f"[{theme['err']}]☺ people[/] [{theme['dim']}]"
                   f"{s.get('reason', 'offline')}[/]")

    # ── computer detail ───────────────────────────────────────────────────
    c = domains.get("computer", {})
    if c.get("ok"):
        out.append(f"[{theme['fg']}]⬡ machine[/] cpu {c.get('cpu_pct', 0):.0f}%"
                   f" · ram {c.get('ram_pct', 0):.0f}%"
                   f" · tasks {c.get('tasks_running', 0)}")
    else:
        out.append(f"[{theme['err']}]⬡ machine[/] [{theme['dim']}]offline[/]")

    return "\n".join(out)


__all__ = ["life_panel", "DOMAIN_ORDER"]
