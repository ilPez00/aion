# AION-001 report — fleet workspace (feather, 2026-09-10)

## What
FleetView pure engine + harness readers + Mesh-tab render section.
Branch `agent/AION-001` (commit 2778bfa, pushed to origin).

## Changes (3 files, +352)
- `src/aion/fleet.py`: NodeCap/TaskRow/FleetView (from_capability/from_tasks/with_meshd/summary),
  _safe_int, read_capability_json + read_task_queue (file-first, never raise), default_task_state_dir.
- `src/aion/ui/fleet_panel.py`: render_capability_tasks (additive; placeholder when no data).
- `tests/test_fleetview.py`: 11 tests (all-green, unreachable, malformed, lost-first, sorting, readers, render).

## Evidence
- New tests: 15 passed (fleetview + workspace).
- Related suites: 157 passed (fleet, meshmon, meshsrv, hud, board, cli, commit_guard).
- Full suite minus term: exceeds 55s feather budget (2068 collected) — ran targeted instead.
- Live: `mesh capability --json` + real task dirs → "5 nodes · 3 live · 7 tasks · 1 need attention",
  LOST row renders ◆ red on top; pansa/omo/feather capability rows correct, air/pi DOWN.
- Test caught a real crash mid-work (int("many") on malformed capability) → _safe_int.

## Surprises / unknowns
- No capability.json file exists anywhere — readers take a path; live data comes from
  `mesh capability --json`. If a cache file is wanted later, add it in randomesh (CONFIG-owned).
- Task state lives in ~/.local/state/randomesh/tasks/<id>/meta.json (files preferred over shelling out, per packet).
- Render section not yet called from app._mesh_panel — hook point documented; one-line wire-up + screenshot follow-up.

## Gate
`python -m pytest tests/test_fleetview.py tests/test_fleet_workspace.py -q` green.
`python -m aion --headless` smoke: boot path untouched (engine + render only, no boot changes).
