---
id: AION-001
track: aion
phase: 1
status: todo
rung: 1
depends: []
gate: "python -m pytest tests/test_fleet_workspace.py -q"
commit: "branch agent/AION-001"
---

# HUD: fleet workspace — 'what can it do' and 'what is it doing'

## Why
The Mesh workspace reads meshd.state only. The two questions the HUD cannot
answer today: what can the fleet DO (capability.json) and what is it DOING
(agent-task queue). Backlog N6, still open.

## Steps
1. Engine: aion.fleet.FleetView — pure functions over (capability dict,
   task list, meshd state) → view models (node capability table; task table
   sorted by recency; counts). NO I/O in the engine; unit-test it with
   fixtures (all-green, one-lost-task, node-unreachable).
2. Harness: workspace reads ~/.local/state/randomesh/capability.json +
   `agent-task.sh status` output (or its state dirs directly — prefer files
   over shelling out), renders on the existing Mesh tab.
3. Lost/timed-out tasks render LOUDLY (they are the honest-failure states).

## Tests you must add
- **Unit (pytest):** FleetView over fixtures incl. malformed/partial json
  (HUD must never crash on stale fleet data); sorting; lost-state highlighting.
- **Integration:** harness against the real state dirs on this machine if
  present, else fixture dirs (same paths, temp).

## Gate
```bash
python -m pytest tests/ -q
python -m aion --headless 2>/dev/null || true   # smoke: no import-time crash
```

## Never
- Write to randomesh state from aion (read-only consumer). Crash the HUD on
  missing files — degrade the view, print a placeholder row.
