# aion — plan & status

**aion = splitscreen HUD + application desktop + orchestrator.** Workspaces are
config-driven (three shipped by default, ten in a full install), driven by one
`Intent` bus across keyboard / joystick / voice / CyclUno deck / Colmi ring.
See `docs/IDENTITY.md` for what the product is and what it refuses to be.

## Workspaces

The eight below are the panels with real content. A full install also carries
**Runs** (loop history) and **Net** (peers/tunnels); the shipped default config
enables only Models / Tasks / Agent.

| # | Workspace | Absorbs | Content |
|---|-----------|---------|---------|
| 1 | Desktop | Projects | System status, launcher, todos, sessions, data, GPU, agents, projects, activity, quick commands |
| 2 | Models | — | Harness list (vram/tier/running) |
| 3 | Tasks | Board | Task registry (progress/history) + kanban boards |
| 4 | Agent | Agents + Swarm | Agent entity cards + swarm dashboard + LLM chat |
| 5 | Vault | Memory | Notes graph + memory facts |
| 6 | System | Physis + Health | CPU/RAM/disk/net/GPU + processes + health + coherence brain + **CYCLOPS panel** |
| 7 | Term | — | Live embedded terminal |
| 8 | Settings | Skills + Hermes | Provider env vars + installed skills |

## Ecosystem

- **aion** — the cockpit (this repo): Textual TUI + web HUD, harness engine, physis brain.
- **Cyclops** — the wearable/physical trio feeding aion:
  - **Cycluno** — Arduino deck (serial): joysticks + buttons + MODE. `deck/{link,protocol,gamepad}.py`. *(No rotary encoder — removed; the PRS is stale on this.)*
  - **Colmi R02 ring** (BLE): HR/SpO2/battery telemetry + accel-derived taps. `deck/ring.py` + `input.RingInput`.
  - **XIAO pendant** (cam/mic): scaffold only. `deck/pendant.py`.

## Status — done

- Core cockpit: harnesses, Intent bus, Fleet, remotes, 494+ tests.
- **Factory loop** + **DeepResearch loop** (pure engine + thin harness).
- **Factory stall detection** — bails a spinning Ralph loop (`STOP_STALLED`), pure/local.
- **physis_pro brain wired into both loops** — per-iteration coherence (opt-in),
  outcome recorded (+1 flow / −1 block), research runs ingested into the holarchy.
  Coherence is now a **control input**, not only a HUD number: `coherence_window`
  ends a run that has drifted (`STOP_INCOHERENT`) — the failure novelty cannot
  see, because fresh output about the wrong thing looks maximally novel right up
  to the budget. Opt-in, off by default, cannot fire when the brain is
  unreachable (0.0 means "no reading", not "bad"), loses ties to the local stall
  signal, and can only end a run — never fail one, never start one.
  See `docs/physis-integration.md`.
- **Colmi R02 ring** — telemetry + `TapDetector` "button" + `CYCLOPS` HUD panel.
  Verify offsets against hardware: `python scripts/verify_ring_accel.py`.
- **HITL approval gates** (`hitl.py`) — fail-closed; a pending gate captures the
  ACTIVATE any device emits (deck/joystick/ring/Enter) as approve, BACK as reject;
  destructive prompts + `requires_approval` harnesses gate `_spawn`. Voice + HUD wired.
- **Web HUD is a PWA** — installable; LAN-reachable via `AION_WEB_HOST=0.0.0.0`.
  See `docs/install-mobile.md`.
- **Graph file manager** (`fsgraph.py` + `/api/fs/*`) — physis-compatible
  payload, local TF-IDF + spherical k-means clustering, sandboxed to
  `AION_FS_ROOT`. Preview + move from the HUD.
- **Web HUD rebuilt around one organic graph renderer** — Files, Vault and
  System are the same canvas force layout with different adapters; each has a
  keyboard/screen-reader list twin. Responsive to 375px (bottom nav + bottom
  sheet inspector), WCAG-AA palette shared with the TUI, reduced-motion aware.
- **Launcher usability** — `./aion.sh graph|doctor|help`; `AION_WEB_PORT`
  honoured (PTY websocket follows at +1) so two HUDs can coexist.
- **Agent process graph** (`procgraph.py` + `/api/agents`) — fleet instances,
  harnesses, tasks and swarm deps as one organic graph, reconstructed from the
  on-disk checkpoints so it works with no cockpit running. Orphan harnesses
  surfaced rather than dropped.
- **Ctrl-K palette** (`/api/search/all`) — one search across modules,
  harnesses, tasks, task logs, instances, notes, filenames and file contents;
  every hit jumps to its node.
- **Navigation** — hash routing + browser back/forward, clickable
  breadcrumbs, Backspace to climb, click-a-hub to isolate a cluster,
  directional arrow traversal, Tab to walk graph neighbours.
