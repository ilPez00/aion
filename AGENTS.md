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
All development happens via randomesh ssh connection on machine pansa
(`100.65.59.18` over Tailscale; `10.42.0.201` on the omo<->pansa link).
`192.168.1.15` was pansa's address before 2026-09-29 and is dead — see
`randomesh/docs/INCIDENT_2026-09-29_pansa_offline.md`.
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
| Local tools | `github_growth/` | Legitimate, approval-gated GitHub engagement |

## GitHub Growth subsystem (`github_growth/`)

A legitimate, non-spam GitHub profile/project discovery and engagement toolkit.
All actions are **dry-run by default** — use `--live` to actually follow/unfollow.

- Token resolution: `CredentialStore` → `GITHUB_TOKEN` env → `GITHUB` env → `~/.env` `GITHUB=`
- Safety: kill switch (`enabled=False` default), daily limits (5 follow/day, 10 total/day),
  bot/org exclusion, allowlist/blocklist, min followers/stars thresholds
- Scoring: 5-factor weighted (contribution 25%, engagement 25%, quality 20%,
  spam-inverted 15%, network 15%) with configurable `min_score_to_queue`
- Audit: all actions logged to `~/.aion/github_growth.db` (SQLite)
- Commands: `github-growth discover | enrich | score | queue list|approve|clear | run | follow | unfollow | execute | report summary|user|metrics | stats | config`
- Tests: `tests/test_github_growth.py` (34 tests)

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
## Resource discipline & engineering workflow (added 2026-11-10 after OOM hard reset)

Last crash (2026-11-10 09:19): kernel OOM killer terminated physis-pro-web (547 MB RSS), syncthing, gh, python3. Root cause: too many concurrent cargo/physis processes + heavy build caches on the root fs; SSH became unreachable. Prevent recurrence.

**Before ANY resource-intensive task (cargo build, physis observe on large corpus, node build, model load, benchmark):**
1. Check running tasks & resources first: `ps aux --sort=-rss | head; free -h; uptime; pgrep -c cargo; pgrep -c physis`. If load > 0.8*cores OR free RAM < 4 GiB OR >3 cargo/physis processes running → STOP: wait, kill stale ones, or delegate.
2. GPU/CPU-intensive work (cargo builds, model inference, large observes, heavy tests) → delegate to **omo** worker (ssh omo-ts, 100.116.39.57, Ryzen 9 6900HX / RX 6650 XT 8G). Use ssh+tmux or nerve_remote_delegate_task. Never stack multiple heavy builds on pansa.
3. Cache-intensive processes (cargo target/, node_modules, .venv, .physis snapshot-cache.json ~237 MB, target/ dirs): keep on /mnt (bigstore) or external volume, symlink into place; export/clean temp caches before periodic reboots so root fs stays <85% and reboot recovery is fast. Check `/var/lib/docker` (root fs) <95% before restarting services (disk guard).

**Engineering workflow (applies to physis-next, aion+randomesh, praxis-webapp):**
Every feature, implemented or not, must pass through, with a written artifact per stage:
1. Planning — feature spec in docs/ or packets/ (what, why, ICP, scope)
2. Evaluation — feasibility/cost/risk note; query physis world model FIRST (`physis observe`, `physis context`, `physis query`) before inventing
3. Development — isolated worktree/branch; use physis world model for context packets instead of manual grep
4. Integration — merge candidate only after gates green
5. Unit + integration testing — cargo test / pytest / jest / vitest; no skips
6. GUI & settings exposure — surface in UI/settings if user-facing; document config
7. Refinement — clippy/lint, perf, docs, commit-message quality

**Periodic hygiene (every session / every few commits):**
- Commit + push after each completed feature unit (no accumulated dirty trees). Branch hygiene: feature branches → PR.
- Report open workflows/active loops (cargo processes, physis serve instances, timers, agent loops) + dev notes in the repo tracking doc (DEV_PLAN.md / packets/ / CURRENT.md).
- Auto-write PRs to expand physis capabilities when a new primitive/pattern is discovered (physis primitive discover + physis act preview/do, then gh pr create --draft). Leverage physis world model as much as possible for code context.
- Cache-intensive processes exported from temp/cache for periodic reboots (see point 3).

**Auto-write PRs:** when a capability gap is found, prefer opening a draft PR with tests over leaving a TODO.
