"""What a model is told it may say, versus what is accepted.

Two prompts in this repo hand a model a vocabulary: `interpret.build_prompt`
(plain language into a palette command) and `voicecmd._LLM_PROMPT` (a spoken
phrase into a HUD action). Each is a contract with a validator somewhere else,
and neither had anything checking the two agreed.

`interpret`'s did not. Its `goto` list named `memory`, `sys`, `hermes`,
`skills`, `projects` and `swarm` — none of them workspaces — and omitted
`runs` and `net`, which are. A model obeying that prompt exactly produced
"goto: no workspace 'sys'", and neither half could see the problem: the prompt
was self-consistent, the dispatcher was self-consistent, and the disagreement
lived between them.

The fix is derivation, not correction — a hardcoded vocabulary is only right
on the day it is typed. These tests hold that: they compare the rendered
prompt against the live source of truth, so a workspace added tomorrow
appears in the prompt or fails here.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aion.interpret import APP_SYNONYMS, build_prompt, workspace_ids  # noqa: E402
from aion.voicecmd import ALLOWED_ARGS, MODULE_WORDS  # noqa: E402
from aion.voicecmd import _LLM_PROMPT as VOICE_PROMPT  # noqa: E402


def _between(text: str, prefix: str) -> set[str]:
    """The `a|b|c` alternation following a marker in a prompt."""
    m = re.search(re.escape(prefix) + r"([a-z_|]+)", text)
    return set(m.group(1).split("|")) if m else set()


# ── the palette translator ──────────────────────────────────────────────────

def test_every_workspace_the_model_is_offered_exists():
    """The bug. Six of these named nothing, so the model was instructed to
    produce commands the dispatcher answers with an error."""
    offered = _between(build_prompt(), "goto <")
    assert offered == set(workspace_ids()), (
        f"offered but unreachable: {offered - set(workspace_ids())}; "
        f"reachable but not offered: {set(workspace_ids()) - offered}")


def test_a_workspace_added_to_config_reaches_the_prompt(monkeypatch):
    """Derivation is the whole fix. If this can pass with a stale list, the
    next rename puts it straight back where it was."""
    import aion.interpret as interp

    monkeypatch.setattr(interp, "workspace_ids", lambda: ["alpha", "beta"])
    assert _between(interp.build_prompt(), "goto <") == {"alpha", "beta"}


def test_every_app_the_model_is_offered_is_launchable():
    assert _between(build_prompt(), "app <") == set(APP_SYNONYMS)


def test_the_workspace_list_is_never_empty():
    """An empty alternation reads as an invitation to invent one, which is
    the failure being removed rather than a smaller version of it."""
    assert workspace_ids()
    assert _between(build_prompt(), "goto <")


def test_a_broken_config_falls_back_rather_than_offering_nothing(monkeypatch):
    import aion.core as core

    def boom(*a, **k):
        raise OSError("no config")
    monkeypatch.setattr(core, "load_config", boom)
    assert workspace_ids() == ["models", "tasks", "agent"]


def test_every_verb_the_prompt_documents_survives_the_allowlist():
    """`llm_translate` drops any reply whose first word is not in its
    allowlist. A verb documented in the prompt but missing from that tuple is
    one the model is asked for and then thrown away — the same shape as the
    goto bug, one layer down."""
    src = (ROOT / "src" / "aion" / "interpret.py").read_text(encoding="utf-8")
    allowed = set(re.findall(
        r'"([a-z]+)"', src.split("first in (")[1].split(")")[0]))
    # Verbs are the first token of each documented command line.
    documented = {line.strip().split()[0]
                  for line in build_prompt().splitlines()
                  if line.startswith("  ") and line.strip()
                  and line.strip()[0].islower()}
    assert documented <= allowed, f"documented but discarded: {documented - allowed}"


# ── the voice translator ────────────────────────────────────────────────────

def test_every_voice_module_offered_is_one_the_validator_accepts():
    """`_validate` drops any module not in MODULE_WORDS, so a name here that
    is not there is one the model emits and the cockpit silently discards."""
    offered = _between(VOICE_PROMPT, '"module":"')
    assert offered == set(MODULE_WORDS)


def test_the_voice_prompt_only_documents_real_actions():
    documented = set(re.findall(r'"action":"([a-z]+)"', VOICE_PROMPT))
    assert documented <= set(ALLOWED_ARGS), documented - set(ALLOWED_ARGS)


def test_voice_still_cannot_be_told_to_approve_anything():
    """A standing rule, and worth a test that fails loudly if a prompt edit
    ever softens it: voice may DENY a gate and never grant one."""
    assert '"decision":"reject"' in VOICE_PROMPT
    assert '"decision":"approve"' not in VOICE_PROMPT
    assert "Never emit an approval" in VOICE_PROMPT