- **Live push** (`procgraph.fingerprint/diff` + `/ws/events`) — the daemon
  watches the cockpit's checkpoint files and pushes deltas; the graph patches
  in place (no rebuild, pan/zoom/selection preserved) and pulses state
  transitions. Idle cost is a hash of a few small files at 4Hz, nothing sent.
  Falls back to polling with a visible liveness dot.

- **Swarm persistence** (`SwarmStore`) — the dependency DAG survives a restart
  and is visible cross-process; in-flight agents restore as IDLE.
- **Worktrees** (`worktrees.py` + `/api/worktrees`) — repos/worktrees/branches
  as a graph, tasks linked to the tree they mention, parallel scan. Read-only.
- **Obsidian** — the web Vault now resolves the real vault
  (`AION_VAULT` > recorded setup answer > repo `notes/`); fixed a path
  traversal in `/api/notes/content` while wiring it.
- **Open in editor** (`opener.py`) — allowlisted editors (zed/code/…),
  argv-only, sandboxed paths.
- **Notifications** (`notify.py`) — opt-in webhook for failed/stalled/gate
  events only, deduped, never raises, URL redacted from logs.

- **Cross-instance routing** (`routing.py` + `/api/route`) — drag a task onto
  an instance to run it there. Fail-closed (no dispatch without `confirm`),
  target resolved from discovery only, every decision explained with a score
  breakdown and per-candidate rejection reasons.

- **TUI surfaces in the web HUD** (`bridge.py`) — Desk (todos/memory/apps/
  modes/agents/profile), Board (kanban), Settings (providers/skills/paths) and
  a real PTY Term. Reads the same `~/.aion/shared/` stores the cockpit writes;
  todos and memory are editable from the browser.
- **One-command run** — `./aion.sh up` (physis + HUD + browser), `down`,
  `status`.

- **HITL gates in the web HUD** (`hitl.GateStore` + `/gate` transport) —
  pending gates published for cross-process display and answered over the
  authenticated transport. The published file is display-only; a forged
  approval in it releases nothing.

- **Voice control of the web HUD** (`voicecmd.py` + `/api/voice`) — spoken
  phrases drive navigation, scanning, filtering, isolation and view controls.
  Voice can DENY an approval gate but never grant one; low confidence is shown,
  not executed.
- **Conversational voice** — continuous listening with spoken replies, and an
  LLM fallback (`voicecmd.understand`) that maps ANY phrasing onto a validated
  action. The model's reply is untrusted: schema-checked, args whitelisted,
  gate decisions forced to reject. `./aion.sh up` opens Chrome, since the Web
  Speech API is Chrome-only.

- **SSH peers** (`sshlink.py` + `./aion.sh peers` + `/api/peers`) — aion
  instances on OTHER machines, reached through an `ssh -L` tunnel so the
  existing HTTP transport, routing and gates work unchanged and neither end
  binds a public interface. Per-peer keys, `restrict,permitopen=` in
  authorized_keys (no shell, no pivot), argv-injection validation, orphaned
  tunnels reaped. Peers appear in the Agents graph and as routing targets;
  dispatch is still fail-closed. See `docs/ssh-peers.md`.

- **Swarm across machines** — a step can name an instance (`instance_for` in
  the add form) and runs there via the existing token-authenticated transport,
  while the rest of the DAG runs locally. Remote work cannot announce itself on
  this process's bus, so it is polled per task (`GET /task?id=`) rather than
  read from `/status`, whose 20-task cap drops exactly the completions the
  watcher exists to see. A silent poll is a miss, not a failure — four
  consecutive misses before a peer counts as lost, so a sleeping laptop does
  not cancel live work. Spawn and poll are non-blocking (`PENDING` +
  `attach`/`deliver`): the cockpit calls these from inside its own event loop.

- **A budget stops a swarm that is working perfectly** (`swarmbudget.py`) —
  admission capped parallelism and VRAM, neither of which is money, so a DAG
  left running unattended could sit inside both limits and spend all night.
  Reserve-then-reconcile: the prompt is known before a step runs and the output
  is not, so admission holds an estimate and the real figure replaces it at the
  end. Without the hold, every step admitted in one tick sees the same
  "committed so far". Failed-but-ran settles; never-started and cancelled
  release. It is a governor and not accounting — external CLIs report stdout
  and an exit code, not tokens, so the figures are characters over four times a
  configured price and every payload carries `estimated: True`.

- **The browser shows the same swarm condition, in the cockpit's words** — the
  HUD's process graph is rebuilt from checkpoints on disk, and budget, slots
  and retry state live in the running cockpit, so none of it could appear
  there. The Agents view now asks the instance for `status` and renders a
  standing Swarm pane: the condition sentence, the limits, and one block per
  dead letter with retry/remove wired to the same verbs the TUI uses. The
  strings are composed by the cockpit (`swarmview`) and printed verbatim — two
  renderers each phrasing "nothing is running" their own way is how a cockpit
  starts contradicting itself, and a static test now fails if the JS starts
  assembling its own. `SwarmRunner.status()` also carries the agent list: only
  the bus publish ever attached it, so anything reading status() alone saw a
  swarm with no steps in it.

