# aion work packets (HUD, aggregation, push surface)

Read PROTOCOL.md first. Then the repo rules that override everything:

- aion is a Textual (Python) split-screen HUD + app-managing desktop. Pattern
  to follow everywhere: **pure engine + thin harness**. Engine = testable
  python, no I/O; harness = reads state, calls engine, renders. Never mix.
- `src/aion/meshsrv.py` may have uncommitted work from another session —
  re-check `git status` before any destructive step (this clone had
  modifications + a docs/plans/2026-09-01_aion-randomesh-integration.md
  at packet-writing time).
- aion consumes: meshd.state, capability.json, the agent-task queue output,
  memd. It does not own those formats — randomesh packets define them.
