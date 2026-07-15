"""health.py — real-life statistics reader (pluggable: Google / Apple / JSON).

Normalizes wildly different export formats from Google Fit and Apple Health
into one shape the HUD renders:

    Record = {"date": "YYYY-MM-DD", "steps": int, "heart_rate": float,
              "sleep_hours": float, "active_calories": float,
              "screen_time": float}

The JSON source is the reference (easy to hand-maintain / script). Google and
Apple importers translate their export files into the same list of Records so
the HUD code never knows which provider produced the data.
"""
from __future__ import annotations

import csv
import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# normalized metric keys the HUD knows about
METRICS = ("steps", "heart_rate", "sleep_hours", "active_calories", "screen_time")


@dataclass
class HealthRecord:
    date: str
    steps: int = 0
    heart_rate: float = 0.0
    sleep_hours: float = 0.0
    active_calories: float = 0.0
    screen_time: float = 0.0

    def as_dict(self) -> dict:
        return {
            "date": self.date, "steps": self.steps, "heart_rate": self.heart_rate,
            "sleep_hours": self.sleep_hours, "active_calories": self.active_calories,
            "screen_time": self.screen_time,
        }


class HealthReader:
    """Pluggable real-life stats reader.

    source = "json" | "google" | "apple"
    path   = file to read (JSON file, Google Takeout dir, or Apple export.xml)
    """

    def __init__(self, source: str = "json", path: str | Path | None = None) -> None:
        self.source = source
        self.path = Path(path) if path else None

    # ---- public API ------------------------------------------------------
    def records(self) -> list[HealthRecord]:
        if self.source == "google":
            return self._from_google()
        if self.source == "apple":
            return self._from_apple()
        return self._from_json()

    def summary(self) -> dict[str, Any]:
        recs = self.records()
        if not recs:
            return {"ok": False, "count": 0, "latest": None, "series": {}}
        # latest + last-7-day series per metric (oldest->newest)
        latest = recs[-1]
        series: dict[str, list[float]] = {m: [] for m in METRICS}
        window = recs[-7:]
        for r in window:
            for m in METRICS:
                series[m].append(getattr(r, m))
        # simple rolling averages for the headline numbers
        avgs = {m: round(sum(series[m]) / max(1, len(series[m])), 1)
                for m in METRICS}
        return {
            "ok": True,
            "count": len(recs),
            "latest": latest.as_dict(),
            "series": series,
            "avg_7d": avgs,
            "source": self.source,
        }

    # ---- JSON (reference) ------------------------------------------------
    def _from_json(self) -> list[HealthRecord]:
        if self.path is None or not Path(self.path).exists():
            # graceful: no data yet -> empty, HUD shows "(no health data)"
            return []
        raw = json.loads(Path(self.path).read_text())
        items = raw["records"] if isinstance(raw, dict) and "records" in raw else raw
        out: list[HealthRecord] = []
        for d in items:
            out.append(HealthRecord(
                date=str(d.get("date", "")),
                steps=int(d.get("steps", 0) or 0),
                heart_rate=float(d.get("heart_rate", 0) or 0),
                sleep_hours=float(d.get("sleep_hours", 0) or 0),
                active_calories=float(d.get("active_calories", 0) or 0),
                screen_time=float(d.get("screen_time", 0) or 0),
            ))
        out.sort(key=lambda r: r.date)
        return out

    # ---- Apple Health (export.xml) --------------------------------------
    def _from_apple(self) -> list[HealthRecord]:
        if self.path is None or not Path(self.path).exists():
            return []
        tree = ET.parse(self.path)
        root = tree.getroot()
        by_day: dict[str, HealthRecord] = {}
        tree = ET.parse(self.path)
        root = tree.getroot()
        by_day: dict[str, HealthRecord] = {}
        hr_sum: dict[str, list[float]] = {}
        step_type = "HKQuantityTypeIdentifierStepCount"
        hr_type = "HKQuantityTypeIdentifierHeartRate"
        sleep_type = "HKCategoryTypeIdentifierSleepAnalysis"
        for rec in root.iter("Record"):
            rtype = rec.get("type", "")
            val = rec.get("value", "")
            start = rec.get("startDate", "")
            day = start[:10]
            if not day:
                continue
            r = by_day.setdefault(day, HealthRecord(date=day))
            try:
                if rtype == step_type:
                    r.steps += int(float(val))
                elif rtype == hr_type:
                    hr_sum.setdefault(day, []).append(float(val))
                elif rtype == sleep_type and val == "HKCategoryValueSleepAnalysisAsleep":
                    # startDate..endDate duration in hours
                    end = rec.get("endDate", "")
                    dur = self._hour_diff(start, end)
                    if dur > 0:
                        r.sleep_hours += dur
            except (ValueError, TypeError):
                continue
        for day, vals in hr_sum.items():
            if day in by_day and vals:
                by_day[day].heart_rate = sum(vals) / len(vals)
        # Apple doesn't export calories/screen-time here; leave as 0
        return [by_day[d] for d in sorted(by_day)]

    @staticmethod
    def _hour_diff(a: str, b: str) -> float:
        try:
            from datetime import datetime
            fmt = "%Y-%m-%d %H:%M:%S %z"
            ta = datetime.strptime(a[:25].strip(), fmt)
            tb = datetime.strptime(b[:25].strip(), fmt)
            return (tb - ta).total_seconds() / 3600.0
        except Exception:
            return 0.0

    # ---- Google Fit (Takeout CSV) ---------------------------------------
    def _from_google(self) -> list[HealthRecord]:
        if self.path is None or not Path(self.path).exists():
            return []
        # Google Takeout layout varies; accept a CSV file or a dir containing one
        csv_path = self.path
        if csv_path.is_dir():
            hits = list(csv_path.rglob("*.csv"))
            csv_path = hits[0] if hits else None
        if csv_path is None or not csv_path.exists():
            return []
        by_day: dict[str, HealthRecord] = {}
        with csv_path.open(newline="", encoding="utf-8", errors="ignore") as f:
            reader = csv.DictReader(f)
            for row in reader:
                day = self._google_day(row)
                if not day:
                    continue
                r = by_day.setdefault(day, HealthRecord(date=day))
                # column names differ across Takeout versions -> tolerant match
                r.steps += self._grab_int(row, ("step", "steps"))
                r.heart_rate = self._grab_float(row, ("heart", "bpm", "hr"))
                r.active_calories += self._grab_float(row, ("calorie", "active"))
                r.sleep_hours = max(r.sleep_hours, self._grab_float(row, ("sleep",)))
        return [by_day[d] for d in sorted(by_day)]

    @staticmethod
    def _google_day(row: dict) -> str:
        for key in ("Date", "date", "Day", "startDate"):
            if key in row and row[key]:
                return str(row[key])[:10]
        return ""

    @staticmethod
    def _grab_int(row: dict, needles: tuple[str, ...]) -> int:
        for k, v in row.items():
            if any(n in k.lower() for n in needles) and v not in (None, ""):
                try:
                    return int(float(v))
                except (ValueError, TypeError):
                    return 0
        return 0

    @staticmethod
    def _grab_float(row: dict, needles: tuple[str, ...]) -> float:
        for k, v in row.items():
            if any(n in k.lower() for n in needles) and v not in (None, ""):
                try:
                    return float(v)
                except (ValueError, TypeError):
                    return 0.0
        return 0.0
