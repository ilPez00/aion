---
id: AION-004
track: aion
phase: 2
status: todo
rung: 1
depends: [AION-001]
gate: "python -m pytest tests/test_daily_push.py -q"
commit: "branch agent/AION-004"
---

# HUD: daily check surface — line 1 of the daily report, always visible

## Why
RM-008 produces ~/.local/state/randomesh/daily-check/<date>.md whose line 1
is the verdict. The HUD should show that line permanently and the full report
one keypress away. This closes the 'pushed at you' requirement without any
new notification infrastructure.

## Steps
1. Engine: DailyCheck view model — read today's file (and yesterday's if
   today's absent), parse verdict line, extract item counts.
2. Harness: status bar segment (verdict + date) + a panel with the full
   report; panel goes red when verdict != ALL GREEN and when the file is
   >36 h stale (means the timer died — the check itself needs checking).

## Tests you must add
- **Unit:** verdict parsing (ALL GREEN / N ITEMS / missing file / malformed
  first line); staleness boundary at 36 h with a fake clock.
- **Integration:** fixture dir round-trip; a stale file flips the panel red.

## Gate
```bash
python -m pytest tests/test_daily_push.py -q
```

## Never
- Silently show yesterday's verdict as if current. Fail the HUD when the
  file is absent — show 'no daily check yet'.
