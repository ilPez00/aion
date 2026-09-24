# Model serving — fleet config

Fleet-wide model serving (inventory, routing, provisioning) lives in the
randomesh repo, which aion consumes rather than duplicates:

- Inventory + truth: `~/dev/randomesh/fleet-models.json`
- Routing: `~/dev/randomesh/scripts/fleet/model_router.py` (omo :8090)
- Docs: `~/dev/randomesh/docs/MODEL_MESH.md`
- Unification plan (aion ⇄ randomesh): `~/dev/randomesh/docs/UNIFICATION_PLAN.md`

Local-only notes that have not moved yet:

- FrankenLLM router: `~/dev/frankenllm/scripts/frankenllm_server.py`
  (L0 knowledge → L1 physis → L4 fast → L5 heretic :8088 → L6 cloud chain;
  binds `127.0.0.1:8085`, consumed by Praxis `/api/ai/*` and
  `AICoachingService.runWithFallback`).
- Per-user workspaces: `~/dev/frankenllm/scripts/workspace.sh` (docker
  `franken-ws-<uid>`, hermes-routed exec).
