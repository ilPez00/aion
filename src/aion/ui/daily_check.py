"""daily_check.py — daily-check surface (AION-004).

RM-008 produces ~/.local/state/randomesh/daily-check/<date>.md whose line 1
is the verdict. This engine reads today's file (yesterday's ONLY as a
labelled fallback — never presented as current), parses the verdict, and
flags staleness: a file >36 h old means the timer died, so the panel goes
red. Absent file -> "no daily check yet", never a failure, never silence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

STALE_AFTER_H = 36.0


@dataclass
class DailyCheck:
    verdict: str = ""              # line 1, verbatim
    report_date: str = ""          # which file it came from
    is_today: bool = False
    age_h: float = -1.0
    counts: dict = field(default_factory=dict)
    missing: bool = False

    @property
    def all_green(self) -> bool:
        return "ALL GREEN" in self.verdict.upper()

    @property
    def stale(self) -> bool:
        return self.missing or self.age_h < 0 or self.age_h > STALE_AFTER_H

    @property
    def red(self) -> bool:
        """Panel goes red when the verdict is bad OR the check itself died."""
        return self.missing or self.stale or not self.all_green

    def status_line(self) -> str:
        if self.missing:
            return "no daily check yet"
        tag = self.report_date if self.is_today else f"{self.report_date} (yesterday)"
        return f"{self.verdict.strip()} · {tag}"


def _parse_counts(lines: list) -> dict:
    counts: dict = {}
    for line in lines[1:]:
        s = line.strip().lower()
        if s.startswith("- ") or s.startswith("* "):
            key = s[2:].split(":")[0].strip()[:32]
            if key:
                counts[key] = counts.get(key, 0) + 1
    return counts


def read_daily_check(state_dir: str | Path,
                     now: datetime | None = None) -> DailyCheck:
    """Read today's verdict, else yesterday's (labelled). Never raises."""
    today = (now or datetime.now(timezone.utc)).date()
    root = Path(state_dir)
    for delta, is_today in ((0, True), (1, False)):
        day = today - timedelta(days=delta)
        for name in (f"{day.isoformat()}.md", f"{day.strftime('%Y%m%d')}.md"):
            p = root / name
            try:
                if not p.is_file():
                    continue
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            lines = [ln for ln in text.splitlines() if ln.strip()]
            if not lines:
                continue
            try:
                mtime = datetime.fromtimestamp(p.stat().st_mtime,
                                               tz=timezone.utc)
                age_h = (((now or datetime.now(timezone.utc)) - mtime)
                         .total_seconds() / 3600.0)
            except OSError:
                age_h = -1.0
            return DailyCheck(verdict=lines[0][:120],
                              report_date=day.isoformat(),
                              is_today=is_today,
                              age_h=age_h,
                              counts=_parse_counts(lines))
    return DailyCheck(missing=True)


def default_daily_check_dir() -> Path:
    return (Path.home() / ".local" / "state" / "randomesh" / "daily-check")


def render_daily_check(check: DailyCheck, theme: dict) -> str:
    di = theme.get("dim", "#9aabbb")
    a = theme.get("accent", "#5ad1ff")
    ok_ = theme.get("ok", "#7CFFB2")
    err = theme.get("err", "#FF8A8A")
    if check.missing:
        return f"[{a}]☀ DAILY[/]  [{di}]no daily check yet[/]"
    color = err if check.red else ok_
    stale = f" [{err}]STALE {check.age_h:.0f}h[/]" if check.stale else ""
    head = f"[{a}]☀ DAILY[/]  [{color}]{check.status_line()}[/]{stale}"
    if not check.counts:
        return head
    items = "  ".join(f"{k}:{v}" for k, v in list(check.counts.items())[:6])
    return head + f"\n  [{di}]{items}[/]"
