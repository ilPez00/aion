# Plan: aion ↔ randomesh integration

Date: 2026-09-01 — based on `aion/main` + `~/dev/randomesh` (CONFIG.md / fleet.json / bin/mesh).

## Status: what's already wired (verified 08-31)
- `meshsrv.py` probes `SERVICES` (hardcoded dict) over SSH; HUD renders per-service
  UP/down. Services: physis:8090, praxis:8070, mesh-lm-orch:8088 (Caddy LB),
  omo-llm/pansa-llm/air-llm:8081, colibri:11435.
- `meshmon.py` already reads `~/dev/randomesh/fleet.json` as the node/alias/ROLE map
  (with built-in fallback). So node discovery is NOT drifted — aion uses randomesh's.
- `agg.py` (new, committed) collects sessions/memory/docs/models across nodes via
  those aliases; `aion mesh agg {collect,status,sessions,memory,docs,search}` works.
- `bin/mesh` dispatches `status|update|serve|…` and calls aion meshsrv for `status`.
  No `agg` subcommand in `bin/mesh` yet.

## The ONE drift gap (the real one)
`meshsrv.SERVICES` is hardcoded in aion and does NOT track randomesh's
`SERVING_ORCHESTRATION` section in CONFIG.md → fleet.json `serving` block.
If randomesh adds a serving node or changes a port, aion's HUD goes stale.
`fleet/export-config.sh` already emits `serving.nodes` (name→ip/port/gpu/tps);
meshsrv should consume it.

## Integration steps

### (1) meshsrv: derive SERVICES from fleet.json serving section
- Add `_load_serving_services()` reading `fleet.json` → `serving.nodes`.
  For each node: host alias = the node's `tailscale` (from NODES section),
  probe = tcp(8081), start/stop = the canonical `serve-start.sh` /
  `pkill -f llama-server.*<port>` (reuse existing cmds as defaults).
- Always-on base services (physis:8090, praxis:8070, mesh-lm-orch:8088,
  colibri:11435) remain hardcoded — they're aion/HUD-specific, not randomesh.
- Merge: base + per-node llama-server. If fleet.json absent, use built-ins.
- This makes `bin/mesh status` + the HUD reflect randomesh's *desired* serving
  set, not a stale snapshot.

### (2) bin/mesh: expose `agg` subcommand
- Add `agg)` case → `cd ~/dev/aion && exec .venv/bin/python -m aion.ui.app mesh agg "$@"`
  so randomesh operators can run `mesh agg collect` / `mesh agg search …`
  from the same dispatcher, without knowing aion's venv path.

### (3) auto-updater hook → refresh agg.db
- After `scripts/auto_updater.sh` runs a ff-only pull that advances HEAD
  (i.e. randomesh CONFIG.md changed), the collector script
  `scripts/aion-mesh-agg-collect.service` (already shipped) re-runs
  `aion mesh agg collect` on its 15-min timer anyway — so agg.db self-heals.
  Optionally: a `post-pull` hook in auto_updater.sh calls aion collect now,
  to keep HUD hot. Decision: rely on the 15-min timer for now; add explicit
  refresh only if HUD staleness is observed (cheaper than racing the timer
  from every pull on every node).

### (4) cross-repo test seam (no cross-repo code)
- Add a test in `tests/test_meshsrv.py` asserting SERVICES includes every
  fleet.json serving node (with fake fleet.json pointing at a tmp file).
- Add `tests/test_agg.py` case: `collect_all` nodes list is sourced from
  meshmon NODES (already is) — confirm.

## Non-goals (out of scope this cycle)
- Compute mesh (ggml-RPC): still disabled per CONFIG.md (`enabled: false`);
  split-compute blocked on llama.cpp build 8831. Aggregation does not fuse.
- FreeToken / colibri GPU rebuild: colibri stays CPU-only on omo;
  omo's RX 6650M Vulkan serves llama.cpp directly. No code change needed.
- physis-pro source vs runtime bundle propagation: that's physis-sync cron
  (`scripts/fleet/physis-sync.sh`), not mesh. Leave as-is.

## Verification
- `AION_FLEET_CONFIG=tmp.fleet.json python -m pytest tests/test_meshsrv.py`
- `mesh agg status` from randomesh checkout → uses aion venv transparently.
- Live: edit a fake CONFIG.md serving node, `fleet/export-config.sh`, reload
  aion meshsrv snapshot, assert the new node shows in HUD probe list.

## Rollout order
1. (1) meshsrv SERVICES-from-fleet.json + test — PR unit
2. (2) bin/mesh agg shim — same PR
3. (3) timer reference (already shipped) — docs only
Ship as one conventional commit.
