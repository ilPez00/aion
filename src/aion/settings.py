"""settings.py — everything configurable, described once.

The web Settings module used to be three read-only lists: which providers have
a key, which skills are installed, where files live. Everything you could
actually *change* lived in `config/layout.json` and was edited by hand.

This is the schema for the editable surface. It exists as data rather than as a
form because the alternative — a hand-written control per setting in hud.js,
plus a hand-written validator per setting in aion_web.py — is four places to
edit for one new option and four places for them to disagree. Here a field is
one entry; the HUD renders whatever it is told about and the validator is
generic.

Design rules, each of which is load-bearing:

  * **Validation is server-side and total.** The browser is untrusted input,
    the same as the LLM in voicecmd. Unknown keys are dropped, not merged; a
    value outside its declared range is rejected with a reason rather than
    clamped silently into something the user did not ask for.
  * **Secrets are never sent to the browser.** Provider keys report presence
    only. `write()` will accept a secret, but `read()` will never echo one
    back — a screenshot of a settings page must not leak a token.
  * **Restart-required is declared, not discovered.** Some values are read once
    at boot (the listening socket, the state root). Saying so next to the field
    beats a user wondering why nothing changed.
  * **Rejections are itemised.** A save that silently drops two of five fields
    is worse than one that fails, so `write()` returns exactly what it applied
    and exactly what it refused.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

# ── Field types ──────────────────────────────────────────────────────────────
BOOL, INT, FLOAT, STR, CHOICE, PATH, SECRET, COLOR = (
    "bool", "int", "float", "str", "choice", "path", "secret", "color")

REDACTED = "••••••••"


@dataclass
class Field:
    key: str
    label: str
    type: str = STR
    default: Any = ""
    choices: list[str] = field(default_factory=list)
    min: float | None = None
    max: float | None = None
    help: str = ""
    restart: bool = False          # only takes effect on the next boot
    readonly: bool = False         # shown, but set elsewhere (env, CLI)
    env: str = ""                  # environment variable that overrides it

    def as_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "type": self.type,
                "default": self.default, "choices": self.choices,
                "min": self.min, "max": self.max, "help": self.help,
                "restart": self.restart, "readonly": self.readonly,
                "env": self.env}


@dataclass
class Section:
    id: str
    label: str
    help: str = ""
    fields: list[Field] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"id": self.id, "label": self.label, "help": self.help,
                "fields": [f.as_dict() for f in self.fields]}


# ── The schema ───────────────────────────────────────────────────────────────
SECTIONS: list[Section] = [
    Section("fleet", "Fleet", (
        "How this instance identifies itself and how quickly a peer is called "
        "stale. Binding to the LAN exposes an endpoint that runs prompts, so "
        "it is off unless you say otherwise."), [
        Field("instance", "Instance name", STR, "main", restart=True,
              env="AION_INSTANCE",
              help="Names this cockpit's state directory under ~/.aion/instances."),
        Field("listen", "Listen on", CHOICE, "local",
              choices=["local", "lan"], restart=True, env="AION_LISTEN",
              help="'lan' exposes the remote transport to the whole network. "
                   "Prefer an SSH peer (see docs/ssh-peers.md)."),
        Field("heartbeat_s", "Heartbeat", FLOAT, 5.0, min=1, max=60,
              help="Seconds between writes of this instance's meta.json."),
        Field("local_stale_s", "Local stale after", FLOAT, 15.0, min=1, max=600,
              help="A same-machine peer this quiet is shown as lagging."),
        Field("local_offline_s", "Local offline after", FLOAT, 30.0, min=2, max=3600),
        Field("remote_stale_s", "Remote stale after", FLOAT, 20.0, min=1, max=600,
              help="Remote peers are polled over a network, so these are "
                   "deliberately more patient than the local ones."),
        Field("remote_offline_s", "Remote offline after", FLOAT, 60.0, min=2, max=3600),
    ]),

    Section("persona", "Persona", "How the cockpit talks back.", [
        Field("verbosity", "Verbosity", CHOICE, "normal",
              choices=["terse", "normal", "verbose"]),
        Field("formality", "Formality", CHOICE, "casual",
              choices=["casual", "neutral", "formal"]),
        Field("voice_enabled", "Voice", BOOL, True,
              help="Offline voice control in the TUI. The web HUD's voice is "
                   "the browser's own and is toggled with the MIC button."),
    ]),

    Section("voice", "Voice (web)", (
        "The HUD's voice uses the browser's Web Speech API, which is "
        "Chrome-only. Voice may DENY an approval gate but can never grant "
        "one — approval stays a deliberate button press."), [
        Field("speak_replies", "Speak replies", BOOL, True,
              help="Answer out loud after acting. Off means it still listens."),
        Field("continuous", "Keep listening", BOOL, True,
              help="Stay open between utterances instead of one-shot."),
        Field("lang", "Recognition language", STR, "en-US",
              help="BCP-47 tag passed to the recogniser, e.g. en-GB, it-IT."),
        Field("llm_fallback", "Understand any phrasing", BOOL, True,
              help="Send phrases the rules do not match to the model. Its "
                   "reply is schema-checked before anything runs."),
    ]),

    Section("graph", "Graph", (
        "How much of a graph is drawn at once. The budget scales with the "
        "viewport, sub-linearly in its area, and caps out — past a couple of "
        "hundred nodes the picture is a texture regardless of screen size."), [
        Field("detail", "Detail", CHOICE, "normal",
              choices=["sparse", "normal", "dense", "all"],
              help="'all' disables the budget entirely."),
        Field("reduced_motion", "Reduce motion", BOOL, False,
              help="Present the layout already settled, with no breathing or "
                   "pulses. Follows the OS setting when left off."),
        Field("show_finished", "Show finished tasks", BOOL, True,
              help="Keep done/failed tasks in the Agents graph."),
    ]),

    Section("deck", "CyclUno deck", "Arduino control deck over serial.", [
        Field("enabled", "Enabled", BOOL, True),
        Field("port", "Serial port", STR, "", restart=True,
              help="Leave empty to auto-detect /dev/ttyACM* or /dev/ttyUSB*."),
    ]),

    Section("ring", "Colmi R02 ring", "BLE heart-rate and tap input.", [
        Field("enabled", "Enabled", BOOL, True),
        Field("address", "BLE address", STR, "", restart=True,
              help="Leave empty to scan. Needs the 'ring' extra installed."),
    ]),

    Section("notify", "Notifications", (
        "Opt-in webhook for failed, stalled and gate events only. There is no "
        "default endpoint and no telemetry; nothing is sent until you set a "
        "URL here."), [
        Field("url", "Webhook URL", SECRET, "",
              help="Redacted once saved. Task labels are sent to this "
                   "endpoint — point it somewhere you control."),
        Field("on_failed", "On failure", BOOL, True),
        Field("on_stalled", "On stall", BOOL, True),
        Field("on_gate", "On approval gate", BOOL, True),
    ]),

    Section("paths", "Paths", (
        "Set by environment because they decide what the process can reach, "
        "and a LAN-reachable page must not be able to widen its own sandbox."), [
        Field("home", "aion home", PATH, "", readonly=True, env="AION_HOME"),
        Field("vault", "Obsidian vault", PATH, "", readonly=True, env="AION_VAULT"),
        Field("fs_root", "File scan root", PATH, "", readonly=True,
              env="AION_FS_ROOT",
              help="The graph file manager cannot see outside this."),
        Field("web_port", "Web HUD port", PATH, "", readonly=True,
              env="AION_WEB_PORT"),
    ]),
]

SECTION_BY_ID = {s.id: s for s in SECTIONS}
# Sections that live in config/layout.json. `paths` is environment-only and
# `graph` is a browser preference, so neither is written there.
PERSISTED = ("fleet", "persona", "voice", "deck", "ring", "notify")


# ── Validation ───────────────────────────────────────────────────────────────
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def coerce(f: Field, raw: Any) -> tuple[bool, Any, str]:
    """(ok, value, reason). Never raises, never guesses.

    A rejected value is reported rather than clamped: silently turning a
    heartbeat of 0 into 1 gives the user a setting they did not choose and no
    way to tell it happened.
    """
    if f.readonly:
        return False, None, "read-only (set by environment)"

    if f.type == BOOL:
        if isinstance(raw, bool):
            return True, raw, ""
        s = str(raw).strip().lower()
        if s in _TRUE:
            return True, True, ""
        if s in _FALSE:
            return True, False, ""
        return False, None, f"not a boolean: {raw!r}"

    if f.type in (INT, FLOAT):
        try:
            v = int(raw) if f.type == INT else float(raw)
        except (TypeError, ValueError):
            return False, None, f"not a number: {raw!r}"
        if f.min is not None and v < f.min:
            return False, None, f"below the minimum of {f.min}"
        if f.max is not None and v > f.max:
            return False, None, f"above the maximum of {f.max}"
        return True, v, ""

    if f.type == CHOICE:
        s = str(raw).strip()
        if s not in f.choices:
            return False, None, f"not one of {', '.join(f.choices)}"
        return True, s, ""

    if f.type == COLOR:
        s = str(raw).strip()
        if not _HEX.match(s):
            return False, None, "not a #rrggbb colour"
        return True, s, ""

    if f.type == SECRET:
        s = str(raw)
        # The browser is sent REDACTED for secrets, so it will send it back on
        # any save that did not touch the field. Treat that as "leave alone"
        # rather than as a new value of literally "••••••••".
        if s == REDACTED:
            return False, None, "unchanged"
        return True, s.strip(), ""

    s = str(raw).strip()
    if f.type == STR and len(s) > 500:
        return False, None, "too long (max 500 characters)"
    return True, s, ""


@dataclass
class WriteResult:
    section: str
    applied: dict = field(default_factory=dict)
    rejected: dict = field(default_factory=dict)
    restart_needed: list[str] = field(default_factory=list)
    path: str = ""

    @property
    def ok(self) -> bool:
        return not self.rejected

    def as_dict(self) -> dict:
        return {"ok": self.ok, "section": self.section, "applied": self.applied,
                "rejected": self.rejected, "restart_needed": self.restart_needed,
                "path": self.path}


def validate(section_id: str, values: dict) -> WriteResult:
    """Check a section's values without writing anything."""
    out = WriteResult(section=section_id)
    section = SECTION_BY_ID.get(section_id)
    if section is None:
        out.rejected["_section"] = f"no section named {section_id!r}"
        return out
    known = {f.key: f for f in section.fields}
    for key, raw in (values or {}).items():
        f = known.get(key)
        if f is None:
            # Dropped, not merged. An unknown key in a config file written from
            # a browser is either a typo or someone probing.
            out.rejected[key] = "unknown setting"
            continue
        ok, value, why = coerce(f, raw)
        if not ok:
            if why != "unchanged":
                out.rejected[key] = why
            continue
        out.applied[key] = value
        if f.restart:
            out.restart_needed.append(key)
    return out


