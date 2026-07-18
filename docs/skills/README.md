# aion agent skills

Product identity lives in-repo:

- [`../IDENTITY.md`](../IDENTITY.md) — HUD / desktop, not assistant
- [`../../AGENTS.md`](../../AGENTS.md) — factory gates for contributors/agents

Hermes skills (loaded by aion `SkillLoader` from `~/.hermes/skills/`):

| Skill | Path | Use |
|-------|------|-----|
| **aion-factory-loop** | `~/.hermes/skills/aion-factory-loop/` | Ship cycles: PDCA + identity gate |
| **aion-deepsearch** | `~/.hermes/skills/aion-deepsearch/` | Research HUD/desktop patterns |
| **aion-build** | `~/.hermes/skills/aion-build/` | Code map + pitfalls |

Parent SOPs (compose, don’t override identity):

- `factory-loop` — generic autonomous production
- `deepsearch` — generic multi-source research

In aion TUI: open **Skills** workspace and search `aion-`.