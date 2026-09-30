---
id: AION-500
track: aion
phase: D
status: todo
rung: 3
depends: [AION-002]
gate: "test -f packets/AION-5xx-proposals.md"
commit: "branch agent/AION-500"
---

# DISCOVERY: next quarter's aion packets

## Sources
docs/plans/2026-09-01_aion-randomesh-integration.md (uncommitted at write
time — resolve via PR-000-style hygiene first); meshd state surfaces not yet
visualised; memd as a HUD-searchable knowledge base (recall in the HUD);
provider/model status panel (from RM-009 bench results); decision log panel
(packets/reports/* 'DECIDED' markers).

## Multi-year themes
- aion as the single operator surface: if a task needs a terminal command
  more than 2×/week, propose a panel for it.
- Everything new follows pure-engine + thin-harness, or the PR explaining why
  not.

## Gate
```bash
test -f packets/AION-5xx-proposals.md
```

## Never
- Add a dependency for a panel that is 50 lines of stdlib.