# ── Reading ──────────────────────────────────────────────────────────────────
def _config() -> dict:
    from .core import load_config
    try:
        return load_config()
    except Exception:  # noqa: BLE001
        return {}


def read(section_id: str | None = None) -> dict:
    """Current values for every section. Secrets come back redacted."""
    cfg = _config()
    out: dict[str, dict] = {}
    for section in SECTIONS:
        if section_id and section.id != section_id:
            continue
        block = cfg.get(section.id, {}) if isinstance(cfg.get(section.id), dict) else {}
        values: dict[str, Any] = {}
        for f in section.fields:
            if f.type == SECRET:
                values[f.key] = REDACTED if block.get(f.key) else ""
                continue
            if f.readonly and f.env:
                values[f.key] = os.environ.get(f.env, "") or _path_default(f.key)
                continue
            value = block.get(f.key, f.default)
            # The environment wins at runtime, so showing the file's value
            # would be showing something that is not in force.
            if f.env and os.environ.get(f.env):
                value = os.environ[f.env]
            values[f.key] = value
        out[section.id] = values
    return out


def _path_default(key: str) -> str:
    from .fleet import AION_HOME
    if key == "home":
        return str(AION_HOME)
    if key == "fs_root":
        return os.path.expanduser("~")
    return ""


def schema() -> list[dict]:
    return [s.as_dict() for s in SECTIONS]