- **The governor's numbers are on screen, not only in its refusals** — the
  budget could stop a swarm and the slot/VRAM limits could hold ready steps
  back, and neither showed a figure anywhere, so the first time an operator
  met either was as a refusal. The panel now carries `N/M slots · vram x/yG`
  and `~$0.12 of $1.00 (12%) est`, with retry spend broken out because retries
  are exactly what a budget exists to bound. Sub-cent resolution below ten
  cents: two decimals read "$0.00" for the whole early life of a swarm, and a
  cost display that says zero while spending gets believed. Dead letters get
  their own block, leading with what each failure is holding up — "what
  failed" is already on the row above; "what is stuck behind it" is what
  decides fix-now from fix-Monday.

- **A loop's opinion of itself now leaves the process** (`core.py`,
  `organic.js`) — the factory harness has written `task.coherence` every
  iteration since coherence scoring existed, and `Task.as_dict()` never carried
  it. So it died where it was computed: the web HUD, a peer polling `/task`,
  the process graph all had a progress bar and nothing about whether the work
  was going anywhere. A loop 80% through its budget producing fluent nonsense
  looked exactly like one about to succeed. Now on the wire, and on the edge
  feeding each task — coherent work travels a taut, brighter line, drifting
  work sags further and dims. The load-bearing part is the encoding: `null`,
  never 0.0, when nothing scored the loop. A disabled brain, an unreachable
  one and a harness that does not score all report 0.0, so transmitting that as
  a measurement paints an unscored loop as maximally incoherent — a verdict
  invented out of an absence, and the same trap `swarmlive` avoids with
  "never heard from" versus "went quiet". Every visual change sits inside the
  `!== null` guard, so a graph with no scoring anywhere looks untouched, and
  the pulse honours reduced-motion.

- **CI had never been green** (`.github/workflows/ci.yml`) — 25 of 25 runs red,
  going back as far as the API lists. The `tests` job was fine; the `guard` job
  ran `python -m pytest` with no install step and ended every run in "No module
  named pytest". It had been failing since it was written, so the rules that
  stop a blind `git add -A` from sweeping credentials into a commit — the exact
  guard this repo has needed twice — had never once executed. A job that fails
  for a setup reason looks identical to one that fails for a real one, and a
  red X that is always there stops being read. Now checked rather than trusted:
  a test parses `ci.yml` and fails if any job runs a Python tool it never
  installs. `pyyaml` is a declared dev dependency for it, because skipping that
  test when the import is missing would recreate the failure it exists to
  catch.

- **The model was being told to say things nothing accepts** (`interpret.py`,
  `voicecmd.py`) — the plain-language translator's prompt listed `goto` targets
  `memory`, `sys`, `hermes`, `skills`, `projects` and `swarm`, none of which
  are workspaces, and omitted `runs` and `net`, which are. A model obeying that
  prompt exactly produced "goto: no workspace 'sys'". Neither half could see
  it: the prompt was self-consistent, the dispatcher was self-consistent, and
  the disagreement lived between them — the same shape as the `ToolEnv` crash
  and the dead `run` branch. Both prompts now DERIVE their vocabulary from the
  thing that validates it (`workspace_ids()` off the live config, the app list
  off `APP_SYNONYMS`, the voice module list off `MODULE_WORDS`), because a
  hardcoded vocabulary is only correct on the day it is typed and nothing about
  it being wrong later is visible from either end. Tests compare rendered
  prompt against source of truth in both directions, including that every verb
  the prompt asks for survives `llm_translate`'s allowlist rather than being
  requested and then discarded. Also pinned: voice can still never be told to
  approve a gate.

- **The agent chat crashed on every message** (`agent.py`, `llm.py`) — `_chat`
  built its `ToolEnv` with `think=`, a keyword the dataclass has no field for,
  so constructing the tool environment raised `TypeError` before the model
  call, before any tool, before anything that could catch it. Talking to the
  cockpit's own agent has been impossible for as long as the `think` tool has
  existed. It was wired at one end out of four: the tool surface is a contract
  between the prompt in `llm.py` that tells the model which tools exist,
  `ToolEnv`'s fields, `execute`'s dispatch, and the environment the store
  builds — and `think` was in none of the first three. Now in all four, with a
  set-comparison test in each direction, because the interesting failure is
  never "this tool is wrong", it is "these two halves of one contract are each
  correct alone". The existing tests could not have caught it: they assert
  each tool's RETURN STRING, and every one passed while `_agent_run_tool` was
  emitting a command the dispatcher could not parse. Right answer, wrong
  effect, nothing joining the two.

