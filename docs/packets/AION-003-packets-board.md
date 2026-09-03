---
id: AION-003
track: aion
phase: 2
status: todo
rung: 1
depends: [AION-002]
gate: "python -m pytest tests/test_packets_board.py -q"
commit: "branch agent/AION-003"
---

# HUD: packets board — the multi-year skeleton, visible

## Why
The packet skeleton lives in five repos on pansa. Out of sight, it will rot.
The HUD should answer: which packets exist, which are todo/claimed/done/
blocked, which are stale-claimed (frontmatter claimed_at > 7 days → flag).

## Steps
1. Engine: PacketsBoard — pure functions over a list of parsed packet
   frontmatters (write a tiny YAML-subset parser or use python-frontmatter
   IF already a dependency — check first, do not add deps casually;
   no-new-deps is a praxis rule but the spirit holds here). Views: by phase,
   by status, stale-claimed, blocked-with-evidence.
2. Harness: scan ~/dev/*/packets/*.md and ~/ops/packets/*.md on the node the
   HUD runs on (feather; note pansa is the canonical copy — sync direction
   documented in the panel footer).
3. Drill-down: select packet → render body (why/steps/gate) read-only.

## Tests you must add
- **Unit:** frontmatter parser (well-formed, missing fields, duplicate ids
  across repos — surface the collision, do not hide); staleness math with a
  fake clock.
- **Integration:** over a fixture packets dir with all status variants.

## Gate
```bash
python -m pytest tests/test_packets_board.py -q
```

## Never
- Let the HUD EDIT packets (read-only board; claiming happens in the repo).
