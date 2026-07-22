# physis_pro integration — how the coherence brain plugs into aion's loops

`physis.py` is a thin, dependency-free (`urllib`) client for Gio's Rust
`physis_pro` engine (`http://127.0.0.1:19876`, env `PHYSIS_URL`/`PHYSIS_PORT`).
It **soft-fails everywhere**: if physis is down, callers get neutral results and
a log line, never an exception that stalls the cockpit.

## What physis offers aion

| Endpoint / client call        | Meaning for aion |
|-------------------------------|------------------|
| `classify(text)` → cells      | What **domain of work** a task/output is (semiotic grid, e.g. `CONSTRUCT/REST`) + a match score. |
| `register(node, score, edge)` | Persist a **coherence fact**: +1 flowing / 0 idle / −1 blocked, optionally edged to a domain. |
| `ingest(node, edges)`         | Add a node + edges to the **shared holarchy vector graph** (knowledge that survives across runs). |
| `reconstruct()`               | Browse the accumulated wiki by content. |

## Where it was already wired

- `store.py` (`_spawn`): on task creation, `classify(prompt)` tags the task with
  a `domain`, publishes it on the `physis` bus topic (Tasks workspace shows it),
  and `register`s the new task at coherence +1.
- `harnesses.PhysisHarness`: background poller of embedder health for the
  `physis` workspace HUD.

## What this change adds (loop-level integration)

The two long-running loops now **close the coherence feedback loop** — they
don't just get classified at birth, they report their *outcome* back to the brain.

### Factory loop (`factory.py` + `FactoryHarness`)
- **Stall detection is pure and physis-independent.** `output_novelty()` (difflib
  trailing-window diff) + `detect_stall()` catch a spinning Ralph loop — one whose
  output stopped changing — and stop it with `STOP_STALLED` instead of burning the
  whole budget. This never depends on physis being up. Off by default in the
  engine (`stall_window=0`); the harness defaults it on (`stall_window=3`).
- **physis scores each round (opt-in).** With `extra.coherence=true`, each
  iteration's output is scored via `physis.score_text()` (top-cell score → [−1,1])
  and stored on `Iteration.coherence`. Telemetry for the HUD/brain — it **never
  gates the loop** (an unverified remote must not be able to halt work).
- **Outcome recorded.** When the loop ends, `record_outcome(task, ±1/0, domain)`
  tells physis whether that task-domain **flowed** (DONE → +1) or **blocked**
  (ERROR/STALLED → −1). Over time physis's dream loop can see which kinds of work
  keep stalling.

### Research loop (`ResearchHarness`)
- On a completed run, `_ingest_research()` pushes the run into the holarchy:
  `ingest("research:<id>", [source urls])` — so discovered knowledge accumulates
  into the shared vector graph and is `reconstruct()`-able later — plus
  `record_outcome(+1 if covered else 0)`.

## Design rules honoured
- **Soft-fail:** every physis call is wrapped; the loop runs identically with
  physis down.
- **Thread-safety:** `score_text` runs inside `run_factory`'s `to_thread` worker
  (blocking urllib, no registry/asyncio access — allowed). Outcome/ingest calls
  run via `asyncio.to_thread` off the event loop so blocking HTTP never stalls
  the UI.
- **Brain scores, engine decides:** physis is telemetry + memory. The only hard
  control signal added (stall) is pure and local.

## Next
- Feed `Iteration.coherence` into the DAG edge animation (spec's "kinetic token
  streams") on the HUD.
- Use `reconstruct()` to prime a new research run with what physis already knows
  (skip queries already covered by a past run).
- Pendant vision/audio (`deck/pendant.py`) → `ingest` scene/voice context.