- **The dead-branch class is now checked mechanically**
  (`tests/test_split_bounds.py`) — having found two branches that indexed past
  their own `split(" ", 1)`, the question worth answering was whether there
  were more. Nothing existing could answer it: the type checker is happy (the
  code is well-typed), the tests cannot be (a dead branch has no behaviour to
  assert on), and review would need someone holding a split from a hundred
  lines earlier. It is a two-line arithmetic fact about one function, so it is
  checked as one — an AST pass that finds every local assigned from a bounded
  split and fails on any `len(v) >= k` or `v[i]` in the same function that the
  bound makes impossible. Function-scoped, because `_agent_command` splits
  without a limit two methods away and a file-wide check would call its
  `parts[3]` a bug. Run against the pre-fix commit it reports exactly the two
  known sites and nothing else; against the tree, nothing. The first version
  used "narrowest binding wins" and produced a false positive on working code
  where one name is split two different ways in two branches that each return
  — so the rule is the nearest binding ABOVE the use, and that false positive
  is now a test. Calling working code dead is the worse failure of the two.

- **Two command branches that could never fire** (`store.py`) — `_run_command`
  opens with `parts = text.split(" ", 1)`, so `parts` is never longer than two.
  Two branches were written against a list split on every space, and neither
  had ever executed. `run <harness> <prompt>` tested `len(parts) >= 3` and read
  `parts[1]`/`parts[2]`; it fell through to the final fallback instead, which
  spawns the ACTIVE harness with the whole line — so `run claude explain X` ran
  on whatever happened to be selected, with "run claude " still inside the
  prompt. `_agent_run_tool` emits exactly that form, which means the model's
  only way to choose a harness has never worked and every prompt it sent was
  prefixed with the command that sent it. `setup set KEY VAL` tested
  `len(parts) >= 4` and fell to the scope parser, printing a usage line; the
  env writer behind it had never run once. Neither is visible from the branch
  itself — you have to be holding a split from a hundred lines earlier — so
  the tests go through `_run_command` with real text rather than calling the
  helpers. Making the env writer reachable also made it the first version of
  that code to actually create `~/.env`, so it now forces 0600, collapses a
  pre-existing duplicate key rather than shadowing it, and keeps the VALUE out
  of the logs, which are on screen and published to the HUD. This also
  corrects the previous cycle: the missing `Path` import was real for
  `_load_skills_data`, which genuinely ran and failed, but the `setup set`
  half of that claim was wrong — that code was unreachable, not broken.

- **`store.py` is 560 lines smaller** (`storeswarm.py`) — it had reached 2093
  lines, and the largest single thing in it was the swarm: the lazy runner and
  its five policies, the remote spawn/poll/control hooks, the typed `swarm`
  verbs, plan/apply, the replan tick. One question — how does a plan get run —
  answered in the middle of a file that also handles todos, env vars, chat,
  boards and the task registry. Moved as a mixin rather than redesigned: every
  method keeps the `self` it had, no call site changes, and the diff is a
  relocation that can be verified method-by-method rather than a reshuffle
  where a regression cannot be bisected. The module docstring names every
  Store attribute it reaches for instead of pretending to be decoupled, since
  it is not, and a test pins that list. The lint gate from the previous cycle
  earned itself immediately here: the moved code used `asyncio` through
  `store.py`'s module-level import, and `F821` caught it before the tests ran.

- **A lint gate, and the four live bugs that justified it** (`ruff` in CI) —
  nothing in this repo ever checked for names that do not exist, and four had
  shipped. `store.py` never imported `Path`, so `_load_skills_data` raised
  NameError on its third line into an `except Exception: pass` and the Skills
  panel was permanently empty with nothing said about it — a hard breakage made
  indistinguishable from "this machine has no skills installed". The same
  missing import sat in `setup set KEY VAL` — though that turned out to be a
  smaller claim than it first looked, and the next cycle corrects it: that
  branch could never run at all, for an unrelated reason. `remote add` referenced
  `RemoteNode`, which two OTHER methods in the same class imported locally and
  that one did not, so the only command that CREATES a remote could not run.
  And the web PTY editor called `shlex.quote` with no `shlex` imported — the
  one line in that handler doing shell-injection quoting was the broken one.
  All four share a shape tests structurally cannot catch: an undefined name in
  a branch nothing covers, behind a handler that swallows it. The ruleset is
  deliberately narrow — pyflakes, syntax errors and the bugbear checks that
  find real defects — with four documented relaxations and a test that pins
  that list by exact set, because a gate whose exclusions grow by quiet append
  converges on selecting nothing. Style is not the target: this codebase's
  formatting is a house style and a linter that argues with it is one people
  learn to skip.

