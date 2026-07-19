# aion — Agentic HUD OS

**aion = splitscreen HUD + application desktop.** 8 unified workspaces.

## Workspaces

| # | Workspace | Absorbs | Content |
|---|-----------|---------|---------|
| 1 | Desktop | Projects | System status, launcher, todos, sessions, data, GPU, agents, projects, activity, quick commands |
| 2 | Models | — | Harness list (vram/tier/running) |
| 3 | Tasks | Board | Task registry (progress/history) + kanban boards |
| 4 | Agent | Agents + Swarm | Agent entity cards + swarm dashboard + LLM chat |
| 5 | Vault | Memory | Notes graph + memory facts |
| 6 | System | Physis + Health | CPU/RAM/disk/net/GPU + processes + health + coherence brain |
| 7 | Term | — | Live embedded terminal |
| 8 | Settings | Skills + Hermes | Provider env vars + installed skills |

## Verification

```bash
cd /home/gio/aion
.venv/bin/python -m pytest tests/ --ignore=tests/test_term.py -q
TERM=xterm-256color timeout 6 .venv/bin/python -m aion.ui.app
```
