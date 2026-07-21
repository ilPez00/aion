"""
ui/app.py — the aion cockpit (Textual), rewritten for SPEED + INTUITION.

Performance fix (why the old one was slow): previously every bus message
tore down and re-mounted all widgets. Now we keep a STABLE widget tree and
update only the widget that changed (Textual reactivity / direct .update()).
Structural changes (workspace switch, task added/removed) do a cheap rebuild
of just that panel; data changes (progress tick) mutate one label's text.

Intuition fix: actions are immediate and discoverable — pressing a key DOES
the thing (no grammar memorization). A help overlay (?) lists every shortcut.
The command palette is optional, searchable, and shows completions.
"""
from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Any

from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Header, Static, Input, Label, Footer
from textual import events

from ..core import (
    Bus, Intent, IntentType, TOPIC_INTENT, TOPIC_VOICE, TOPIC_HERMES, TOPIC_SKILL, TOPIC_SETTINGS, load_config,
)
from ..harnesses import build_harnesses, TelemetryHarness, StatsHarness, ProjectsHarness, SystemHarness, HealthHarness, VaultHarness, PhysisHarness, AgentEntityHarness, BoardHarness, TIER_CHEAP, TIER_STANDARD, TIER_PREMIUM, HarnessConfig
from ..term import TermHarness
from ..input import Router, KeyboardMap, JoystickInput, VoiceInput, DeckInput
from ..store import Store


# shared render helper
def bar(pct: float, width: int = 18, color: str = "#7CFFB2") -> str:
    pct = max(0.0, min(1.0, pct))
    filled = int(round(pct * width))
    return f"[{color}]{'█' * filled}[/][#5a6b7b]{'░' * (width - filled)}[/] {int(pct * 100):3d}%"


class Cell(Static):
    """A single updatable text cell. Mutating .update() is cheap (no remount)."""
    DEFAULT_CSS = "Cell { height: auto; } Cell:hover { background: #15303f; }"

    def on_click(self) -> None:
        """Click activates the focused item or toggles palette on bottom."""
        app = self.app
        if not isinstance(app, AiOSApp):
            return
        app.action_activate()


class TermPane(Static):
    """Live embedded terminal pane — re-renders the pty/pyte screen fast.

    Keystrokes are forwarded to the pty by the app's on_key (when the Term
    workspace is active), not here, so there's a single clear passthrough path.
    """
    DEFAULT_CSS = "TermPane { height: 1fr; background: #0a0e14; }"

    def __init__(self, harness: TermHarness, **kwargs):
        super().__init__("", **kwargs)
        self._h = harness
        self.set_interval(0.08, self._redraw)

    def _redraw(self) -> None:
        try:
            self.update(self._h.render())
        except Exception:
            pass


def _key_bytes(key: str) -> bytes:
    table = {
        "enter": b"\r", "escape": b"\x1b", "tab": b"\t",
        "up": b"\x1b[A", "down": b"\x1b[B", "right": b"\x1b[C", "left": b"\x1b[D",
        "backspace": b"\x7f", "space": b" ",
        "ctrl+c": b"\x03", "ctrl+d": b"\x04", "ctrl+z": b"\x1a",
        "ctrl+a": b"\x01", "ctrl+b": b"\x02", "ctrl+e": b"\x05", "ctrl+f": b"\x06",
        "ctrl+k": b"\x0b", "ctrl+l": b"\x0c", "ctrl+n": b"\x0e", "ctrl+p": b"\x10",
        "ctrl+r": b"\x12", "ctrl+t": b"\x14", "ctrl+u": b"\x15", "ctrl+w": b"\x17",
        "ctrl+x": b"\x18", "ctrl+y": b"\x19",
        "pageup": b"\x1b[5~", "pagedown": b"\x1b[6~",
        "home": b"\x1b[H", "end": b"\x1b[F",
        "insert": b"\x1b[2~", "delete": b"\x1b[3~",
    }
    return table.get(key, b"")