- **A running step can now be asked whether it is still alive** (`swarmlive.py`)
  — `stalled()` opened with `if self._in_flight(): return ""`, so the one
  condition it refused to diagnose was the one that kills a swarm quietly: a
  step stuck in WORKING. A wedged subprocess, a sleeping peer, a model call
  that never returns — the agent stayed WORKING, every panel reported a healthy
  busy swarm, and the DAG waited forever on a step nothing was running. Nothing
  timed it out because nothing was watching: harnesses report progress through
  `set_progress`, and the swarm threw every one of those updates away
  (`on_task_state` returned early on "running", and the store's `cur != prev`
  guard dropped the rest), so a step's progress was 0.0 right up to the moment
  it was 1.0. The load-bearing distinction is between two silences — a step
  that NEVER reported says nothing at all, because plenty of CLI harnesses
  block until they exit and report once, and a watchdog that kills on that
  invents the failure it was installed to catch; a step that reported
  repeatedly and then STOPPED is evidence. Same discipline as a physis
  coherence of 0.0 meaning "no reading" rather than "bad". So `stall_after`
  only ever fires on the second kind, and the first is bounded — if at all — by
  a separate, blunter `max_runtime`, the only instrument that sees a mute
  harness. Everything is off by default (`swarm_heartbeat`), because this is
  the one policy here that ENDS work rather than declining to start it. A
  reaped step is FAILED rather than cancelled: nobody decided to stop it, an
  unclassifiable failure is one the retry policy runs again, and `fail()`
  settles the ledger against the run that really happened where `cancel()`
  would release it. Reaping happens at the top of `pump()`, since a wedged step
  holds a slot and VRAM that only become capacity again once it is let go. Two
  clock bugs fell out of the tests: `started` was stamped from the
  orchestrator's `time.time()` while liveness compared it against the runner's
  injected clock, and `heard` used a strict `>` that made a harness reporting
  the instant it starts indistinguishable from one that never reported. On
  screen: running rows carry `4m` / `10m, quiet 6m`, composed once and printed
  verbatim by both surfaces.

- **A step can state a value, not just describe it** (`swarmfacts.py`) — the
  second half of the handoff problem. `swarmio` fixed where the work LANDED;
  this fixes the small number of VALUES a run turns on — the base URL the scout
  found, the migration id, the port the server came up on. They arrived buried
  in a page of reasoning, and three things went wrong with them: the upstream
  budget clips prose by character count, so the one line that mattered was as
  likely to be past the cut as anything else; the next agent re-read the prose
  and re-decided what the value was, differently; and nothing else could see
  more than "produced 4kB of output". A step may now write `FACT key=value` on
  its own line, and those are carried WHOLE into every downstream prompt in
  their own block, exempt from the prose budget — the sentence explaining a
  value is compressible, the value is not. Keys stay qualified by the step that
  stated them (`scout.api_base`), because two upstream steps naming a thing
  `path` is the normal case and picking a winner silently is how a swarm
  produces a confidently wrong answer. Line-oriented rather than a JSON block:
  asking a CLI harness for JSON works most of the time, and "most of the time"
  over a hundred steps is a parser that fails for reasons nobody can reproduce.
  A step is only ASKED for facts when something depends on it. Also fixed here:
  the prompt was built in two places — admission priced one string and the
  runner sent another, so a budget could govern a prompt that was never sent.
  `swarm facts` in the cockpit, a Values block in the HUD.

- **The swarm remembers what happened, not only what is** (`swarmlog.py`) —
  everything durable about a run was a SNAPSHOT: `swarm.json` holds the DAG as
  it stands and each write replaces the last, so the file answers "what is the
  state" and can answer none of the questions that come up afterwards — how
  long the scrape took, whether the writer retried, which step added the three
  nobody remembers planning. State forgets. Every transition now appends one
  line to `~/.aion/instances/<id>/swarm-events.jsonl` (`started finished failed
  retry gave_up expanded cancelled`), same shape and same reasons as
  `hitl.AuditLog`: appending cannot destroy what came before, a line torn by a
  crash costs exactly that line, and the file is evidence rather than a control
  channel — nothing reads it back into a swarm. `gave_up` is kept distinct from
  `failed` because a dead-lettered step and one that never had a retry budget
  are different stories about the same red mark. `timeline()` is the pure half:
  it folds the log into one row per step, measuring duration from the FIRST
  start to the last terminal event, so a step that failed twice before working
  reports what it actually cost rather than the cost of the attempt that
  happened to succeed. A step still running gets no duration invented for it —
  filling in "now" makes a stalled step look like a slow one. `swarm log`
  prints it; `status()` carries it; a broken log prints and never stops a run.

- **Steps say what they write, so races stop being invisible** (`swarmio.py`) —
  everything a step passed downstream went through one channel: its stdout,
  spliced into the next prompt. Fine for "here is what I found", useless for
  what agents actually do, which is write files. Two steps in the same wave
  editing `docs/api.md` is not a merge conflict — no merge, no branch, no lock,
  just one of them silently losing. A step may now declare `>> path`, and that
  declaration answers two questions: `conflicts()` finds writers of one path
  with NO ordering between them (an ordered pair is a sequence and normal —
  finding the unordered ones needs reachability, not a set intersection), and
  the runner holds a step whose paths are being written by something already
  running OR admitted earlier in the same tick. That second half is the one a
  test caught: checking only what is already WORKING lets two writers start
  together on the very first pump, which is exactly when a fresh DAG races.
  Downstream prompts also name the files upstream wrote — an agent told to
  "polish the draft" otherwise guesses the filename, and its usual guess is to
  write a second draft beside the first.

