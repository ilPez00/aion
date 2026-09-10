# AION-002 report — task board (feather, 2026-09-10)

## What (branch agent/AION-002, stacked on AION-001)
- `src/aion/ui/task_board.py`: TaskBoard over task state-dir dicts (files,
  not shelling — same journal the queue writes; no SSH in UI thread),
  grouped by node, age-relative times, rc column, running-only filter,
  POLL_INTERVAL_S=30 (floor 15). Pure render included.
- `tests/fixtures/tasks/`: 3 golden metas (done/lost/queued, 2 nodes).
- `tests/test_taskboard.py`: 7 tests (grouping, filter, ages, render,
  poll floor, live dirs, slow queue round-trip).

## Evidence
- 10 passed, 2 honest skips (live dirs present; slow round-trip skipped:
  feather mesh install lacks agent-task.sh at ~/.local/scripts — real
  finding, repo file exists; install path broken on feather).
- Existing test_task_board.py (placeholder skips) now runs: basic/parser/
  sorting pass with the engine present.

## Unknowns
- Remote-node tasks surface only when state syncs to local dirs (documented).
- Stale >7d claims belong to AION-003 (out of scope here).
