# AGENTS.md — aion

## Product (read first)

**aion = splitscreen HUD + application desktop.** Not an AI assistant.

Full identity: [`docs/IDENTITY.md`](docs/IDENTITY.md).

When in doubt:

- Prefer **status, layout, app lifecycle, deck, harness wiring**
- Deprioritize **persona lines, proactive chat, “Jarvis” wit**

## Repo

- Path: `/home/gio/dev/aion` (branch `main`; old path `/home/gio/aion` is a symlink)
- Package: `src/aion/`
- Config: `config/layout.json`
- Tests: `tests/` via `.venv/bin/python -m pytest`
- Deck firmware: separate repo `/home/gio/dev/CyclUno` (shared v2 frame protocol; old path `/home/gio/CyclUno` is a symlink)

## Development Workflow
All development happens via randomesh ssh connection on machine pansa (192.168.1.15).
Use `ssh pansa-ts` to connect. All code changes, builds, and tests are performed
on the pansa machine through the randomesh framework. See `today_pansa.log` for
details on the omo vs pansa distinction.

## Architecture map

| Layer | Files | Rule |
|-------|--------|------|
| Intent bus | `core.py` | Only cross-module command channel |
| Brain | `store.py` | Owns harnesses, tasks, routing |
| Backends | `harnesses.py` | App/shell/term/web/… — workers, not chat UI |
| Desktop data | `dashboard.py` | Pure snapshot for landing HUD |
| TUI | `ui/app.py`, `ui/gauges.py` | Render only; emit Intents |
| Deck | `deck/*`, `input.py` | Serial → Intent / uinput gamepad |
| Optional I/O | `voice/*`, `llm.py` | Secondary; must not own product identity |

## Factory gates (every cycle)

```bash
cd /home/gio/dev/aion
.venv/bin/python -m pytest tests/ --ignore=tests/test_term.py -q
TERM=xterm-256color timeout 6 .venv/bin/python -m aion.ui.app   # boot smoke
# if deck/protocol touched:
.venv/bin/python -m pytest tests/test_deck.py -q
# if HUD data layer touched:
.venv/bin/python -m pytest tests/test_hud.py -q
```

Never `git add -A`. Stage explicit paths. One logical cycle per commit.

Enforced, not just written down — enable the hook once per clone:

```bash
git config core.hooksPath .githooks   # refuses stray paths, credential
                                      # filenames, blobs and real-looking keys
```

Rules live in `scripts/commit_guard.py` (pure, tested in
`tests/test_commit_guard.py`) and run again in CI, because a hook can be
skipped with `--no-verify` or simply never enabled in a fresh clone.

Work on `main`. The `freerouting-omniroute` and `slice/*` branches are
historical; everything is merged.

## Skills to load

- **`aion-factory-loop`** — cycle SOP for this product
- **`aion-deepsearch`** — research for HUD/desktop (not assistant) patterns
- **`aion-build`** — code recipes + pitfalls

Generic Hermes `factory-loop` / `deepsearch` apply only where they don’t contradict identity.

## Anti-goals (do not “improve” toward these)

- Making aion primarily a chat companion
- Coupling product success to TTS personality
- Treating Cyclops wearable features as aion’s core UX
- Expanding assistant tools when app-management / splitscreen density is broken