- **"Who approved what" is now durable** (`hitl.AuditLog`) — a gate decision
  lived in a task's in-memory log, the gate itself was dropped by
  `clear_resolved()`, and `gates.json` only ever holds what is still PENDING —
  so the moment a gate was answered, the answer stopped existing anywhere. For
  a fleet that runs privileged actions on other machines, that was the one
  record worth keeping. Now every decision appends to
  `~/.aion/instances/<id>/approvals.jsonl` with WHO made it: `cockpit` (a
  keypress here), `remote` (the HUD over the authenticated transport), `policy`
  (nobody was asked — the most important kind to have on record) or `timeout`
  (fail-closed). Append-only JSONL because a log that gets rewritten is not
  evidence of anything, and a torn line from a crash costs exactly that line.
  Recorded BEFORE the waiting task is released: a crash in between would
  otherwise leave an action performed with nothing saying who allowed it. A
  broken sink never deadlocks a gate — a cockpit stuck on a full disk is worse
  than a gap in a log, and the gap is printed. Like `gates.json`, the file is
  evidence and never a control channel: nothing reads it back into a book.

- **A DAG can grow from its own results** (`swarmreplan.py`) — the shape used to
  be decided once, before any of it ran, so every step's goal had to be written
  by someone who did not yet know what step one would find. A research step that
  discovers three subsystems worth auditing had no way to say so. Now a finished
  step's output can propose follow-up work, and the bounds are the feature: off
  unless configured (`swarm_replan`); a width cap per step AND a ceiling on the
  whole DAG, so ten steps each allowed three cannot cooperate past it; a
  checkpointed `generation` that bounds the RECURSION, which is what actually
  runs up a bill overnight; every new step forced to depend on the parent whose
  output proposed it, so nothing is schedulable before its own justification;
  and every refusal logged on that parent, because a swarm that silently
  declines to grow looks exactly like one whose planner said nothing. The
  proposal is a model call, so the runner only QUEUES it — the cockpit drains
  the queue, asks off-thread, and hands the answer to `apply_expansion`. The
  queue is deliberately not checkpointed: a restart comes back with the DAG a
  human approved, rather than resuming the growth of one nobody is watching.

- **`swarm plan <goal>` builds the DAG for you, in the terminal** — the
  planner (`swarmplan.py`) validated a model-proposed DAG, capped its steps,
  refused cycles and unresolvable dependencies… and only the browser could
  reach it. In the cockpit, building a DAG meant one `swarm add <name> <goal>
  << deps` line per step, in an order `add_checked` accepts — so the surface
  people actually use had the worst way to do the thing this program is for.
  `swarm plan` now proposes, `swarm apply` creates, `swarm run` starts: three
  decisions, because every step is a prompt a harness will execute. The
  proposal is HELD, drawn in the same wave view the live swarm uses (bars
  dropped — nothing has run, so they would be a column of identical noise),
  and `apply` re-validates exactly the steps that were shown rather than
  re-planning, since a non-deterministic re-roll would make the review
  decorative. The call runs on a worker thread: a 30s model call on the event
  loop is a frozen cockpit — no keystrokes, no task updates, no heartbeat.

- **The DAG is drawn in running order, and says why it stopped**
  (`swarmview.py`) — the old view was a flat list with `← deps` glued on the
  end, which answers "what steps exist" (a question nobody asks) and cannot
  answer either question a running DAG actually gets asked: what runs next, and
  who is holding up whom. `waves()` layers the steps topologically, so reading
  order IS running order; a cycle is drawn as its own labelled wave rather than
  silently dropped, and a dependency naming no step is called out on the row
  instead of looking like patience. `explain()` turns the state into the one
  sentence the operator needs — "2 ready — `swarm run` to start", "blocked: b
  needs a, which failed", "a retries in 30s (attempt 2)" — because "nothing is
  running" has six causes and six different next moves. The same sentence is
  computed server-side for the web HUD, so the two views cannot phrase the same
  swarm differently, and each HUD node carries its `wave` so a force graph has
  a reading order at all. Also fixed here: `swarm status` stored the legacy
  TEXT dashboard while every consumer read the dict the bus publishes, so the
  one command whose whole job is "show me the swarm" left the panel raising
  `AttributeError` on `.get`.

- **A failed step is no longer the end of the DAG** (`swarmpolicy.py`) — one
  answer to a failure, and it was "stop": the agent went FAILED, `dep_state`
  blocked every dependent, and an unattended swarm sat dead until morning,
  including when the cause was a tunnel that dropped for four seconds. Now the
  failure is classified (transient / permanent / unknown, off the message,
  because CLIs report an exit code and not a taxonomy), retried with
  exponential backoff up to `max_attempts`, and dead-lettered with the reason
  retrying stopped and the list of steps stuck behind it. A retrying step goes
  back to IDLE rather than FAILED — FAILED blocks dependents, and a step we
  intend to run again in ten seconds has blocked nothing yet. Three details
  that would each have made it a liability: `attempts` is checkpointed (a
  restart that reset it turns a bounded policy into an unbounded one), the
  ledger banks the previous attempt so retries are charged rather than free,
  and the cockpit heartbeat calls `due_for_retry()` — a backoff is the one
  thing that becomes startable with no event to announce it. Default is
  `max_attempts=1`, i.e. exactly the old behaviour: an upgrade must not start
  re-running, and re-paying for, work nobody asked to be re-run. Configure with
  `"swarm_retry": 3` or the full dict.