def snapshot() -> dict:
    """Schema + values + the read-only surfaces, in one request."""
    from . import bridge
    base = {"schema": schema(), "values": read(), "persisted": list(PERSISTED)}
    try:
        base["providers"] = bridge.providers()
    except Exception:  # noqa: BLE001
        base["providers"] = []
    try:
        base["skills"] = bridge.skills().get("skills", [])
    except Exception:  # noqa: BLE001
        base["skills"] = []
    base["harnesses"] = harnesses()
    return base


# ── Writing ──────────────────────────────────────────────────────────────────
def write(section_id: str, values: dict, path=None) -> WriteResult:
    """Validate then persist one section. Nothing is written if nothing passed."""
    from .core import save_config_section

    out = validate(section_id, values)
    if section_id not in PERSISTED:
        if section_id in SECTION_BY_ID:
            out.rejected["_section"] = (
                f"{section_id} is not stored in the config file "
                f"({'environment-only' if section_id == 'paths' else 'a browser preference'})")
            out.applied = {}
        return out
    if not out.applied:
        return out
    try:
        written = save_config_section(section_id, out.applied, path)
        out.path = str(written)
    except Exception as e:  # noqa: BLE001
        out.rejected["_write"] = f"{type(e).__name__}: {str(e)[:160]}"
        out.applied = {}
    return out


