# Model serving — fleet config

## LFM (Liquid.ai) family

| id | source | node | status |
|---|---|---|---|
| `lfm2.5-2.6b` | `LiquidAI/LFM2.5-2.6B-GGUF` Q4_0 | pansa (ollama 127.0.0.1:11434) | available |
| `lfm2-350m-math` | `LiquidAI/LFM2-350M-Math-GGUF` | pansa | provisioning |
| `lfm2.5-vl-3b` | `LiquidAI/LFM2.5-VL-3B` (PyTorch→gguf convert) | pansa | requires conversion |
| `lfm2-1.3b-math` | `LiquidAI/LFM2-1.3B-Math` (HF-gated) | pansa | requires_hf_auth |

Provisioning: download into `/home/gio/models/liquidai/<id>/`, then
`ollama create <id>:latest -f Modelfile` (FROM .gguf path, num_ctx 131072).
Inventory lives in `~/dev/randomesh/fleet-models.json` (source of truth read by
`scripts/fleet/model_router.py` on omo :8090).

## FrankenLLM routing (`~/dev/frankenllm`)

`scripts/frankenllm_server.py` `route()`:
1. L0 — deterministic knowledge lookup (`_KNOWLEDGE`)
2. L1 — Physis retrieval (`/api/v1/search/nodes` on `:19876`) + small model
3. L4 — fast local (gemma4:e2b, LFM2.5)
4. L5 — oracle local (gemma4-e2b-heretic on omo llama-server :8081)
5. L6 — **cloud free-tier fallback chain** (auto-fallback, first non-empty):
   `gemini-2.5-flash → nvidia/nemotron-70b → groq/llama-3.1-8b-instant → opencode-zen
   → llm7/GLM-5.3-Flash → cerebras → mistral → together → sambanova → deepinfra → openrouter`
   Keys from `~/.env` (loaded at boot); tried per-key (multi-value support for
   duplicate env lines). Cloud only engages when local L5 oracle returns empty
   or on the explicit `cloud:auto` model.

Endpoints: `POST /v1/chat/completions`, `POST /v1/embeddings`,
`GET /v1/models`, `GET /health`. Binds `127.0.0.1:8085` only.

## Per-user workspace containers

`scripts/workspace.sh` — docker (`franken-ws:latest`), per-user
`franken-ws-<uid>` (zsh + playwright + chromium, 1 GB RAM, pids-limited,
`--cap-drop ALL`, read-only root, home volume — no privilege, no escape).
- `exec <uid> <cmd>` → routed via **hermes** (`hermes -z`) so command history
  accumulates in `~/.hermes/projects/<uid>`.
- `upload <uid> <name>` → stream into container home.
- `browser <uid> <url>` → playwright screenshot in-container.
- `files <uid>` → list container volume.

Exposed through Praxis as `/api/ai/workspace/*` (auth: Supabase JWT or PAT).