- **A restart adopts remote work instead of re-running it** — `restore()` was
  called nowhere, so the DAG survived on disk and in the web HUD while the
  cockpit came back believing there was no swarm. And once restored, every
  WORKING agent was reset to IDLE — right for a local step whose coroutine
  died, exactly wrong for one pinned to a peer that never noticed we left,
  where it means a second copy of the same job. The task id an agent owns is
  now checkpointed, and `rehydrate()` re-attaches the watch.

- **One state machine for agents and tasks** — a swarm agent is not a process;
  it owns a task id, and the task is what a harness can suspend. So `pause` and
  `resume` have no agent-level implementation at all: `agentctl.route` decides
  them against the real task state and the store applies them through
  `control_task`, the same call a keypress in the cockpit makes. `swarm.can`
  stopped carrying its own copy of the terminal-state and rerun rules, the two
  vocabularies meet in `AGENT_AS_TASK` (a status added without a task meaning
  fails the suite), and a step pinned to another instance is paused over the
  same authenticated transport that polls it. The typed `swarm run` / `add` /
  `stop` now go through `swarm_command` too — `run` used to flip statuses to
  WORKING and spawn nothing, so a DAG typed into the cockpit sat at layer one
  forever while the identical DAG driven from the HUD ran.

- **Swarm planning** (`swarmplan.py`) — describe a goal, get a reviewed DAG.
  `decompose()` created an empty plan nobody read, so every swarm had to be
  hand-built. A model proposes; almost everything it says is refused: step
  count capped, cycles rejected at creation (not discovered later by
  `stalled()`), invented dependencies refused, hallucinated harnesses dropped
  to the default, names charset-checked. Propose → review the whole DAG →
  create → run are four separate decisions, and the reviewed steps are what
  get applied — re-planning on commit would create a DAG nobody read.

- **Swarm executor** (`swarmrun.py`) — the DAG now RUNS. Nothing previously set
  a swarm agent to DONE, so `run_ready()` moved layer one to WORKING and layer
  two waited forever (`run_all()` existed only in a docstring). An agent's goal
  is now spawned as a real task through the cockpit's gated path — inheriting
  HITL, physis classification, checkpointing and cancellation — and its
  terminal state advances the agent, which pumps the scheduler again. Pure
  `admit()` enforces max-parallel and a VRAM budget (naming any agent too big
  to ever fit, rather than starving it silently); pure `prompt_for()` feeds
  upstream output into downstream prompts, so a dependency means "take input
  from", not just "wait for". `stalled()` explains a stopped DAG, cycles
  included.

- **Swarm control from the web HUD** (`SwarmOrchestrator.control/run_ready/
  stop_all/add_checked` + `/swarm` on the transport + `/api/swarm`) — start,
  cancel, retry and remove one agent; add an agent with dependencies; run every
  ready agent or stop everything. Refusals name the blocking dependency rather
  than saying "blocked". Removal is refused while anything still depends on the
  agent (deps are by NAME, so deleting the node silently makes them
  unsatisfiable). Fixed `agents_ready()` counting a FAILED or CANCELLED
  dependency as satisfied — it and `blocked_agents()` disagreed about the same
  agent, and `swarm run` would start a step whose input never arrived.

- **Settings, expanded** (`settings.py` + `/api/settings{,/harness}`) — 8
  sections, 29 fields, declared once as a schema and rendered generically, so a
  new option is one Python entry and zero JS. Fleet thresholds, persona, web
  voice, graph detail, deck, ring, notifications, paths; plus a per-harness
  table (enable, tier, VRAM, and `requires_approval` — inserting a human into
  the loop is now one click). Validation is server-side and total: unknown keys
  dropped, out-of-range rejected with a reason rather than clamped, read-only
  paths refused, secrets redacted on read and never echoed back. Fixed
  `write_json_atomic` escaping non-ASCII, which turned every workspace icon
  into `\uXXXX` on any save.