# ── Harnesses ────────────────────────────────────────────────────────────────
# A list of dicts rather than a flat block, so it gets its own accessors. These
# are the knobs that change what a harness is allowed to do, which is why
# `requires_approval` is here: turning it ON is a safety improvement, and
# turning it OFF is exactly the kind of change worth being visible.
HARNESS_FIELDS = {
    "enabled": (BOOL, None, None),
    "requires_approval": (BOOL, None, None),
    "vram_mb": (INT, 0, 1_000_000),
    "max_steps": (INT, 1, 10_000),
    "tier": (CHOICE, None, None),
}
HARNESS_TIERS = ["local", "standard", "heavy", "remote"]


def harnesses() -> list[dict]:
    """Every configured harness with the fields Settings can change."""
    cfg = _config()
    out = []
    for h in cfg.get("harnesses", []) or []:
        if not isinstance(h, dict) or "id" not in h:
            continue
        out.append({
            "id": h.get("id", ""), "name": h.get("name", h.get("id", "")),
            "type": h.get("type", ""), "tier": h.get("tier", "standard"),
            "enabled": bool(h.get("enabled", True)),
            "requires_approval": bool(h.get("requires_approval", False)),
            "vram_mb": int(h.get("vram_mb", 0) or 0),
            "max_steps": int(h.get("max_steps", 0) or 0),
            "context_tags": list(h.get("context_tags", []) or []),
        })
    return out


def set_harness(harness_id: str, values: dict, path=None) -> dict:
    """Change one harness's settings in place, preserving everything else."""
    from .core import config_path, load_config
    from .fleet import write_json_atomic
    import json

    applied: dict = {}
    rejected: dict = {}
    for key, raw in (values or {}).items():
        spec = HARNESS_FIELDS.get(key)
        if spec is None:
            rejected[key] = "unknown setting"
            continue
        kind, lo, hi = spec
        f = Field(key=key, label=key, type=kind, min=lo, max=hi,
                  choices=HARNESS_TIERS if key == "tier" else [])
        ok, value, why = coerce(f, raw)
        if ok:
            applied[key] = value
        else:
            rejected[key] = why
    if not applied:
        return {"ok": False, "id": harness_id, "applied": {}, "rejected": rejected}

    p = config_path(path)
    try:
        data = json.loads(p.read_text()) if p.exists() else {}
    except (OSError, ValueError):
        data = {}
    items = data.get("harnesses")
    if not isinstance(items, list) or not items:
        items = list(load_config().get("harnesses", []))
    found = False
    for h in items:
        if isinstance(h, dict) and h.get("id") == harness_id:
            h.update(applied)
            found = True
            break
    if not found:
        return {"ok": False, "id": harness_id, "applied": {},
                "rejected": {"_id": f"no harness named {harness_id!r}"}}
    data["harnesses"] = items
    write_json_atomic(p, data)
    return {"ok": not rejected, "id": harness_id, "applied": applied,
            "rejected": rejected, "restart_needed": ["harnesses"],
            "path": str(p)}
