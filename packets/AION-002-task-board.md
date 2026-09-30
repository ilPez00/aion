---
id: AION-002
track: aion
phase: 1
status: todo
rung: 1
depends: [AION-001]
gate: "python -m pytest tests/test_task_board.py -q"
commit: "branch agent/AION-002"
---

# HUD: task board — mesh task status --all, live

## Why
The user asks 'what are the agents doing' hourly. The answer should be a
panel that is always on, not a command someone must remember.

## Steps
1. Engine: TaskBoard model — parse agent-task status --all output (or read
   each node's tasks dir over a helper; pick ONE mechanism, document it in
   the module docstring). Group by node; age-relative times; rc column.
2. Harness: poll interval (default 30 s), keyboard focus, and a filter
   (running-only toggle). The panel is the single place the user looks.
3. A claimed-then-forgotten task (claimed >7d stale per packet protocol) is
   out of scope here — that is the packets board (AION-003).

## Tests you must add
- **Unit:** parser over golden fixtures (real output captured from
  agent-task.sh, stored under tests/fixtures/); partial node unreachable.
- **Integration:** end-to-end on this machine: submit a shell task via the
  queue, watch the board pick it up, wait, see done(0) — as a pytest marked
  slow, skipped if randomesh is absent (honest skip).

## Gate
```bash
python -m pytest tests/test_task_board.py -q
```

## Never
- Poll faster than 15 s (wasteful; the queue already journals). Shell out to
  ssh in the UI thread (async harness or background worker only).