- **Agent control from the web HUD** (`agentctl.py` + `/api/agents/{control,spawn}`
  + `/task` on the transport) — spawn a task on a harness, pause/resume/cancel/
  rerun a running one, from the browser. The HUD never runs a harness: it asks a
  live cockpit over the authenticated transport and that cockpit applies its
  HITL gates. Legality lives in ONE place (`agentctl.legal`), shared by the TUI
  keybindings and the web, so they cannot drift. Fixed three bugs on that path:
  `on_run` dropped the harness argument, `on_cancel` passed an id where a Task
  was required (every remote cancel 500'd), and `_respawn` bypassed the gate so
  an approved destructive prompt could be re-run with no approval.

- **Level of detail in the graph** — the renderer draws a fixed number of
  nodes per unit of screen area and defers the rest, so zooming in reveals new
  elements as room appears rather than crossing a tuned threshold. Hubs, the
  selection and search hits are exempt; the list view and search stay complete;
  the deferred count is on screen and in the screen-reader description.

## Roadmap — next

### Needs a human

Nothing blocking. Both items that were here are closed:

0. ~~Revoke the flagged Gemini keys~~ — **owner decided not to act.** 16 of 29
   stored keys return 403 "reported as leaked". Recorded, not open; do not
   re-raise it.
0b. ~~`gh auth refresh -s repo,workflow`~~ — **done, and it found something.**
   CI had never been green: 25 of 25 runs red. The `tests` job passes (the
   lint gate included, now verified rather than assumed); the `guard` job had
   no install step and ended every single run in "No module named pytest". So
   the staged-content rules — the ones that stop a blind `git add -A` from
   sweeping credentials into a commit — had never once executed in CI, and the
   red X had been there long enough to read as background. Note for the
   future: `GITHUB_TOKEN` in the environment carries `copilot` scope only and
   overrides the keyring token, so Actions queries need `env -u GITHUB_TOKEN`.

### Next feature (named, not started)

- **Todo tab ↔ praxis.** Wire the Desktop todo list to the praxis backend so
  the two are one list rather than two that disagree.
- **Any AI as an axiom provider.** Today `axiom` means one provider; the goal
  is the harness treatment — whichever model is configured answers, and the
  caller does not know which.

### Software

1. **Split `store.py`** — first cut done (2093 → 1534; the swarm half is now
   `storeswarm.py`). Next candidates, same method: the `setup`/`profile`/env
   block, then memory/todo/vault. The seam test (`tests/test_store_split.py`)
   carries a line-count ratchet, so each extraction has to hold.
2. **mypy, per-module.** Its own cycle with an opt-in list, starting from the
   pure modules that already have full signatures (`swarm*.py`, `hitl.py`).
   Switched on repo-wide it produces thousands of findings and a gate everyone
   disables — which is worse than no gate, because it looks like one.
3. **Coverage floor** on `core.py` / `store.py` / `harnesses.py`. Worth having
   *after* (1): a percentage on a 2100-line module reports something nobody can
   act on.

### Hardware / field (all blocked on physical access)

4. **Colmi accel offsets** — run the verify harness against the real ring, pin
   `decode_accel()`, tune `TapDetector.threshold`. *(blocks the ring "button" in field)*
5. **Real APK** — pair phone Wireless Debugging, `tailscale serve` HTTPS the PWA,
   Bubblewrap build (`twa-manifest.json`), `adb install`. *(blocked on phone pairing code)*
6. **Pendant → physis ingest** — the transport is **done**: HTTP pull against the
   endpoints the cyclops firmware already serves (`/snap`, `/audio.wav`,
   `/stream`), in `deck/pendant.py`, LAN-only, stdlib. BLE was never viable for
   VGA JPEGs at the ~2 KB/s notify throughput bring-up measured. What is left is
   feeding `PendantEvent` blobs into the physis context (vision → sightings,
   audio → transcriber) and registering the link in `ui/app.py` next to
   `DeckInput`/`RingInput`. Point it with `AION_PENDANT_HOST`.
7. **assetlinks.json route** in `aion_web.py` for TWA trust (needed by the APK).

### Brain

8. ~~**HUD coherence stream**~~ — done. `Iteration.coherence` reaches the graph
   and animates the edge feeding each task.
9. **Research priming** — use physis `reconstruct()` to skip queries a past run covered.

### A note on where the effort has gone

Cycles 9–12 added five modules to the swarm subsystem in a row (event log,
stated values, liveness, plus the lint gate that fell out of it). That
subsystem is now the most developed part of aion by a wide margin, and the
gaps chosen were the ones already to hand rather than the ones ranked against
everything else. The list above is ordered by what is actually blocking, not by
what is nearest — the hardware items have been "next" for longer than any of
the swarm work existed.

## Verification

```bash
cd /home/gio/aion
.venv/bin/python -m pytest tests/ --ignore=tests/test_term.py -q
TERM=xterm-256color timeout 6 .venv/bin/python -m aion.ui.app        # TUI smoke
AION_WEB_HOST=0.0.0.0 .venv/bin/python scripts/aion_web.py           # web HUD on LAN
```

## Reference docs

- `docs/ssh-peers.md` — reaching aion on another machine, and the key restrictions.
- `docs/aion-cyclops-reconcile.md` — spec-vs-repo map (what the PRS got wrong).
- `docs/physis-integration.md` — how the brain plugs into the loops.
- `docs/install-mobile.md` — PWA/APK install path + the HTTPS caveat.
