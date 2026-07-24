# Aion — Improvement Plan

Multi-harness TUI cockpit that orchestrates coding agents (opencode / claude /
droid) in Ralph-style **factory loops**, scores runs with a physis coherence
brain, discovers peers over a LAN mesh, and mirrors state to a web HUD (PWA /
APK). Pure-engine + harness architecture. Three parts: premortem, dev plan,
business plan.

---

## 1. Premortem — "It's 12 months out and Aion failed. Why?"

Ranked by likelihood × blast radius.

### P0 — The repo eats its own tail (multi-writer chaos)
This is the top *operational* risk and it has already fired. Multiple actors
commit to the same branch between sessions: the human, a second agent ("Aion"),
and an autosave cron. Consequences already observed:
- A blanket `git add -A` twice swept another agent's untracked WIP into a
  commit — once **pushing a HIGH-severity SSH-key-leaking `node.py` to a public
  remote.**
- Untracked notes (`pi-opencode-remote.md`) reappear and risk re-capture.

No branch discipline + no staging discipline + autonomous committers = the
project's own history becomes an attack surface and a source of leaks. **This
kills the project via a security incident, not a code bug.**

### P1 — Factory loops are RCE by design; the guardrails are young
`factory.py` runs agent-authored shell commands in a loop. The engine is clean
(`shlex.quote` on data, `max_iters` hard cap, stall/novelty guard to stop a
spinning loop). But the *thing it runs* is an LLM deciding what to execute. A
prompt-injected or confused agent burns budget doing damage. The stall guard
stops *waste*, not *harm*. If Aion ever runs against untrusted input (a remote
prompt, a scraped issue), the blast radius is the whole host.

### P2 — Mesh trusts peer-reported data
`agents/node.py` is freshly hardened (fail-closed bind, `hmac` auth, SSH
material stripped from peers) — good. Residual: a peer is keyed by its *self-
reported* `host`, so a malicious peer claiming another node's IP overwrites
that node's inventory via `found[i].update(safe)`. Low severity (inventory
only, no SSH), but it's the seam where "trusted LAN" assumptions leak.

### P3 — Secrets live in the open next to an auto-committer
`~/gemini_api_keys.txt`, `~/github_token.txt`, `~/groq.txt` are plaintext in
home. Combined with P0 (autonomous `git add` actors in a repo rooted near
them), the failure mode writes itself: a key gets swept into a commit and
pushed. The git-credential fix (env token shadowing) shows this class of
problem has already bitten once.

### P4 — `ui/app.py` is a 2056-line single file
The cockpit's core is one massive module. As harnesses multiply, this is where
velocity dies and regressions hide. Not fatal, but it's the tax that compounds.

---

## 2. Development Plan

### Now — make the repo safe to commit into (days)
1. **Codify staging discipline in tooling, not memory.** Add a
   `pre-commit` hook that **refuses `git add -A`-style broad stages** and warns
   on any staged path under `src/aion/agents/` or matching `*token*`/`*key*`/
   `*.pem`. The rule "stage explicit paths only" must live in the repo, not in
   an agent's head.
2. **Give the writers lanes.** Each autonomous committer gets its own branch
   (`aion/autosave`, `agent/aion`); human + review work on `main`/feature
   branches; merges are explicit. Kills the clobber/leak-by-collision path.
3. **Move secrets out of home + out of git reach.** `gemini/github/groq` keys →
   a single gitignored `~/.config/aion/secrets.env` (0600) or OS keyring;
   `.gitignore` hardened with `*token*.txt`, `*.key`, `*api*.txt`.

### Next — contain the factory (weeks)
4. **Sandbox the loop.** Run `run_cmd` inside a constrained profile (dedicated
   user / container / `bwrap`) with an allowlist of writable paths. `max_iters`
   caps *count*; this caps *reach*.
5. **HITL on destructive intent.** `hitl.py` gates already exist. Route factory
   commands matching a danger pattern (`rm -rf`, `git push`, `curl … | sh`,
   credential paths) through an approval gate before exec. The infrastructure
   is built; wire it to the loop.
6. **Close the mesh identity seam (P2).** Bind a peer's reported `host` to the
   socket's actual source IP before trusting it as a merge key; reject
   mismatches. Cheap, removes the inventory-spoof path.

### Later — scale the cockpit (months)
7. **Decompose `ui/app.py`.** Extract per-panel modules behind the existing
   pure-engine boundary (the factory/physis engines are already clean — apply
   the same split to the view layer). One file → panels + a thin router.
8. **Physis coherence as a real signal.** It's telemetry-only today
   (`coherence_fn` never gates the loop). Once trusted, let sustained low
   coherence trigger an early stop or a harness switch — turn the score into a
   control input, not just a HUD number.

### Testing / CI
The pure-engine design is already test-friendly (`run_cmd`/`check_cmd`
injected). Add: a secret-scanner in CI (gitleaks) gating every push — given P0,
this is the highest-leverage single control. Plus the pre-commit stage-guard
from #1 as a fast local mirror of it.

---

## 3. Business Plan

### What it is
A **local-first orchestration cockpit for coding agents.** You bring the
harnesses (opencode, claude-code, droid); Aion runs them in disciplined
loops, knows when a loop is stuck (stall/novelty detection), scores output
quality, gates risky actions (HITL), and shows it all in a TUI + phone HUD.
It's the control plane, not another agent.

### Who cares and why
- **Solo builders / small teams running agent fleets** who are today
  babysitting terminals. The value is *unattended-but-safe*: loops that stop
  themselves when spinning and ask before doing something dangerous.
- **Agent-tooling power users** who want harness-agnostic orchestration instead
  of being locked to one vendor's runner.

### Wedge
The market is filling with *agents*. Far fewer people are building the **safe
orchestration layer** around them — the part that answers "how do I let this
run for an hour without it burning tokens on a stuck loop or `rm -rf`-ing my
repo?" Aion's stall guard + HITL gates + physis scoring are exactly that layer.
The moat is operational trust, and it's harness-agnostic by design (a feature
no single-vendor runner will build).

### Model
- **Open-core cockpit.** The engine + TUI open source (adoption + trust);
  hosted/team features paid.
- **Aion Fleet** (the money): multi-machine orchestration via the mesh,
  shared HITL approval inbox, run history/analytics, coherence dashboards
  across a team's agent runs. Priced per active machine/seat.
- **Design-partner services** early: help teams stand up safe agent loops;
  fund the product, learn the real danger patterns to gate.

### Sequence to first dollar
1. Close P0 (repo can't leak) and P1/P4 of the sandbox — *you cannot sell
   "safe orchestration" from a repo that pushed an SSH leak twice.* Fix the
   house first.
2. Package the single-machine cockpit as a clean install; ship the stall-guard
   + HITL story as the headline.
3. Sell **Fleet** to the first few multi-agent teams as design partners —
   their pain (babysitting, runaway loops, "who approved that push?") is the
   feature list.
4. Convert to per-seat once the approval-inbox + run-analytics surfaces are
   real.

### Biggest business risk
It's the same as premortem P0/P1: Aion's entire pitch is *safety and control*.
A single "Aion's own loop / autosave leaked my key / nuked my repo" story
invalidates the whole value proposition. The product's credibility is bounded
by its own operational hygiene — so hygiene (branch lanes, secret isolation,
sandbox, CI secret-scan) is not overhead here, it's the product proving itself.
