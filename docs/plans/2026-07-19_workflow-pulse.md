# Plan: Workflow Pulse + Mission Strip

**Date:** 2026-07-19  
**Product gate:** HUD density / glanceable agentic status — not chat or persona theater.

## Goal

At a glance (any workspace, half-screen), answer:

1. Is anything agentic running?
2. What is the mission / goal?
3. What stage is it on (plan → act → wait → verify → done)?
4. What is blocked, and on whom?
5. Which key intervenes (p / x / a / r)?

## Non-goals

- New chat UI or Jarvis monologue
- Decorative equalizers as primary signal
- Parallel agent brain (still host harnesses / swarm / board as sources)

## Design

### Unified row model (`workflows.py`)

```text
WorkflowRow:
  id, kind ∈ {swarm, task, board, hermes, agent}
  title, stage ∈ {plan, act, wait, blocked, verify, done, failed}
  progress 0..1
  agents: [{name, status, progress, blocked_by?}]
  blocked_by, age_s, cost_hint?
  next_action ∈ {pause, cancel, rerun, act, none}
```

Collectors merge:

| Source | When live |
|--------|-----------|
| `registry.tasks` | pending / running / failed / interrupted |
| `swarm` metrics or orchestrator | any agents not all-idle/done |
| `board` stats | any board with backlog/active cards |
| Hermes `stats.agents` | live sessions |
| `agent_entity` working agents | assigned task / working status |

### Surfaces

| Surface | Change |
|---------|--------|
| **Header Mission Strip** | One line: live count · top mission · stage pipeline · block |
| **Right rail Workflow Pulse** | Top of rail: 3–4 one-line workflows; expand semantic over decorative viz when live |
| **Desktop MISSIONS** | Replace decorative VIZ when workflows live; idle = dim “no agentic work” |
| **Swarm panel** | ASCII DAG using deps (not only flat list) |
| **Board panel** | 3-up glance counts (backlog/active/done) on one line per board |

### Stage color language (global)

| Stage | Glyph | Theme key |
|-------|-------|-----------|
| plan | ◇ | dim |
| act | ● | warn |
| wait | ⏳ | warn |
| blocked | ⊘ | err |
| verify | ◈ | accent |
| done | ✓ | ok |
| failed | ✗ | err |

## Implementation steps

1. `src/aion/workflows.py` — pure collect + render (no Textual).
2. `dashboard.py` — `workflows: list[dict]` on `DashboardData`.
3. `store.py` — pass swarm / board / registry context into collect when building desktop.
4. `ui/app.py` — header strip, right-rail pulse first, desktop MISSIONS, swarm DAG, board glance.
5. `tests/test_workflows.py` — collector + render unit tests.
6. Gates: pytest (ignore term), boot smoke.

## Premortem

- **Wrong product?** No — this is glanceable status, not chat.
- **Break splitscreen?** Cap pulse to 4 rows; pipeline strip ≤ ~40 chars.
- **Namespace?** New module `workflows.py`; do not clobber `vault`/`health`/`sysinfo`.
- **swarm_dashboard type drift?** Already str|dict; collector normalizes both + orchestrator agents.
- **Rollback:** one logical commit; explicit paths only.

## Verify

```bash
cd /home/gio/aion
.venv/bin/python -m pytest tests/test_workflows.py tests/test_desktop_home.py tests/test_hud.py -q
.venv/bin/python -m pytest tests/ --ignore=tests/test_term.py -q
TERM=xterm-256color timeout 6 .venv/bin/python -m aion.ui.app
```

## Completed

| Date | Work |
|------|------|
| 2026-07-19 | All 6 steps: `workflows.py`, `dashboard.py`, `store.py` wiring, 4 UI surfaces, 51 tests |
| 2026-07-19 | External agent detection (opencode, agy, claude, codex via `pgrep`) + type summary in header + right-rail live count |

## UX Audit (2026-07-19)

### Premortem findings

1. **Cognitive overload** — 9 workspaces × 4 panels × 20+ hidden commands. No clear next action on boot.
2. **Hidden commands** — "just type what you want" means the user has to guess what to type. 20+ commands, zero discoverability.
3. **Meta-TUI** — `┌── BOXES ──┐` drawn inside Textual, which runs inside a terminal. 3 UI layers for one status bar.
4. **Right-rail junk drawer** — viz + workflow pulse + observer + live tasks + telemetry + tokens + live agents. Everything crammed.
5. **6-second boot sequence** — cinematic waste before any utility.
6. **Two competing paradigms** — keyboard shortcuts AND Ctrl-K AND wizard AND tour. Four learning paths, none polished.
7. **Hermes hostage** — half the HUD dies without `~/.hermes/state.db`.

### Simplicity target

- Boot to value in < 1s. No cinematic sequence.
- Single interaction model (shell-style `▶` prompt over hidden Ctrl-K palette).
- Right rail shows ONE thing: workflow pulse or live tasks, not everything.
- Remove one layer of TUI nesting.
- Visible command bar instead of "just type what you want."
- Graceful degradation when Hermes is absent.

## Next cycle (immediate)

1. Kill boot sequence — straight to desktop, no cinematic intro
2. Show persistent `▶` command bar in footer (visible by default, not hidden Ctrl-K)
3. Clean right rail — move observer/tokens/live tasks into center, right rail = pulse only
4. One interaction model: keyboard nav + visible prompt line

## Further follow-ups

- Web HUD same JSON + DAG canvas
- Semantic spectrum labels PLAN/TOOL/CODE/WAIT/DONE from step logs
- Focused workflow key routing (Enter expands mission graph)