class AiOSApp(App):
    CSS = """
    Screen { background: #0c1116; color: #c7d3df; }
    #header { height: 1; background: #11202c; }
    #rail   { width: 24; background: #0e161e; }
    #center { width: 1fr; background: #0c1116; }
    #right  { width: 36; background: #0e161e; border-left: solid #1c2b38; }
    #bottom { height: 1; background: #11202c; border-top: solid #1c2b38; dock: bottom; }
    #help   { background: #0e161e; border: solid #5ad1ff; padding: 1 2; width: 60%; height: 60%; }
    #palette { dock: bottom; background: #0e161e; }
    Cell { padding: 0 1; }
    .focus { background: #15303f; }
    """

    BINDINGS = [
        ("enter", "activate", "Activate"),
        ("?, slash", "help", "Help"),
        ("w", "tour", "Tour"),
        ("escape", "back", "Back"),
        ("space", "activate", "Activate"),
        ("p", "pause", "Pause/Resume"),
        ("x", "cancel", "Cancel"),
        ("r", "rerun", "Re-run"),
        ("a", "act", "Act on Jarvis"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.cfg = load_config()
        # before any store is built: this decides the state root and the port
        from .. import fleet as _fleet_boot
        _fleet_boot.configure(self.cfg.get("fleet"))
        self.bus = Bus()
        from ..core import SessionStore
        self.store_fs = SessionStore()
        # store owns the registry; build harnesses bound to it, then hand them
        # to the store (store is the brain, harnesses are its workers)
        self.store = Store(self.cfg, self.bus, store=self.store_fs)
        self.harnesses = build_harnesses(self.cfg["harnesses"], self.bus,
                                         self.store.registry, self.store_fs)
        self.store.harnesses = self.harnesses
        # share the config reference so theme changes in store reflect in app
        self.cfg = self.store.cfg
        self.router = Router(self.bus, self.cfg["keybindings"])
        self.keymap = KeyboardMap(self.cfg["keybindings"])
        self.voice = VoiceInput(model_size="tiny",
                                workspaces=self.cfg["workspaces"])
        self.router.register(self.voice)
        deck_cfg = self.cfg.get("deck", {})
        self.deck = DeckInput(port=deck_cfg.get("port"))
        if deck_cfg.get("enabled", True):
            self.router.register(self.deck)
        self._rail: list[Cell] = []
        self._center: list[Cell] = []
        self._right: list[Cell] = []
        from ..voice.persona import Persona
        from ..voice.output import VoiceOutput
        self.persona = Persona()
        self.voice_output = VoiceOutput()
        self._term_pane = None          # mounted TermPane widget (lazy)
        self._term_active = False       # whether the term workspace is active
        from ..observer import Observer
        self.observer = Observer()      # observant AI HUD over the Term pty

        self._jarvis_tick = 0           # proactive jarvis poll counter
        self._viz_tick = 0              # visualizer animation frame
        self._task_wave_history: list[float] = []  # recent task counts
        # remote instance control
        from ..remotes import RemoteServer, RemoteClient, RemoteNode
        from .. import fleet as _fleet
        # port is derived from the instance name, so a second cockpit on this
        # box gets its own listener instead of losing the bind race on 8765
        self._heartbeat = _fleet.Heartbeat()
        self._remote_server = RemoteServer(port=self._heartbeat.port)
        self._remote_client = RemoteClient()
        self._remote_nodes: dict[str, RemoteNode] = {}
        # per-node running-task history, feeds the Fleet sparklines
        self._fleet_history: dict[str, list[float]] = {}
        self._local_peers: list = []    # refreshed on a timer, not per render
        self._tour_active = False       # walkthrough mode
        self._tour_step = 0
        self._wizard_active = False
        self._wizard_step = 0
        self._wizard_data: dict[str, str] = {}
        self._wizard_install_status: dict[int, str] = {}
        self._wizard_install_output: str = ""

    # ----- compose: STABLE tree (built once) ----------------------------
    def compose(self) -> ComposeResult:
        yield Header(id="header")
        with Horizontal():
            yield VerticalScroll(id="rail")
            yield VerticalScroll(id="center")
            yield VerticalScroll(id="right")
        yield Static("", id="bottom")
        yield Input(placeholder="Ctrl-K: ask or command — try 'todo buy milk', 'help manual', 'agent create Alice'", id="palette")
        yield Static("", id="help")

    async def on_mount(self) -> None:
        self.query_one("#palette").display = False
        self.query_one("#help").display = False
        self.set_interval(1.0, self._tick)
        # all background HUD pollers (Jarvis HUD + Iron Man panels)
        for h in self.harnesses.values():
            if isinstance(h, (TelemetryHarness, StatsHarness, ProjectsHarness,
                              SystemHarness, HealthHarness, VaultHarness,
                              PhysisHarness, AgentEntityHarness, BoardHarness)):
                asyncio.create_task(h.start())
        self._render_all()
        self.router.register(JoystickInput())
        asyncio.create_task(self.router.start_all())

        # ── Remote server + nodes ─────────────────────────────────────────
        from ..remotes import RemoteNode
        from .. import fleet as _fleet
        self._remote_server.on_status = lambda: {
            "hostname": __import__("socket").gethostname(),
            "version": self.cfg.get("app_name", "aion"),
            "active_harness": self.store.state.active_harness,
            "running_count": sum(1 for t in self.store.registry.tasks.values()
                                 if t.state.value in ("running", "pending")),
            "tasks": [{"id": t.id, "label": t.label, "state": t.state.value,
                       "progress": t.progress}
                      for t in self.store.registry.tasks.values()][:20],
            "stats": {k: v for k, v in self.store.state.stats.items()
                      if k in ("system", "stats")},
        }
        self._remote_server.on_run = lambda p, h: (
            self.store.handle.__wrapped__(Intent.command(p)) if hasattr(self.store.handle, '__wrapped__')
            else self.store.handle(Intent.command(p)) or {"task_id": "?"}
        )
        self._remote_server.on_cancel = lambda tid: (
            self.store.registry.cancel(tid) or {}
        )
        asyncio.create_task(self._start_remote_server())
        # advertise this instance to same-host peers + keep the beat going
        beat_s = _fleet.settings().heartbeat_s
        self._beat()
        self.set_interval(beat_s, self._beat)
        self._refresh_local_peers()
        self.set_interval(beat_s, self._refresh_local_peers)
        for rn in self.cfg.get("remote_nodes", []):
            self._remote_nodes[rn["id"]] = RemoteNode(
                id=rn["id"], host=rn["host"], port=rn.get("port", 8765),
                label=rn.get("label", ""),
            )

        self.store.remote_callback = self._handle_remote_command
        self.store.fleet_callback = self._handle_fleet_command

        self.title = self.cfg["app_name"]
        self.sub_title = f"multi-harness · stats visualizer · mode: {self.store.state.active_mode}"
        # route bus -> store (store is the brain, app just re-renders)
        self.bus.subscribe(TOPIC_INTENT, self._on_intent)
        self.bus.subscribe(TOPIC_VOICE, self._on_voice)
        self.bus.subscribe(TOPIC_HERMES, self._on_hermes_event)
        self.bus.subscribe(TOPIC_SKILL, self._on_skill_event)
        asyncio.create_task(self.store._load_hermes_data())
    def _tick(self) -> None:
        self._viz_tick += 1
        # track task count history for wave visualizer
        running = sum(1 for t in self.store.registry.tasks.values()
                      if t.state.value in ("running", "pending"))
        self._task_wave_history.append(running / max(1, running + 1))
        self._task_wave_history = self._task_wave_history[-40:]

        # poll remote nodes every 3 ticks
        if self._viz_tick % 3 == 0 and self._remote_nodes:
            for node in self._remote_nodes.values():
                asyncio.create_task(self._remote_client.fetch_status(node))

        self._render_header()
        self._render_right()   # live HUD (tokens/agents) refresh without input
        # Proactive Jarvis: every ~10 ticks, scan state for suggestions
        self._jarvis_tick += 1
        if self._jarvis_tick >= 10:
            self._jarvis_tick = 0
            self._poll_jarvis()
        # workspaces that poll on a timer, not on input
        wid = self.cfg["workspaces"][self.store.state.active_ws]["id"]
        if wid in ("vault", "system", "sys", "desktop"):
            self._render_center()
        self._sync_term_pane()
        self._tick_observer()
        self._push_deck_hud()

    def _sync_term_pane(self) -> None:
        """Lazily mount/unmount the embedded terminal when entering/leaving
        the Term workspace. Considers last-render so we only act on change."""
        ws_id = self.cfg["workspaces"][self.store.state.active_ws]["id"]
        want = (ws_id == "term")
        desired_cmd = self.store.state.term_command
        # restart the pane in place when an `app <name>` command changed the
        # program while the Term workspace is already active
        if (want and self._term_active and self._term_pane is not None
                and desired_cmd and self._term_pane._h.command != desired_cmd):
            self._term_pane._h.stop()
            self._term_pane.remove()
            self._term_pane = None
            self._term_active = False
        if want == self._term_active:
            return
        self._term_active = want
        center = self.query_one("#center", expect_type=VerticalScroll)
        if want:
            # clear the normal cell list, mount the live pane
            for c in self._center:
                c.remove()
            self._center = []
            # `app <name>` overrides the default term harness (btop)
            term_h = None if desired_cmd else self.harnesses.get("term")
            if term_h is None:
                term_h = TermHarness(
                    HarnessConfig.from_dict({"id": "term", "type": "term",
                                             "command": desired_cmd or os.environ.get("SHELL", "bash")}),
                    self.bus, self.store.registry)
            term_h.ensure_running()
            self._term_pane = TermPane(term_h, id="termpane")
            center.mount(self._term_pane)
            self.observer.attach(term_h.command.split()[0])
        else:
            # tear down: kill pty, restore empty cell list (rebuilt on next render)
            if self._term_pane is not None:
                term_h = self._term_pane._h
                term_h.stop()
                self._term_pane.remove()
                self._term_pane = None
            self.observer.detach()
            self._center = []

    def _tick_observer(self) -> None:
        """Feed the Term screen to the observer HUD; fire the optional AI
        one-liner in an executor when due (never blocks the UI loop)."""
        if not (self._term_active and self._term_pane is not None):
            return
        self.observer.ai_enabled = self.store.state.observer_ai
        try:
            self.observer.tick(self._term_pane._h.render())
        except Exception:
            return
        if self.observer.want_ai_pass():
            prompt = self.observer.begin_ai_pass()

            async def ai_pass():
                from ..llm import ChatSession, chat_send
                loop = asyncio.get_event_loop()
                try:
                    reply = await loop.run_in_executor(
                        None, chat_send, ChatSession(), prompt, 15)
                    self.observer.set_ai_result(reply)
                except Exception:
                    self.observer.ai_failed()
            asyncio.create_task(ai_pass())

    def _poll_jarvis(self) -> None:
        """Proactive Jarvis: scan state, surface actionable suggestions.

        Calls the pure `suggest()` engine and stores the Suggestion list on
        state. If new, prepends the top suggestion to the activity log.
        """
        from ..jarvis import suggest
        try:
            sugg = suggest(self.store.state, self.cfg)
        except Exception:
            sugg = []
        self.store.state.suggestions = sugg
        if sugg:
            top = sugg[0].text
            # avoid spamming the log with the same line every poll
            if not self.store.state.logs or self.store.state.logs[-1] != top:
                self.store.state.logs.append(top)
                # keep logs bounded
                self.store.state.logs = self.store.state.logs[-50:]

    def _push_deck_hud(self) -> None:
        """Mirror aion status onto the CyclUno OLED (1 Hz, rate-limited)."""
        if not self.deck.link.available:
            return
        s = self.store.state
        running = [t for t in self.store.registry.tasks.values()
                   if t.state.value == "running"]
        if self.deck.app_mode:
            line = "APP PAD active"
        elif running:
            t = running[0]
            line = f"{t.harness[:6]} {int(t.progress * 100):3d}% r{len(running)}"
        else:
            line = f"{s.active_harness[:10]} idle"
        self.deck.link.send_note(line)

    # ===== INPUT =========================================================
    async def _on_intent(self, intent: Intent) -> None:
        self.store.handle(intent)
        self._render_all()

    async def _on_voice(self, msg: dict) -> None:
        try:
            await self.voice_output.say(msg.get("text", ""))
        except Exception:
            pass

    async def _on_hermes_event(self, msg: dict) -> None:
        self._render_all()

    async def _on_skill_event(self, msg: dict) -> None:
        self._render_all()

    def on_key(self, event: events.Key) -> None:
        # embedded terminal passthrough (Term workspace). Ctrl-K still opens
        # the palette (so `app <name>` can swap the program), and while the
        # palette is visible its handler below owns the keys.
        if (self._term_active and self._term_pane is not None
                and not self.query_one("#palette").display):
            if event.key == "ctrl+k":
                self._toggle_palette()
                event.prevent_default()
                return
            if event.key == "ctrl+t":
                # reserved: leave the terminal pane back to normal navigation
                return
            self._term_pane._h.send(
                event.character.encode("utf-8", "replace")
                if event.character else _key_bytes(event.key))
            event.prevent_default()
            return
        if self.query_one("#palette").display:
            if self._wizard_active:
                if event.key == "escape":
                    self._wizard_close()
                    event.prevent_default()
                elif event.key == "enter":
                    p = self.query_one("#palette", expect_type=Input)
                    text = p.value.strip()
                    p.value = ""
                    self.query_one("#palette").display = False
                    self._wizard_next(text)
                    event.prevent_default()
                return
            if event.key == "escape":
                self.query_one("#palette").display = False
                self.set_focus(None)
                event.prevent_default()
                return
            if event.key == "enter":
                # Submit palette text directly
                p = self.query_one("#palette", expect_type=Input)
                text = p.value.strip()
                p.value = ""
                self.query_one("#palette").display = False
                if text:
                    # compare command: "compare <prompt>" -> multi-model side-by-side
                    if text.lower().startswith("compare "):
                        asyncio.create_task(self.router.emit(Intent.compare(text[8:].strip())))
                    elif text.lower().replace("  ", " ").startswith("setup wizard"):
                        self._start_wizard()
                    elif text.lower().startswith("tour"):
                        self.action_tour()
                    elif text.lower() in ("manual", "help manual") or text.lower().startswith("manual "):
                        self.action_manual()
                    else:
                        asyncio.create_task(self.router.emit(Intent.command(text)))
                event.prevent_default()
                return
            # All other keys go through to Input widget
            return
        if self.query_one("#help").display:
            if self._tour_active:
                if event.key == "escape":
                    self._tour_close()
                elif event.key in ("enter", "space"):
                    self._tour_next()
                return
            if self._wizard_active:
                if event.key == "escape":
                    self._wizard_close()
                elif event.key in ("enter", "space"):
                    step = self.WIZARD_STEPS[self._wizard_step]
                    if step["type"] == "env":
                        self.query_one("#palette").display = True
                        self.query_one("#palette").focus()
                    else:
                        self._wizard_next("")
                return
            if event.key == "escape":
                self.query_one("#help").display = False
                event.prevent_default()
            return
        if event.key in ("?", "/"):
            self.action_help()
            event.prevent_default()
            return
        if event.key in ("enter", "space", "escape"):
            return
        if event.key == "ctrl+k":
            self._toggle_palette()
            return
        if event.key == "v":
            asyncio.create_task(self.voice.toggle())
            return
        intent = self.keymap.resolve(event.key)
        if intent is not None:
            event.prevent_default()
            asyncio.create_task(self.bus.publish(TOPIC_INTENT, intent))

    # action handlers (bound keys)
    def action_help(self) -> None:
        h = self.query_one("#help")
        if h.display:
            h.display = False
        else:
            h.update(self._help_text(extended=False))
            h.display = True

    def action_manual(self) -> None:
        h = self.query_one("#help")
        if h.display:
            h.display = False
        h.update(self._help_text(extended=True))
        h.display = True

    def action_back(self) -> None:
        if self.query_one("#palette").display:
            self.query_one("#palette").display = False

    def action_activate(self) -> None:
        self.store.handle(Intent.activate())

    def action_pause(self) -> None:
        self.store.handle(Intent(IntentType.PAUSE))

    def action_resume(self) -> None:
        self.store.handle(Intent(IntentType.RESUME))

    def action_cancel(self) -> None:
        self.store.handle(Intent(IntentType.CANCEL))

    def action_rerun(self) -> None:
        self.store.handle(Intent(IntentType.RERUN))

    def _toggle_palette(self) -> None:
        p = self.query_one("#palette")
        p.display = not p.display
        if p.display:
            p.focus()

    # ===== RENDER (targeted) ============================================
    def _render_all(self) -> None:
        self._render_header()
        self._render_rail()
        self._render_center()
        self._render_right()
        self._render_bottom()

    def _render_header(self) -> None:
        theme = self.cfg["theme"]
        s = self.store.state
        h = self.harnesses.get(s.active_harness)
        name = h.name if h else s.active_harness
        running = sum(1 for t in self.store.registry.tasks.values() if t.state.value == "running")
        vmode = " [VOICE ON]" if s.voice_active else ""
        if self.deck.link.available:
            vmode += " [PAD]" if s.deck_app else " [DECK]"
        clock = __import__("time").strftime("%H:%M:%S")
        status = f"running: {running}" if running else "standing by"
        # Mission strip: live count · type summary · pipeline · block
        wf = s.workflows
        wlive = s.workflows_live
        if wf:
            from ..workflows import stage_glyph, stage_theme_key, pipeline_strip, workflow_types_summary
            summary = workflow_types_summary(wf)
            top = next((r for r in wf if r.get("stage") not in ("done",)), wf[0])
            top_stage = top.get("stage", "plan")
            col = theme.get(stage_theme_key(top_stage), theme["dim"])
            g = stage_glyph(top_stage)
            pipe = pipeline_strip(top.get("pipeline", []), top_stage, theme, width=24)
            import re
            pipe_plain = re.sub(r'\[/?\w+(?:=\S+)?\]', '', pipe)[:20]
            blocked = top.get("blocked_by")
            blk = f" ⊘{blocked[:8]}" if blocked else ""
            status = (f"⇶[{theme['warn']}]{wlive}[/]"
                      f" [{theme['dim']}]{summary}[/]"
                      f" [{theme['dim']}]{pipe_plain}[/]{blk}")
        # Jarvis HUD: token burn + live agents from all sources
        hud = ""
        st = self.store.state.stats.get("stats")
        ext_count = sum(1 for w in wf if w.get("kind") == "tool") if wf else 0
        if st and st.get("ok"):
            from ..stats import human_tokens
            tot = st.get("in", 0) + st.get("out", 0) + st.get("reasoning", 0)
            cost = st.get("cost_usd", 0.0)
            live = st.get("live", 0) + ext_count
            costs = f" ${cost:.2f}" if cost else ""
            hud = (f"  [{theme['accent']}]Σ{human_tokens(tot)}[/]"
                   f"[{theme['dim']}]/{st.get('window','today')}[/]"
                   f"[{theme['ok']}]{costs}[/]"
                   f"  [{theme['warn']}]◆{live} live[/]")
        self.query_one("#header", expect_type=Header).text = (
            f"[{theme['accent']}]{self.persona.name}[/]  "
            f"harness: [{theme['ok']}]{name}[/]  "
            f"[{theme['accent']}]◉{s.active_mode}[/]  "
            f"{status}  "
            + (f"[{theme['err']}]⚠{len(s.suggestions)}[/]  " if s.suggestions else "")
            + f"[{theme['dim']}]{clock}{vmode}[/]"
        )

    def _render_rail(self) -> None:
        theme = self.cfg["theme"]
        rail = self.query_one("#rail", expect_type=VerticalScroll)
        items = self.cfg["workspaces"]
        if len(self._rail) != len(items):
            for c in self._rail:
                c.remove()
            self._rail = [Cell() for _ in items]
            rail.mount(*self._rail)
        for i, (cell, w) in enumerate(zip(self._rail, items)):
            mark = "▶" if i == self.store.state.active_ws else " "
            col = theme["accent"] if i == self.store.state.active_ws else theme["dim"]
            cls = "focus" if i == self.store.state.active_ws else ""
            cell.set_class(i == self.store.state.active_ws, "focus")
            cell.update(f"[{col}]{mark} {w['icon']} {w['title']}[/]")

    def _render_center(self) -> None:
        theme = self.cfg["theme"]
        ws = self.cfg["workspaces"][self.store.state.active_ws]["id"]
        center = self.query_one("#center", expect_type=VerticalScroll)

        if ws == "term":
            self._sync_term_pane()
            return

        items = self.store._current_items()
        # rebuild only if the item count changed (structural); else mutate text
        if len(self._center) != len(items):
            for c in self._center:
                c.remove()
            self._center = [Cell() for _ in items]
            center.mount(*self._center)
        for i, (cell, it) in enumerate(zip(self._center, items)):
            focused = i == self.store.state.focus
            cell.set_class(focused, "focus")
            cell.update(self._center_line(ws, it, focused, theme))

    def _set_center_text(self, text: str, center: VerticalScroll) -> None:
        """Render a single string into the center (used for boot + transient views)."""
        if len(self._center) != 1:
            for c in self._center:
                c.remove()
            self._center = [Cell()]
            center.mount(*self._center)
        self._center[0].update(text)

    def _center_line(self, ws: str, it: dict, focused: bool, theme: dict) -> str:
        f = "▌" if focused else " "
        col = theme["accent"] if focused else theme["dim"]
        if ws == "models":
            mark = "●" if it["id"] == self.store.state.active_harness else " "
            tag = it.get("context_tag", "")
            tag_label = f"[{theme['ok']}]●[/]" if tag == "active" else f"[{theme['dim']}]·[/]"
            return (f"[{col}]{f}{tag_label} {it['name']}[/]"
                    f"  [{theme['dim']}]run:{it['running']}[/]")
        if ws == "tasks":
            # board panel item embedded in tasks list
            if it.get("type") == "board":
                return self._board_panel(theme, item=it)
            st = it["state"]
            scol = {"running": theme["warn"], "done": theme["ok"], "failed": theme["err"],
                    "pending": theme["dim"], "cancelled": theme["dim"],
                    "interrupted": theme["warn"]}.get(st, theme["dim"])
            eta = f"eta {int(it['eta'])}s" if it.get("eta") else ""
            note = " [Enter: re-run]" if st in ("interrupted", "cancelled", "failed") else \
                   " [Enter: pause]" if st == "running" else ""
            return (f"[{col}]{f}{it['id']} {it['label']}[/]\n"
                    f"  [{scol}]{st}{' (paused)' if it.get('paused') else ''}[/] "
                    f"{bar(it['progress'])} [{theme['dim']}]{eta}{note}[/]")
        if ws == "settings":
            if it.get("type") == "fleet_header":
                return (f"[{theme['accent']}]FLEET[/]  "
                        f"[{theme['dim']}]Ctrl-K: 'fleet set <key> <value>'[/]\n"
                        f"  [{theme['dim']}]state    {it.get('instance_root','')}[/]\n"
                        f"  [{theme['dim']}]shared   {it.get('shared_root','')}[/]")
            if it.get("type") == "fleet_setting":
                # an env override cannot be changed from here -- show why
                locked = it.get("source", "").startswith("env")
                vcol = theme["warn"] if locked else theme["ok"]
                note = ""
                if locked:
                    note = f"  [{theme['warn']}]{it.get('source')}[/]"
                elif it.get("restart"):
                    note = f"  [{theme['dim']}]restart to apply[/]"
                return (f"[{col}]{f}{it.get('key','?'):18s}[/]"
                        f"[{vcol}]{it.get('value','')}[/]{note}")
            if it.get("type") == "skill":
                desc = it.get("description", "")[:60]
                return (f"[{col}]{f}{it.get('name','?')}[/]  "
                        f"[{theme['dim']}]{desc}[/]")
            if it.get("type") == "env_hint":
                return (f"[{theme['dim']}]{f} {it.get('text','')}[/]\n"
                        f"  [{theme['warn']}]{it.get('hint','')}[/]")
            ep = it.get("endpoint", "")
            key = it.get("key_preview", "")
            return (f"[{col}]{f}{it['id']}[/]\n"
                    f"  [{theme['dim']}]{ep}[/]\n"
                    f"  [{theme['ok'] if key else theme['err']}]{key}[/]")
        if ws == "vault":
            if it.get("type") == "memory_fact":
                q = self.store.memory.query
                head = f" [filter: {q}]" if q else ""
                return (f"[{col}]{f}◎ #{it['n']} {it['text']}[/]  "
                        f"[{theme['dim']}]{it['when']}{head}[/]")
            return self._vault_line(it, focused, theme)
        if ws == "net":
            return self._net_panel(theme)
        if ws in ("system", "sys"):
            return self._sys_panel(theme)
        if ws == "desktop":
            return self._desktop_panel(theme)
        if ws == "agent":
            mode_label = it.get("mode_label", "")
            mode_line = f"[{theme['accent']}]◈ {mode_label}[/]\n[{theme['dim']}]" + "─" * 40 + "[/]\n" if mode_label else ""
            if it.get("type") == "agents":
                return mode_line + self._agent_cards_panel(theme)
            if it.get("type") == "swarm_dashboard":
                return mode_line + self._swarm_panel(theme, item=it)
            if it.get("type") in ("compare", "chat"):
                return mode_line + self._agent_panel(theme)
            return mode_line + self._agent_panel(theme)
        # agent log fallback
        tail = "\n".join(self.store.state.logs[-40:]) or "[agent] no output yet — run a harness"
        return f"[{theme['dim']}]{tail}[/]"

    def _vault_line(self, it: dict, focused: bool, theme: dict) -> str:
        f = "▌" if focused else " "
        col = theme["accent"] if focused else theme["dim"]
        if it.get("name") == "(none)":
            return (f"[{col}]{f}vault not loaded[/]\n"
                    f"  [{theme['dim']}]{it.get('preview','') or 'notes/ not found'}[/]")
        title = it.get("title", it.get("name", "?"))
        degree = it.get("degree", 0)
        bl = len(it.get("backlinks", []))
        lk = len(it.get("links", []))
        tags = " ".join("#" + t for t in it.get("tags", [])[:4])
        head = it.get("headings", [])
        head_txt = f"  [{theme['dim']}]{' · '.join(head[:3])}[/]" if head else ""
        preview = it.get("preview", "")
        prev_txt = f"\n    [{theme['dim']}]{preview[:90]}[/]" if preview else ""
        tag_txt = f"  [{theme['warn']}]{tags}[/]" if tags else ""
        return (f"[{col}]{f}{title}[/]  [{theme['dim']}][{lk}→{bl}][/]\n"
                f"  [{theme['accent']}]⛓ {degree} links[/]{tag_txt}{head_txt}{prev_txt}")

    def _sys_panel(self, theme: dict) -> str:
        """Iron Man HUD: computer + real-life stats, rendered as gauges."""
        from .gauges import (hbar, core_grid, sparkline, metric, gauge_panel,
                             mem_readable, bytes_per_sec)
        from .visualizers import holo_gauge, spectrum_eq
        st = self.store.state.stats
        blocks: list[str] = []

        # ── Holo Gauges ───────────────────────────────────────────────────
        sys_ = st.get("system")
        if sys_ and sys_.get("ok"):
            cpu_pct = sys_["cpu"]["total_pct"] / 100.0
            mem_pct = sys_["mem"]["pct"] / 100.0
            disk_pct = max((d["pct"] for d in sys_.get("disks", [])), default=0) / 100.0
            gauges = [
                holo_gauge(cpu_pct, self._viz_tick, width=14, label="CPU", color=theme["warn"]),
                holo_gauge(mem_pct, self._viz_tick, width=14, label="RAM", color=theme["accent"]),
                holo_gauge(disk_pct, self._viz_tick, width=14, label="DSK", color=theme["warn"]),
            ]
            gauge_lines = [g.split("\n") for g in gauges]
            # merge side by side: row by row
            merged = []
            for row_i in range(3):  # 3 lines per gauge
                parts = []
                for g in gauge_lines:
                    if row_i < len(g):
                        parts.append(g[row_i])
                merged.append("  ".join(parts))
            blocks.append("\n".join(merged))

        # ---- COMPUTER (system) ----
        sys_ = st.get("system")
        if sys_ and sys_.get("ok"):
            cpu = sys_["cpu"]
            cpu_line = (metric("CPU", f"{cpu['total_pct']}", "%", theme["warn"]) +
                        f"  [{theme['dim']}]{cpu['cores']}c load {cpu['load1']}[/]")
            grid = core_grid(cpu["per_core_pct"])
            ram_line = (metric("RAM", f"{sys_['mem']['pct']}", "%", theme["accent"]) +
                        f"  [{theme['dim']}]{mem_readable(sys_['mem']['used'])}/"
                        f"{mem_readable(sys_['mem']['total'])}[/]")
            ram_bar = hbar(sys_["mem"]["pct"] / 100.0, width=20, color=theme["accent"])
            blocks.append(gauge_panel("COMPUTER",
                         "\n  ".join([cpu_line, grid, ram_line, ram_bar]),
                         theme["accent"]))
            disk_lines = []
            for d in sys_["disks"]:
                dl = (metric(d["mount"], f"{d['pct']}", "%", theme["warn"]) +
                      f"  [{theme['dim']}]{mem_readable(d['free'])} free[/]")
                disk_lines.append(dl)
                disk_lines.append("  " + hbar(d["pct"] / 100.0, width=16, color=theme["warn"]))
            if disk_lines:
                blocks.append(gauge_panel("STORAGE", "\n  ".join(disk_lines), theme["warn"]))
            net = sys_["net"]
            net_line = (metric("up", bytes_per_sec(net["up_bps"]), "", theme["ok"]) + "\n  " +
                        metric("dn", bytes_per_sec(net["down_bps"]), "", theme["ok"]) + "\n  " +
                        f"[{theme['dim']}]{net['conns']} active conns[/]")
            blocks.append(gauge_panel("NETWORK", net_line, theme["ok"]))
            gpu = sys_.get("gpu") or {}
            if gpu:
                if "gpu_util_pct" in gpu:
                    g = (metric("util", f"{gpu['gpu_util_pct']}", "%", theme["accent"]) + "\n  " +
                         hbar(gpu["gpu_util_pct"] / 100.0, width=16, color=theme["accent"]) +
                         "\n  " +
                         f"[{theme['dim']}]{gpu.get('gpu_mem_mb',0)}/"
                         f"{gpu.get('gpu_mem_total_mb',0)} MB[/]")
                    blocks.append(gauge_panel("GPU", g, theme["accent"]))
                elif "gpu_models" in gpu:
                    blocks.append(gauge_panel(
                        "GPU",
                        f"[{theme['ok']}]{gpu['gpu_models']} model(s) loaded · "
                        f"{gpu.get('gpu_vram_mb',0)}MB[/]", theme["accent"]))
        else:
            blocks.append(gauge_panel("COMPUTER", "[#5a6b7b](stats unavailable)[/]", theme["accent"]))

        # ---- THERMAL (CPU/thermal sensors) ----
        th = sys_.get("thermal") if sys_ and sys_.get("ok") else None
        if th:
            cpu = th.get("cpu") or []
            other = th.get("other") or []
            lines = []
            if cpu:
                max_c = th.get("max_cpu_c", max((t["current"] for t in cpu), default=0))
                lines.append(metric("max", f"{max_c:.1f}", "°C", theme["err"] if max_c > 85 else theme["warn"] if max_c > 70 else theme["ok"]))
                for t in cpu[:4]:  # top 4 cpu sensors
                    label = t["label"]
                    c = t["current"]
                    col = theme["err"] if c > 85 else theme["warn"] if c > 70 else theme["ok"]
                    lines.append(f"  {metric(label, f'{c:.1f}', '°C', col)}")
            if other:
                for t in other[:4]:  # top 4 other sensors
                    label = t["label"]
                    c = t["current"]
                    lines.append(f"  {metric(label, f'{c:.1f}', '°C', theme['dim'])}")
            if lines:
                blocks.append(gauge_panel("THERMAL", "\n  ".join(lines), theme["err"]))

        # ---- REAL LIFE (health) ----
        hl = st.get("health")
        if hl and hl.get("ok"):
            av = hl.get("avg_7d", {})
            latest = hl.get("latest") or {}
            lines = [
                metric("steps", str(latest.get("steps", 0)), "", theme["ok"]),
                metric("bpm", str(latest.get("heart_rate", 0)), "", theme["err"]),
                metric("sleep", f"{latest.get('sleep_hours',0)}", "h", theme["accent"]),
                metric("active", f"{latest.get('active_calories',0)}", "kcal", theme["warn"]),
                f"[{theme['dim']}](7d avg steps {av.get('steps',0)} · "
                f"sleep {av.get('sleep_hours',0)}h)[/]",
            ]
            series = hl.get("series", {})
            if series.get("steps"):
                lines.append("  " + sparkline(series["steps"], width=20))
            blocks.append(gauge_panel("REAL LIFE", "\n  ".join(lines), theme["ok"]))
        else:
            blocks.append(gauge_panel("REAL LIFE",
                         "[#5a6b7b](no health data — source: google/apple/json)[/]",
                         theme["ok"]))

        # ---- PHYSIS (coherence brain) ----
        ph = st.get("physis")
        if ph:
            a, di, ok, warn = theme["accent"], theme["dim"], theme["ok"], theme["warn"]
            degraded = ph.get("degraded", False)
            kind = ph.get("kind", "?")
            semantic = ph.get("semantic", False)
            status = f"[{warn}]DEGRADED[/]" if degraded else f"[{ok}]LIVE[/]"
            ph_lines = [
                f"  engine: {status}  embedder: [{a}]{kind}[/]  "
                f"semantic: {'yes' if semantic else 'no'}",
            ]
            g = ph.get("graph", {}) or {}
            nodes = g.get("nodes", []) if isinstance(g, dict) else []
            edges = g.get("edges", []) if isinstance(g, dict) else []
            if nodes:
                ph_lines.append(f"[{di}]holarchy: {len(nodes)} nodes · {len(edges)} edges[/]")
                for n in nodes[:5]:
                    label = n.get("label", n.get("id", "?")) if isinstance(n, dict) else str(n)
                    ph_lines.append(f"  [{a}]•[/] {label[:40]}")
            blocks.append(gauge_panel("PHYSIS", "\n  ".join(ph_lines), theme["accent"]))

        # ── Spectrum Analyzer ─────────────────────────────────────────────
        sys_ = st.get("system")
        cpu_pct = (sys_["cpu"]["total_pct"] / 100.0) if sys_ and sys_.get("ok") else 0
        mem_pct = (sys_["mem"]["pct"] / 100.0) if sys_ and sys_.get("ok") else 0
        disk_pct = max((d["pct"] for d in sys_.get("disks", [])), default=0) / 100.0 if sys_ and sys_.get("ok") else 0
        running_count = sum(1 for t in self.store.registry.tasks.values()
                            if t.state.value in ("running", "pending"))
        spec = spectrum_eq(
            [cpu_pct, mem_pct, disk_pct, min(running_count / 3.0, 1.0)],
            self._viz_tick,
            height=4,
            labels=["CPU", "RAM", "DSK", "TASK"],
        )
        blocks.append(gauge_panel("SPECTRUM", spec, theme["accent"]))

        return "\n\n".join(blocks)

    def _swarm_panel(self, theme: dict, item: dict | None = None) -> str:
        """Render the multi-agent swarm dashboard."""
        from .gauges import hbar
        if item is None:
            return (f"[{theme['dim']}]No active swarm.[/]\n"
                    f"  [{theme['accent']}]swarm create <goal>[/] to start.\n"
                    f"  [{theme['dim']}]e.g. swarm create research and prototype a dashboard[/]")
        data = item.get("data", {})
        lines = []
        lines.append(f"[{theme['accent']}]SWARM[/]")
        s = data
        counts = f"🧠 {s.get('working',0)}W · {s.get('waiting',0)}⏳ · {s.get('done',0)}✓ · {s.get('failed',0)}✗ · {s.get('blocked',0)}⊘"
        lines.append(f"  {counts}")
        lines.append(f"[{theme['dim']}]─ Agents ──────────────────────────────────────[/]")
        agents = s.get("agents", [])
        if not agents:
            lines.append(f"  [{theme['dim']}](no agents yet)[/]")
        for a in agents[:10]:
            icon = {"idle": "○", "working": "●", "waiting": "⌛", "done": "✓",
                    "failed": "✗", "blocked": "⊘"}.get(a.get("status","idle"), "?")
            col = theme["ok"] if a.get("status") == "done" else \
                  theme["warn"] if a.get("status") == "working" else theme["dim"]
            bar = hbar(a.get("progress", 0), width=8, color=col)
            lines.append(f"  [{col}]{icon}[/] [{theme['accent']}]{a['name'][:16]:16s}[/]"
                         f" {bar}  [{theme['dim']}]{a['goal'][:28]}[/]")
        plan = s.get("active_plan")
        if plan:
            lines.append(f"[{theme['dim']}]─ Plan ────────────────────────────────────────[/]")
            lines.append(f"  [{theme['accent']}]goal:[/] {plan.get('goal','')[:50]}")
            lines.append(f"  [{theme['dim']}]steps: {plan.get('steps',0)} · done: {plan.get('done',0)}[/]")
        # DAG: show agent dependency graph when >1 agent with deps
        dag_agents = [a for a in agents if a.get("deps") or a.get("dependencies")]
        if dag_agents:
            from ..workflows import swarm_dag
            dag = swarm_dag(agents, theme)
            if dag:
                lines.append(f"[{theme['dim']}]─ DAG ─────────────────────────────────────────[/]")
                for dag_line in dag.split("\n"):
                    stripped = dag_line.replace("DAG ", "")
                    lines.append(f"  {stripped}")
        lines.append(f"[{theme['dim']}]swarm create|add|run|status|stop[/]")
        return "\n".join(lines)

    def _board_panel(self, theme: dict, item: dict | None = None) -> str:
        """Render the kanban board workspace: 3-column post-it layout."""
        a, ok_, wa, er, di = theme["accent"], theme["ok"], theme["warn"], theme["err"], theme["dim"]
        if item is None:
            return (f"[{di}]No boards.[/]\n"
                    f"  [{a}]board create <title>[/] to start.\n"
                    f"  [{di}]e.g. board create Research RAG[/]")
        data = item
        boards = data.get("boards", [])
        if not boards:
            return f"[{di}]No boards. 'board create <title>' to start.[/]"
        lines = [f"[{a}]BOARD[/]"]
        for bi, b in enumerate(boards[:3]):
            focused = bi == self.store.state.focus
            mark = "▌" if focused else " "
            col = a if focused else di
            col_data = b.get("column_data", {})
            bl = len(col_data.get("backlog", []) or [])
            ac = len(col_data.get("active", []) or [])
            dn = len(col_data.get("done", []) or [])
            glance = f"B{bl} A{ac} D{dn}" if (bl or ac or dn) else f"{b.get('card_count', 0)} cards"
            lines.append(f"[{col}]{mark}⬡ {b['title']}[/]  [{di}]{glance}[/]")
            cols = b.get("columns", ["backlog", "active", "done"])
            for ci, col_name in enumerate(cols):
                cards = col_data.get(col_name, [])
                icon = {"backlog": "📋", "active": "⚡", "done": "✓"}.get(col_name, "·")
                clr = {"backlog": di, "active": wa, "done": ok_}.get(col_name, di)
                lines.append(f"  [{clr}]{icon} {col_name.upper()}[/] [{di}]({len(cards)})[/]")
                for c in cards[:5]:
                    agent_tag = f" @{c.get('agent_id','')[:6]}" if c.get("agent_id") else ""
                    lines.append(f"    [{di}]· {c['title'][:36]}{agent_tag}[/]")
                if len(cards) > 5:
                    lines.append(f"    [{di}]· ... +{len(cards)-5} more[/]")
            if bi < len(boards) - 1:
                lines.append(f"  [{di}]─" + "─" * 44 + "[/]")
        lines.append(f"[{di}]board create|add|move|assign|list|cards[/]")
        return "\n".join(lines)

    def _agent_cards_panel(self, theme: dict) -> str:
        """Render persistent agent entities as cards with peripheral health context."""
        items = self.store._current_items()
        a, ok_, wa, er, di = theme["accent"], theme["ok"], theme["warn"], theme["err"], theme["dim"]
        if not items or items[0].get("type") == "empty":
            return (f"[{di}]No agents.[/]\n"
                    f"  [{a}]agent create <name> [goal][/] to start.\n"
                    f"  [{di}]e.g. agent create Alice research-papers[/]")
        data = items[0]
        agents = data.get("agents", [])
        hc = data.get("health_context", {})
        if not agents:
            return f"[{di}]No agents. 'agent create <name>' to start.[/]"
        lines = []

        # peripheral health context bar
        if hc:
            from .gauges import hbar, sparkline
            health_series = hc.get("series", {})
            lines.append(f" [{ok_}]❤[/] {hc.get('steps',0)} steps · "
                         f"{hc.get('heart_rate',0)} bpm · "
                         f"{hc.get('sleep_hours',0)}h sleep · "
                         f"{hc.get('active_calories',0)} kcal")
            lines.append(f" [{di}]7d avg: {hc.get('avg_steps_7d',0)} steps · "
                         f"{hc.get('avg_sleep_7d',0)}h sleep[/]")
            if health_series.get("steps"):
                lines.append("  " + sparkline(health_series["steps"], width=20))
            lines.append(f" [{di}]" + "─" * 48 + "[/]")

        for i, ag in enumerate(agents):
            focused = i == self.store.state.focus
            mark = "▌" if focused else " "
            col = a if focused else di
            status_icon = {"idle": "○", "working": "●", "blocked": "⊘"}.get(
                ag.get("status", "idle"), "○")
            status_col = {"idle": di, "working": wa, "blocked": er}.get(
                ag.get("status", "idle"), di)
            lines.append(f"[{col}]{mark}[{status_col}]{status_icon}[/] "
                         f"{ag['name']}[/]  [{di}]cap: "
                         f"{','.join(ag.get('capabilities',[]))})[/]"
                         f"  [{ok_}]🧠 {ag.get('mem_count',0)} mem[/]")
            if ag.get("goal"):
                lines.append(f"  [{di}]goal:[/] {ag['goal'][:52]}")
            task_status = ag.get("task_status", "idle")
            task_label = ag.get("task_label", "")
            task_prog = ag.get("task_progress", 0.0)
            if task_status != "idle" and task_label:
                from .gauges import hbar
                lines.append(f"  [{wa}]●[/] {task_label[:40]} "
                             f"{hbar(task_prog, 8, wa)}")
            else:
                lines.append(f"  [{di}](idle — no active task)[/]")
            if i < len(agents) - 1:
                lines.append(f"  [{di}]─" + "─" * 48 + "[/]")
        lines.append(f"[{di}]agent create|assign|status|list|forget[/]")
        return "\n".join(lines)

    def _agent_panel(self, theme: dict) -> str:
        """Render the Agent workspace: either chat or side-by-side compare."""
        items = self.store._current_items()
        a, di = theme["accent"], theme["dim"]
        if not items:
            return f"[{di}]Agent workspace ready. Type a message in Ctrl-K or select a harness.[/]"
        it = items[0]
        # Compare view
        if it.get("type") == "compare":
            prompt = it.get("prompt", "")
            answers = it.get("answers", {})
            done = it.get("done", False)
            lines = [f" [{di}]Q: {prompt[:44]}[/]"]
            provs = list(answers.keys())
            # two-column layout
            left = provs[0] if len(provs) > 0 else ""
            right = provs[1] if len(provs) > 1 else ""
            lines.append(f" [{di}]─ {left or '-'} ── {right or '-'} ─[/]")
            L = (answers.get(left, "") or "")[:200]
            R = (answers.get(right, "") or "")[:200]
            for i in range(max(len(L), len(R), 1)):
                lc = L[i:i+24] if i < len(L) else ""
                rc = R[i:i+24] if i < len(R) else ""
                lines.append(f" [{di}]{lc:<24}│{rc}[/]")
            return "\n".join(lines)
        # Chat view
        msgs = it.get("messages", [])
        if not msgs:
            return f"[{di}]Agent workspace ready. Type a message in Ctrl-K or select a harness.[/]"
        lines = []
        for msg in msgs[-20:]:
            role = msg.get("role", "?")
            content = msg.get("content", "")
            col = a if role == "assistant" else theme["ok"] if role == "user" else di
            name = "You" if role == "user" else "AI" if role == "assistant" else role
            lines.append(f" [{col}]{name}:[/]")
            while content:
                lines.append(f" [{di}]{content[:48]}[/]")
                content = content[48:]
        lines.append(f"[{di}]Type 'search <q>' or a message in Ctrl-K to chat[/]")
        return "\n".join(lines)

    def _desktop_panel(self, theme: dict) -> str:
        """Clean desktop: status bar + wokspace dock + context widget."""
        from .gauges import hbar, core_grid
        from ..context import ContextRouter
        items = self.store._current_items()
        data = items[0].get("data", {}) if items else {}
        a, ok_, wa, er, di = theme["accent"], theme["ok"], theme["warn"], theme["err"], theme["dim"]
        ctx = ContextRouter().resolve(self.store)
        domain = ctx.domain.value
        UL, UR, HL, H, V = "┌", "┐", "┬", "─", "│"
        sep = f"[{a}]" + "─" * 50 + "[/]"
        p = []

        ws_icons = {"desktop":"⬡","models":"◈","tasks":"▤","agent":"✦",
                    "vault":"📓","system":"🖥","term":"▣","settings":"⚙","net":"🌐"}
        ws_ids = [w["id"] for w in self.cfg["workspaces"]]

        cpu = data.get("cpu_pct", 0); ram = data.get("ram_pct", 0)
        disk = data.get("disk_pct", 0)
        tr = data.get("tasks_running", 0); td = data.get("tasks_done", 0)
        cc = er if cpu > 80 else wa if cpu > 50 else ok_
        rc = er if ram > 80 else wa if ram > 50 else ok_
        dc = er if disk > 80 else wa if disk > 50 else ok_

        # ┌── STATUS BAR ─────────────────────────────────────────────────┐
        p.append(f" [{a}]{UL}{H*3} STATUS {H*35}{UR}[/]")
        p.append(f" [{a}]{V}[/]"
                 f"[{di}]CPU[/][{cc}] {cpu:2.0f}%[/]"
                 f"  [{di}]RAM[/][{rc}] {ram:2.0f}%[/]"
                 f"  [{di}]DSK[/][{dc}] {disk:2.0f}%[/]"
                 f"  [{di}]TASKS[/] [{ok_}]●{tr}[/] [{ok_}]✓{td}[/]"
                 f"  [{di}]{ctx.icon} {ctx.label}[/]"
                 f"  [{a}]{V}[/]")

        # ┌── WORKSPACE DOCK ─────────────────────────────────────────────┐
        p.append(f" [{a}]{H*50}[/]")
        row1 = []
        for w in ws_ids[:4]:
            ic = ws_icons.get(w, "·")
            row1.append(f"[{a}]{ic}[/] [{di}]{w[:6]}[/]")
        p.append(f" [{a}]{V}[/]  {'  '.join(row1)}         [{a}]{V}[/]")
        row2 = []
        for w in ws_ids[4:]:
            ic = ws_icons.get(w, "·")
            row2.append(f"[{a}]{ic}[/] [{di}]{w[:6]}[/]")
        p.append(f" [{a}]{V}[/]  {'  '.join(row2)}         [{a}]{V}[/]")

        # ┌── WIDGETS ───────────────────────────────────────────────────┐
        context_blocks = []

        # Projects (always)
        proj = data.get("projects", [])
        if proj:
            block = [f"[{a}]PROJECTS[/]"]
            for pr in proj[:3]:
                name = pr.get("name", pr.get("id", "?"))[:18]
                dirty = pr.get("dirty", 0)
                branch = pr.get("branch", "")
                badges = f"{f' ~{dirty}' if dirty else ''}{f' @{branch}' if branch else ''}"
                block.append(f" [{ok_}]⬢[/] [{di}]{name}{badges}[/]")
            context_blocks.append("\n".join(block))

        # Todos (always)
        todos = data.get("todos", [])
        if todos:
            block = [f"[{a}]TODOS[/]"]
            for t in todos[:3]:
                ic, cl = ("✓", di) if t["done"] else ("○", wa)
                block.append(f" [{cl}]{ic}[/] [{di}]{t['text'][:40]}[/]")
            context_blocks.append("\n".join(block))

        # Active tasks + AI sessions (always)
        active = [t for t in data.get("active_tasks", [])
                  if t["state"] in ("running", "pending")]
        sessions = data.get("recent_sessions", [])
        interrupted = data.get("interrupted_tasks", [])
        if active or sessions or interrupted:
            block = [f"[{a}]SESSIONS[/]"]
            for t in active[:2]:
                ic = "⏸" if t.get("paused") else "●"
                harness = t['harness'][:6]
                label = t['label'][:18]
                block.append(f" [{wa}]{ic}[/] [{di}]{harness}[/] {label}"
                             f" {hbar(t['progress'], 6, wa)}")
            for s in sessions[:4]:
                s_ic = {"running": "●", "ended": "✓", "zombie": "⊘"}.get(s["status"], "?")
                s_cl = {"running": wa, "ended": ok_, "zombie": er}.get(s["status"], di)
                repo = s.get("repo", "")[:8]
                model = s["model"][:16]
                block.append(f" [{s_cl}]{s_ic}[/] [{di}]{repo:8s}[/] {model}"
                             f"  [{s_cl}]{s['status']}[/]")
            for t in interrupted[:2]:
                block.append(f" [{er}]⊘[/] [{di}]{t['harness'][:6]}[/] {t['label'][:18]} [{er}]interrupted[/]")
            if not active and not sessions and not interrupted:
                block.append(f" [{di}](idle)[/]")
            context_blocks.append("\n".join(block))

        if context_blocks:
            p.append(f" [{a}]{H*50}[/]")
            for b in context_blocks:
                for line in b.split("\n"):
                    p.append(f" [{a}]{V}[/] {line}")

        # ┌── MISSIONS / VIZ ───────────────────────────────────────────────┐
        wf = self.store.state.workflows
        wlive = self.store.state.workflows_live
        if wf and wlive:
            from ..workflows import desktop_missions
            ms = desktop_missions(wf, theme, max_rows=3)
            ms_lines = ms.split("\n")
            p.append(f" [{a}]{H*3} MISSIONS {H*36}┐[/]")
            for line in ms_lines:
                p.append(f" [{a}]{V}[/] {line}")
        else:
            from .visualizers import pulse_radar
            st = self.store.state.stats
            sys_ = st.get("system") or {}
            cpu = (sys_.get("cpu") or {}).get("total_pct", 0) / 100.0
            mem = (sys_.get("mem") or {}).get("pct", 0) / 100.0
            running_count = sum(1 for t in self.store.registry.tasks.values()
                                if t.state.value in ("running", "pending"))
            _rings = [
                {"label": "CPU", "value": cpu,
                 "items": ["■"] * int(cpu * 12)},
                {"label": "RAM", "value": mem,
                 "items": ["■"] * int(mem * 12)},
                {"label": "TASKS", "value": min(running_count / 5.0, 1.0),
                 "items": ["●"] * min(running_count, 12)},
            ]
            _viz = pulse_radar(_rings, self._viz_tick, size=10)
            _viz_lines = _viz.split("\n")
            p.append(f" [{a}]{H*3} VIZ {H*40}'┐[/]".replace("'┐", "┐"))
            for line in _viz_lines:
                p.append(f" [{a}]{V}[/] {line}")

        # ┌── QUICK ──────────────────────────────────────────────────────┐
        p.append(f" [{a}]{H*3} COMMANDS {H*35}┐[/]")
        p.append(f" [{a}]{V}[/] [{di}]Ctrl-K: say what you want[/]"
                 f"  [{a}]{V}[/]")
        p.append(f" [{a}]{V}[/]"
                 f" [{di}]'todo <t>' · 'swarm <goal>' · 'compare <q>' · 'agent create <n>'[/]"
                 f"  [{a}]{V}[/]")
        p.append(f" [{a}]└" + "─" * 48 + "┘[/]")
        return "\n".join(p)

    async def _handle_fleet_command(self, text: str) -> str:
        """Handle 'fleet show|set|token' palette commands."""
        from .. import fleet as _fleet
        from ..core import save_config_section

        parts = text.split(maxsplit=3)
        sub = parts[1] if len(parts) > 1 else "show"

        if sub == "show":
            rows = [f"  {d['key']:18s} {str(d['value']):10s} "
                    f"({d['source']}{', restart' if d['restart'] else ''})"
                    for d in _fleet.describe_settings()]
            return "fleet settings:\n" + "\n".join(rows)

        if sub == "token":
            action = parts[2] if len(parts) > 2 else "show"
            if action == "show":
                return f"fleet token: {_fleet.load_or_create_token()}\n" \
                       f"copy to every machine: scp {_fleet.token_path()} <host>:~/.aion/token"
            if action == "rotate":
                # every other node stops being able to reach this one until the
                # new secret is copied across -- say so rather than surprise you
                _fleet.token_path().unlink(missing_ok=True)
                _fleet.load_or_create_token()
                return ("fleet token rotated. Every other node is now locked out "
                        f"until you copy {_fleet.token_path()} to it.")
            return "usage: fleet token show | fleet token rotate"

        if sub == "set" and len(parts) >= 4:
            key, raw = parts[2], parts[3].strip()
            if key not in _fleet.FleetSettings.FIELDS:
                return (f"unknown fleet setting '{key}' "
                        f"(try: {', '.join(_fleet.FleetSettings.FIELDS)})")
            if key in ("instance", "listen"):
                value: object = raw
                if key == "listen" and raw not in ("local", "lan"):
                    return "fleet set listen local | lan"
            else:
                try:
                    value = float(raw)
                except ValueError:
                    return f"fleet set {key} expects a number of seconds"

            current = {**_fleet.settings().as_dict(), key: value}
            applied = _fleet.configure(current)
            save_config_section("fleet", applied.as_dict())
            self.cfg["fleet"] = applied.as_dict()

            shown = getattr(applied, key)
            note = ""
            if shown != value:
                note = f" (clamped from {value}: stale must stay under offline)"
            if key in _fleet.FleetSettings.RESTART_KEYS:
                note += " — takes effect on restart"
            return f"fleet {key} = {shown}{note}"

        return ("usage: fleet show | fleet set <key> <value> | "
                "fleet token show|rotate")

    async def _handle_remote_command(self, text: str) -> str:
        """Handle 'remote run|cancel|add|list' palette commands."""
        parts = text.split(maxsplit=2)
        if len(parts) < 2:
            return "usage: remote run <id> <prompt> | remote cancel <tid> | remote add <id> <host:port> | remote list"
        sub = parts[1]
        if sub == "list":
            if not self._remote_nodes:
                return "no remote nodes configured"
            return "\n".join(
                f"  {'●' if n.alive else '○'} {n.id:14s} {n.host}:{n.port}  "
                f"{'running' if n.running_count else 'idle'}"
                for n in self._remote_nodes.values()
            )
        if sub == "add" and len(parts) >= 3:
            rest = parts[2]
            rid, _, addr = rest.partition(" ")
            host, _, port_str = addr.partition(":")
            port = int(port_str) if port_str else 8765
            self._remote_nodes[rid] = RemoteNode(id=rid, host=host, port=port, label=rid)
            return f"remote '{rid}' added ({host}:{port})"
        if sub == "run" and len(parts) >= 3:
            rest = parts[2]
            node_id, _, prompt = rest.partition(" ")
            node = self._remote_nodes.get(node_id)
            if not node:
                return f"unknown remote '{node_id}' (try: remote list)"
            result = await self._remote_client.run_task(node, prompt)
            if result:
                return f"dispatched to {node_id}: {result.get('task_id', 'ok')}"
            return f"remote '{node_id}' unreachable"
        if sub == "cancel" and len(parts) >= 3:
            tid = parts[2]
            for node in self._remote_nodes.values():
                if node.alive:
                    result = await self._remote_client.cancel_task(node, tid)
                    if result:
                        return f"cancelled {tid} on {node.id}"
            return f"task '{tid}' not found on any alive remote"
        return f"unknown remote subcommand: {sub}"

    def on_unmount(self) -> None:
        """Withdraw our heartbeat so peers do not have to time us out.

        Best-effort only: discover_local() reaps meta.json by dead pid, so a
        SIGKILL that skips this is handled too.
        """
        try:
            self._heartbeat.clear()
        except OSError:
            pass

    # ── Fleet: this instance, its same-host siblings, and remote nodes ─────
    async def _start_remote_server(self) -> None:
        """Start the listener, and say so when the port is taken.

        This used to be a bare create_task: a second cockpit on the same box
        lost the bind race, the exception vanished into the task, and remote
        control was silently dead for the rest of the session.
        """
        try:
            await self._remote_server.start()
        except OSError as e:
            self.store.state.logs.append(
                f"fleet: port {self._remote_server.port} unavailable ({e.strerror}) — "
                f"remote control off. Set AION_INSTANCE to claim a different port."
            )

    def _running_count(self) -> int:
        return sum(1 for t in self.store.registry.tasks.values()
                   if t.state.value in ("running", "pending"))

    def _beat(self) -> None:
        """Publish our heartbeat so sibling instances can see us."""
        try:
            self._heartbeat.beat(
                active_harness=self.store.state.active_harness,
                running_count=self._running_count(),
            )
        except OSError:
            pass    # a missing ~/.aion is not worth killing the cockpit over

    def _refresh_local_peers(self) -> None:
        """Scan for sibling instances. On a timer — never inside a render."""
        from .. import fleet as _fleet
        try:
            self._local_peers = _fleet.discover_local()
        except OSError:
            self._local_peers = []
        for peer in self._local_peers:
            self._push_fleet_history(peer.id, peer.running_count)
        for node in self._remote_nodes.values():
            self._push_fleet_history(node.id, node.running_count)

    def _push_fleet_history(self, node_id: str, running: int) -> None:
        """Normalise task load to 0..1 and keep the last 16 samples."""
        hist = self._fleet_history.setdefault(node_id, [])
        hist.append(running / max(1.0, running + 1.0))
        del hist[:-16]

    def _net_panel(self, theme: dict) -> str:
        """Render the Fleet workspace — every aion instance, local and remote."""
        from .. import fleet as _fleet
        from .fleet_panel import FleetRow, render_fleet

        rows: list[FleetRow] = []
        for peer in self._local_peers:
            rows.append(FleetRow(
                id=peer.id,
                addr=f"{peer.host}:{peer.port}",
                health=_fleet.node_health(bool(peer.updated_at), peer.age_s(), local=True),
                local=True,
                is_self=peer.is_self,
                running=peer.running_count,
                harness=peer.active_harness,
                age_s=peer.age_s(),
                history=self._fleet_history.get(peer.id, []),
            ))
        for node in self._remote_nodes.values():
            rows.append(FleetRow(
                id=node.id,
                addr=f"{node.hostname or node.host}:{node.port}",
                health=_fleet.node_health(bool(node.last_seen), node.age_s(), local=False),
                local=False,
                running=node.running_count,
                harness=node.active_harness,
                age_s=node.age_s(),
                history=self._fleet_history.get(node.id, []),
                tasks=node.tasks,
            ))
        return render_fleet(rows, theme, tick=self._viz_tick,
                            listen_port=self._remote_server.port,
                            listen_host=self._remote_server.host)

    def _render_right(self) -> None:
        theme = self.cfg["theme"]
        right = self.query_one("#right", expect_type=VerticalScroll)
        a, ok_, wa, er, di = theme["accent"], theme["ok"], theme["warn"], theme["err"], theme["dim"]
        running = [t for t in self.store.registry.tasks.values()
                   if t.state.value in ("running", "pending")]
        lines: list[str] = []
        wf = self.store.state.workflows
        wf_live = [w for w in wf if w.get("stage") not in ("done",)] if wf else []
        # ---- Workflow Pulse (when live workflows exist) ----
        if wf_live:
            from ..workflows import workflow_pulse
            pulse = workflow_pulse(wf, theme, max_rows=3)
            lines.extend(pulse)
            if pulse:
                lines.append("")
        # ---- Decorative viz (when idle — spectrum/wave/radar rotation) ----
        if not wf_live:
            from .visualizers import spectrum_eq, task_wave, pulse_radar
            st = self.store.state.stats
            sys_ = st.get("system") or {}
            phase = (self._viz_tick // 10) % 3
            if phase == 0 and sys_.get("ok"):
                cpu = sys_["cpu"]["total_pct"] / 100.0
                mem = sys_["mem"]["pct"] / 100.0
                disk_pct = max((d["pct"] for d in sys_.get("disks", [])), default=0) / 100.0
                rcount = min(len(running) / 3.0, 1.0)
                vals = [cpu, mem, disk_pct, rcount]
                labels = ["CPU", "RAM", "DSK", "TASK"]
                viz = spectrum_eq(vals, self._viz_tick, height=4, labels=labels)
                lines.append(f"[{a}]◈ SPECTRUM[/]")
                for line in viz.split("\n"):
                    lines.append(f"  {line}")
            elif phase == 1:
                hist = self._task_wave_history[-24:] if self._task_wave_history else [0.0]
                viz = task_wave(hist, self._viz_tick, width=24, label="ACTIVITY")
                lines.append(f"[{a}]◈ WAVE[/]")
                for line in viz.split("\n"):
                    lines.append(f"  {line}")
            else:
                rcount = min(len(running) / 5.0, 1.0)
                sesh_count = len(self.store.state.stats.get("stats", {}).get("agents", []))
                rings = [
                    {"label": "TASKS", "value": rcount,
                     "items": [f"t{i}" for i in range(min(len(running), 12))]},
                    {"label": "AGENTS", "value": min(sesh_count / 5.0, 1.0),
                     "items": [f"s{i}" for i in range(min(sesh_count, 12))]},
                ]
                viz = pulse_radar(rings, self._viz_tick, size=12)
                lines.append(f"[{a}]◈ RADAR[/]")
                for line in viz.split("\n"):
                    lines.append(f"  {line}")
            lines.append("")
        # ---- LIVE TASKS (compact) ----
        if running or wf_live:
            lines.append(f"[{a}]TASKS[/][{wa}] ●{len(running)}{' ◆'+str(len(wf_live))+'wf' if wf_live else ''}[/]")
            for t in running:
                lines.append(f" [{wa}]●[/] [{di}]{t.label[:20]}[/] {bar(t.progress, width=10, color=wa)}")
        # ---- Telemetry one-liner ----
        tel = self.store.state.stats.get("telemetry")
        if tel:
            bits = []
            if "vram_bytes" in tel:
                bits.append(f"vram{tel['vram_bytes']//(1024*1024)}MB")
            if "loaded_models" in tel:
                bits.append(f"mod{tel['loaded_models']}")
            if "gpu_util_pct" in tel:
                bits.append(f"gpu{tel['gpu_util_pct']}%")
            if bits:
                lines.append(f" [{di}]{' '.join(bits)}[/]")
        # rebuild right rail only when line count changes (rare)
        if len(self._right) != len(lines):
            for c in self._right:
                c.remove()
            self._right = [Cell() for _ in lines]
            right.mount(*self._right)
        for cell, text in zip(self._right, lines):
            cell.update(text)

    def _render_bottom(self) -> None:
        theme = self.cfg["theme"]
        a, ok_, wa, er, di = theme["accent"], theme["ok"], theme["warn"], theme["err"], theme["dim"]
        running = any(t.state.value == "running"
                      for t in self.store.registry.tasks.values())
        cmd = self.store.state.last_command or ""
        tag = f" [{wa}]●{sum(1 for t in self.store.registry.tasks.values() if t.state.value=='running')}[/]" if running else ""
        self.query_one("#bottom", expect_type=Static).update(
            f"[{a}]▶[/] "
            f"[{di if not cmd else ok_}]{cmd[:40] or 'todo, swarm, compare, or just type it'}[/]{tag}"
            f"  [{di}]Ctrl-K[/]")

    WALKTHROUGH = [
        ("Welcome to aion", "This is your AI cockpit. Navigate with ↑↓ (or j/k), switch panels with ←→ (or h/l)."),
        ("Workspaces", "The left rail lists workspaces: Models, Tasks, Agent, Memory, Vault, System, Hermes, Skills, Projects, Term, Swarm. Press 1-9 or ←→ to move."),
        ("Run a harness", "Press Ctrl-K and type 'run demo hello' — a harness executes and shows live progress in the right rail."),
        ("Agent chat", "Go to the Agent workspace (✦) and type a message in Ctrl-K to talk to the inline LLM. 'compare <q>' shows two models side-by-side."),
        ("Voice & deck", "Press 'v' for offline voice (faster-whisper). If you have the CyclUno deck, it drives the cockpit one-handed."),
        ("Proactive Jarvis", "aion watches state and surfaces suggestions (⚠ in the header, ⚡ in the activity panel). You're ready — press Enter to start."),
    ]

    WIZARD_STEPS = [
        {"title": "Welcome to aion", "type": "info",
         "body": "aion is a split-screen HUD + application desktop — a mission-control\n"
                 "center for your terminal.\n\n"
                 "It can:\n"
                 "  • Monitor system health (CPU, RAM, disk)\n"
                 "  • Run AI tasks via LLMs\n"
                 "  • Manage kanban boards & notes\n"
                 "  • Control remote aion instances\n"
                 "  • Connect to physical hardware decks\n\n"
                 "The rest of this wizard covers what you need to get started.\n\n"
                 "Press Enter to continue."},
        {"title": "Workspaces", "type": "info",
         "body": "Nine workspaces, each for a different task:\n\n"
                 " 1 ⬡ Desktop    — Home hub + status overview\n"
                 " 2 ◈ Subsystems — Active AI harnesses\n"
                 " 3 ▤ Tasks      — Task list + kanban boards\n"
                 " 4 ✦ Agent      — AI chat + model comparison\n"
                 " 5 📓 Vault     — Notes & memory facts\n"
                 " 6 🖥 System    — Detailed health gauges\n"
                 " 7 ▣ Term       — Embedded terminal (btop)\n"
                 " 8 ⚙ Settings   — Provider keys & configuration\n"
                 " 9 🌐 Net       — Remote aion node control\n\n"
                 "Press 1-9 or ←→ to switch. Press Enter to continue."},
        {"title": "Dependencies", "type": "info",
         "body": "aion uses several external tools for AI inference.\n\n"
                 "The next steps will detect what's installed and offer to\n"
                 "install anything that's missing.\n\n"
                 "All tools are optional — skip any you don't need.\n\n"
                 "Press Enter to continue."},
        {"title": "OmniRoute", "type": "install",
         "binary": "omniroute",
         "label": "OmniRoute model router",
         "pkg": "@omniroute/cli",
         "body": "OmniRoute is a local AI gateway that routes your requests to\n"
                 "236+ providers through one endpoint.\n\n"
                 "It comes with free tiers — no API key needed to start.\n"},
        {"title": "FCM (free-coding-models)", "type": "install",
         "binary": "free-coding-models",
         "label": "FCM free models daemon",
         "pkg": "free-coding-models",
         "body": "FCM is a local daemon that finds & routes to free LLM models.\n\n"
                 "Runs on localhost:19280. aion uses it as the default backend\n"
                 "when cloud providers aren't configured.\n"},
        {"title": "OpenCode", "type": "install",
         "binary": "opencode",
         "label": "OpenCode coding agent",
         "pkg": "opencode-ai",
         "body": "OpenCode is an agentic CLI for software engineering tasks.\n\n"
                 "aion can delegate coding work to it via the opencode harness.\n"},
        {"title": "Hermes Agent", "type": "install",
         "binary": "hermes",
         "label": "Hermes Agent framework",
         "install_cmd": ["sh", "-c", "curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash"],
         "body": "Hermes is Nous Research's self-improving AI agent.\n\n"
                 "aion reads its memory, kanban boards, and can spawn it as\n"
                 "a subprocess for complex multi-step tasks.\n"},
        {"title": "Groq API key", "type": "env", "env_var": "GROQ_API_KEY",
         "body": "Groq provides fast inference on open-source models.\n"
                 "A free key is enough to start.\n\n"
                 "1. Go to https://console.groq.com/keys\n"
                 "2. Create a free API key\n"
                 "3. Paste it below (or press Enter to skip)\n\n"
                 "Without any cloud keys, aion falls back to FCM (local)."},
        {"title": "OpenRouter API key", "type": "env", "env_var": "OPENROUTER_API_KEY",
         "body": "OpenRouter gives access to 200+ models.\n\n"
                 "1. Go to https://openrouter.ai/keys\n"
                 "2. Create a key and copy it\n"
                 "3. Paste it below (or press Enter to skip)"},
        {"title": "All set", "type": "info",
         "body": "Setup is complete.\n\n"
                 "Quick tips:\n"
                 "  • Ctrl-K         — command palette (try 'todo buy milk')\n"
                 "  • ? or /         — help reference\n"
                 "  • w              — interactive tour\n"
                 "  • setup set <K> <V> — write any env key\n\n"
                 "Keys 1-9 switch workspaces. ↑↓/jk select items. Enter activates.\n\n"
                 "You can rerun this wizard anytime: Ctrl-K → 'setup wizard'\n\n"
                 "Press Enter to finish."},
    ]

    def action_act(self) -> None:
        """Act on the top Jarvis suggestion (Cycle 6 — actionable Jarvis).

        The top suggestion carries an optional `action` command string (e.g.
        'rerun', 'run demo hello', 'mem'). If present, emit it as an intent so
        the cockpit actually does something instead of just displaying it.
        """
        sugg = self.store.state.suggestions
        if not sugg:
            return
        top = sugg[0]
        if top.action:
            self.store.state.logs.append(f"▶ Jarvis: {top.action}")
            self.store.state.logs = self.store.state.logs[-50:]
            # running the action clears that suggestion so it won't repeat
            self.store.state.suggestions = sugg[1:]
            asyncio.ensure_future(self.router.emit(Intent.command(top.action)))

    def action_tour(self) -> None:
        """Launch the interactive walkthrough (talon_hud-style step-by-step)."""
        self._tour_active = True
        self._tour_step = 0
        h = self.query_one("#help")
        h.display = True
        self._tour_render()

    def _tour_render(self) -> None:
        theme = self.cfg["theme"]
        title, body = self.WALKTHROUGH[self._tour_step]
        n = len(self.WALKTHROUGH)
        h = self.query_one("#help")
        h.update(
            f"[{theme['accent']}]◆ TOUR {self._tour_step+1}/{n}: {title}[/]\n\n"
            f"[{theme['dim']}]{body}[/]\n\n"
            f"[{theme['dim']}]Enter: next · Esc: skip[/]"
        )

    def _tour_next(self) -> None:
        if not self._tour_active:
            return
        self._tour_step += 1
        if self._tour_step >= len(self.WALKTHROUGH):
            self._tour_close()
        else:
            self._tour_render()

    def _tour_close(self) -> None:
        self._tour_active = False
        self._tour_step = 0
        self.query_one("#help").display = False

    # ---- setup wizard ---------------------------------------------------

    def _start_wizard(self) -> None:
        """Launch the setup wizard with onboarding + install + env config."""
        env_path = Path.home() / ".env"
        existing = {}
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if "=" in line and not line.strip().startswith("#"):
                    k, _, v = line.partition("=")
                    existing[k.strip()] = v.strip()
        self._wizard_data = {"env": existing}
        self._wizard_install_status = {}
        self._wizard_install_output = ""
        self._wizard_active = True
        self._wizard_step = 0
        self._wizard_render()

    def _wizard_render(self) -> None:
        """Render the current wizard step."""
        a = self.cfg["theme"]["accent"]
        di, ok_, er, wa = (self.cfg["theme"][k] for k in ("dim", "ok", "err", "warn"))
        step = self.WIZARD_STEPS[self._wizard_step]
        t = step["type"]
        n = len(self.WIZARD_STEPS)
        h = self.query_one("#help")
        p = self.query_one("#palette")

        if t == "install":
            idx = self._wizard_step
            status = self._wizard_install_status.get(idx, "")
            found = shutil.which(step["binary"]) is not None

            if not status and found:
                self._wizard_install_status[idx] = "found"
                status = "found"
            elif not status:
                self._wizard_install_status[idx] = "missing"
                status = "missing"

            suffix = ""
            if status == "found":
                suffix = f"[{ok_}]✓ {step['label']} is ready[/]"
                footer = f"[{di}]Enter: next · Esc: cancel[/]"
            elif status == "missing":
                suffix = f"[{wa}]○ {step['label']} not detected[/]"
                if "install_cmd" in step:
                    cmd = "curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash"
                else:
                    cmd = f"npm install -g {step['pkg']}"
                suffix += f"\n[{di}]Install: {cmd}[/]"
                footer = f"[{di}]Enter: install · Esc: skip[/]"
            elif status == "installing":
                suffix = f"[{wa}]⟳ Installing {step['label']}...[/]"
                if self._wizard_install_output:
                    lines = self._wizard_install_output.strip().split("\n")[-6:]
                    suffix += "\n" + "\n".join(f"[{di}]  {l}[/]" for l in lines)
                footer = f"[{di}]Waiting...[/]"
            elif status == "failed":
                err = self._wizard_install_output.strip().split("\n")[-3:]
                suffix = f"[{er}]✗ Install failed[/]\n" + "\n".join(f"[{di}]  {l}[/]" for l in err)
                footer = f"[{di}]Enter: skip · Esc: cancel[/]"
            elif status == "skipped":
                suffix = f"[{di}]○ Skipped[/]"
                footer = f"[{di}]Enter: next · Esc: cancel[/]"

            body = step["body"] + "\n\n" + suffix
            p.display = False

        elif t == "env":
            var_name = step["env_var"]
            current = self._wizard_data.get("env", {}).get(var_name)
            if current:
                preview = current[:8] + "..." + current[-4:]
                body = step["body"] + f"\n\n[{di}]Current value: {preview}[/]"
            else:
                body = step["body"]
            footer = f"[{di}]Type value and press Enter · Esc: cancel[/]"
            placeholder = f"{var_name}=" + ("(set)" if current else "(Enter to skip)")
            p.placeholder = placeholder
            p.value = ""
            p.display = True
            p.focus()

        else:  # info
            body = step["body"]
            footer = f"[{di}]Enter: next · Esc: cancel[/]"
            p.display = False

        h.update(
            f"[{a}]◆ SETUP {self._wizard_step+1}/{n}: {step['title']}[/]\n\n"
            f"[{di}]{body}[/]\n\n"
            f"{footer}"
        )
        h.display = True

    def _wizard_next(self, raw: str) -> None:
        """Advance or trigger install for the current step."""
        if not self._wizard_active:
            return
        step = self.WIZARD_STEPS[self._wizard_step]

        if step["type"] == "install":
            idx = self._wizard_step
            status = self._wizard_install_status.get(idx, "")
            found = shutil.which(step["binary"]) is not None

            if found or status in ("found", "skipped", "failed"):
                self._wizard_step += 1
                if self._wizard_step >= len(self.WIZARD_STEPS):
                    self._wizard_finish()
                else:
                    self._wizard_render()
                return

            if status == "missing":
                self._wizard_install_status[idx] = "installing"
                self._wizard_install_output = ""
                self._wizard_render()
                asyncio.create_task(self._wizard_run_install(step, idx))
                return

            # installing: ignore Enter
            return

        if step["type"] == "env" and raw:
            self._wizard_data["env"][step["env_var"]] = raw

        self._wizard_step += 1
        if self._wizard_step >= len(self.WIZARD_STEPS):
            self._wizard_finish()
        else:
            self._wizard_render()

    async def _wizard_run_install(self, step: dict, idx: int) -> None:
        """Install a dependency asynchronously."""
        import subprocess
        cmd = step.get("install_cmd", ["npm", "install", "-g", step["pkg"]])
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await proc.communicate()
            output = out.decode("utf-8", errors="replace") if out else ""
            self._wizard_install_output = output
            if proc.returncode == 0 and shutil.which(step["binary"]):
                self._wizard_install_status[idx] = "found"
            else:
                self._wizard_install_status[idx] = "failed"
        except FileNotFoundError:
            self._wizard_install_output = f"'{cmd[0]}' not found — is npm/node installed?"
            self._wizard_install_status[idx] = "failed"
        except Exception as e:
            self._wizard_install_output = str(e)
            self._wizard_install_status[idx] = "failed"
        self._wizard_render()

    def _wizard_finish(self) -> None:
        """Write collected env vars to ~/.env and close."""
        collected = {k: v for k, v in self._wizard_data.get("env", {}).items() if v}
        if collected:
            env_path = Path.home() / ".env"
            lines = []
            seen = set()
            if env_path.exists():
                for line in env_path.read_text().splitlines():
                    stripped = line.strip()
                    if "=" in stripped and not stripped.startswith("#"):
                        key = stripped.split("=", 1)[0].strip()
                        if key in collected:
                            lines.append(f"{key}={collected[key]}")
                            seen.add(key)
                            continue
                    lines.append(line)
            for key, val in collected.items():
                if key not in seen:
                    lines.append(f"{key}={val}")
            env_path.write_text("\n".join(lines) + "\n")
        self._wizard_close()

    def _wizard_close(self) -> None:
        self._wizard_active = False
        self._wizard_step = 0
        self._wizard_data = {}
        self._wizard_install_status = {}
        self._wizard_install_output = ""
        self.query_one("#help").display = False
        self.query_one("#palette").display = False

    def _help_text(self, extended: bool = False) -> str:
        theme = self.cfg["theme"]
        ws_count = len(self.cfg["workspaces"])
        ws_keys = "/".join(str(i) for i in range(1, ws_count + 1))
        a, di, ok_, wa = theme["accent"], theme["dim"], theme["ok"], theme["warn"]

        if extended:
            return (
                f"[{a}]═ aion MANUAL ════════════════════════════════════════[/]\n"
                "\n"
                f"[{a}]WHAT IT IS[/]\n"
                f" [{di}]aion is an agentic OS cockpit — a split-screen HUD + application [/]\n"
                f" [{di}]desktop. It runs on your terminal and adapts to what you do.[/]\n"
                "\n"
                f"[{a}]WORKSPACES (keys 1-9)[/]\n"
                f" [{di}]1 ⬡ Desktop[/]   Home hub — status, launcher, context widgets\n"
                f" [{di}]2 ◈ Subsystems[/] Active harnesses filtered by context\n"
                f" [{di}]3 ▤ Tasks[/]      Running/finished tasks + kanban boards\n"
                f" [{di}]4 ✦ Agent[/]     Agents, swarm, chat (context picks mode)\n"
                f" [{di}]5 📓 Vault[/]    Note graph + memory facts\n"
                f" [{di}]6 🖥 System[/]   Detailed computer + health + physis gauges\n"
                f" [{di}]7 ▣ Term[/]     Embedded terminal (btop, shell, etc)\n"
                f" [{di}]8 ⚙ Settings[/] API providers + installed skills\n"
                f" [{di}]9 🌐 Net[/]      Remote aion nodes + live status\n"
                "\n"
                f"[{a}]CTRL-K COMMANDS[/]\n"
                f" [{di}]todo <t>[/]     add to-do item\n"
                f" [{di}]done <n>[/]     mark to-do #n done\n"
                f" [{di}]swarm <goal>[/] create a multi-agent swarm\n"
                f" [{di}]compare <q>[/]  side-by-side model comparison\n"
                f" [{di}]agent create <n>[/] create persistent agent entity\n"
                f" [{di}]agent list[/]   show all agents\n"
                f" [{di}]board create <t>[/] create kanban board\n"
                f" [{di}]board add <t>[/] add card to board\n"
                f" [{di}]setup wizard[/]   full interactive onboarding\n"
                f" [{di}]setup set <K> <V>[/] write env key to ~/.env\n"
                f" [{di}]setup <scopes>[/] profile scan (dev,writing,data...)\n"
                f" [{di}]goto <ws>[/]     jump to workspace\n"
                f" [{di}]help manual[/]  this manual\n"
                f" [{di}]tour[/]         walkthrough\n"
                f" [{di}]mode <name>[/]  switch mode (focus/deep/monitor/stealth/demo)\n"
                f" [{di}]note <text>[/]  quick memory note\n"
                f" [{di}]observe ai[/]   AI observer on terminal output\n"
                f" [{di}]search <q>[/]   search vault notes\n"
                f" [{di}]run <h> <t>[/]  run harness with label\n"
                "\n"
                f"[{a}]KEYS[/]\n"
                f"  {ws_keys}  switch workspace  ↑↓/jk select item\n"
                f"  Enter/Space activate   ←→/hl switch ws\n"
                f"  p pause  x cancel  r re-run  ? help\n"
                f"  v voice toggle   Ctrl-K command palette\n"
                "\n"
                f"[{a}]CONTEXT ADAPTATION[/]\n"
                f" [{di}]The desktop and subsystems workspaces adapt to your current [/]\n"
                f" [{di}]context (dev/system/agent/tasks/health) based on which [/]\n"
                f" [{di}]workspace is active and the last command you typed.[/]\n"
                "\n"
                f"[{a}]TIP: just type what you want in Ctrl-K[/]"
            )

        return (
            f"[{a}]aion — quick reference[/]\n"
            "\n"
            f"[{a}]Ctrl-K palette:[/] just type what you want\n"
            "  'todo buy milk' · 'agent create Alice' · 'swarm research'\n"
            "  'compare recursion' · 'setup wizard' · 'setup dev'\n"
            "\n"
            f"[{a}]Keys[/]  {ws_keys}:workspaces  ↑↓/jk:select  Enter:activate\n"
            f"  p pause  x cancel  r re-run  v voice  ? help\n"
            f"  Ctrl-K: command palette     manual: extended reference\n"
            "\n"
            f"[{a}]Workspaces[/]\n"
            "  1⬡ Desktop  2◈ Subsystems  3▤ Tasks  4✦ Agent\n"
            "  5📓 Vault   6🖥 System     7▣ Term   8⚙ Settings  9🌐 Net\n"
            "\n"
            f"[{a}]More:[/] type 'help manual' in Ctrl-K for full reference"
        )


def main() -> None:
    from .. import fleet as _fleet
    moved = _fleet.migrate_legacy()
    if moved:
        print(f"[aion] moved {len(moved)} file(s) into the shared/instance "
              f"layout: {', '.join(moved)}")
    AiOSApp().run()


if __name__ == "__main__":
    main()
