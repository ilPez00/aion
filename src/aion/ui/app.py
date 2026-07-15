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
from typing import Any

from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Header, Static, Input, Label, Footer
from textual import events

from ..core import (
    Bus, Intent, IntentType, TOPIC_INTENT, TOPIC_VOICE, TOPIC_HERMES, TOPIC_SKILL, TOPIC_SETTINGS, load_config,
)
from ..harnesses import build_harnesses, TelemetryHarness, StatsHarness, ProjectsHarness, SystemHarness, HealthHarness, VaultHarness, TIER_CHEAP, TIER_STANDARD, TIER_PREMIUM, HarnessConfig
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
    DEFAULT_CSS = "Cell { height: auto; }"


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
    # map Textual key names to terminal byte sequences
    table = {
        "enter": b"\r", "escape": b"\x1b", "tab": b"\t",
        "up": b"\x1b[A", "down": b"\x1b[B", "right": b"\x1b[C", "left": b"\x1b[D",
        "backspace": b"\x7f", "space": b" ", "ctrl+c": b"\x03", "ctrl+d": b"\x04",
        "pageup": b"\x1b[5~", "pagedown": b"\x1b[6~",
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
        ("escape", "back", "Back"),
        ("space", "activate", "Activate"),
        ("p", "pause", "Pause/Resume"),
        ("x", "cancel", "Cancel"),
        ("r", "rerun", "Re-run"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.cfg = load_config()
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
        self.voice = VoiceInput(model_size="tiny")
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
        self._greeted = False
        self._term_pane = None          # mounted TermPane widget (lazy)
        self._term_active = False       # whether the term workspace is active

    # ----- compose: STABLE tree (built once) ----------------------------
    def compose(self) -> ComposeResult:
        yield Header(id="header")
        with Horizontal():
            yield VerticalScroll(id="rail")
            yield VerticalScroll(id="center")
            yield VerticalScroll(id="right")
        yield Static("", id="bottom")
        yield Input(placeholder="› command  (run <h> <prompt> · tier <cheap|standard|premium>)", id="palette")
        yield Static("", id="help")

    async def on_mount(self) -> None:
        self.query_one("#palette").display = False
        self.query_one("#help").display = False
        self.set_interval(1.0, self._tick)
        # all background HUD pollers (Jarvis HUD + Iron Man panels)
        for h in self.harnesses.values():
            if isinstance(h, (TelemetryHarness, StatsHarness, ProjectsHarness,
                              SystemHarness, HealthHarness, VaultHarness)):
                asyncio.create_task(h.start())
        self._render_all()
        self.router.register(JoystickInput())
        asyncio.create_task(self.router.start_all())
        self.title = self.cfg["app_name"]
        self.sub_title = f"multi-harness · stats visualizer · mode: {self.store.state.active_mode}"
        # route bus -> store (store is the brain, app just re-renders)
        self.bus.subscribe(TOPIC_INTENT, self._on_intent)
        self.bus.subscribe(TOPIC_VOICE, self._on_voice)
        self.bus.subscribe(TOPIC_HERMES, self._on_hermes_event)
        self.bus.subscribe(TOPIC_SKILL, self._on_skill_event)
        # Proactive Jarvis-style greeting on first boot
        greeting = self.persona.greeting()
        self.query_one("#bottom", expect_type=Static).update(
            f"[{self.cfg['theme']['accent']}]{greeting}[/]")
        self._greeted = True

    def _tick(self) -> None:
        self._render_header()
        self._render_right()   # live HUD (tokens/agents) refresh without input
        # projects + vault + sys workspaces poll on a timer, not on input
        wid = self.cfg["workspaces"][self.store.state.active_ws]["id"]
        if wid in ("projects", "vault", "sys"):
            self._render_center()
        self._sync_term_pane()
        self._push_deck_hud()

    def _sync_term_pane(self) -> None:
        """Lazily mount/unmount the embedded terminal when entering/leaving
        the Term workspace. Considers last-render so we only act on change."""
        ws_id = self.cfg["workspaces"][self.store.state.active_ws]["id"]
        want = (ws_id == "term")
        if want == self._term_active:
            return
        self._term_active = want
        center = self.query_one("#center", expect_type=VerticalScroll)
        if want:
            # clear the normal cell list, mount the live pane
            for c in self._center:
                c.remove()
            self._center = []
            term_h = self.harnesses.get("term")
            if term_h is None:
                term_h = TermHarness(
                    HarnessConfig.from_dict({"id": "term", "type": "term",
                                             "command": "btop"}),
                    self.bus, self.store.registry)
            term_h.ensure_running()
            self._term_pane = TermPane(term_h, id="termpane")
            center.mount(self._term_pane)
        else:
            # tear down: kill pty, restore empty cell list (rebuilt on next render)
            if self._term_pane is not None:
                term_h = self._term_pane._h
                term_h.stop()
                self._term_pane.remove()
                self._term_pane = None
            self._center = []

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
        # embedded terminal passthrough (Term workspace)
        if self._term_active and self._term_pane is not None:
            if event.key == "ctrl+t":
                # reserved: leave the terminal pane back to normal navigation
                return
            self._term_pane._h.send(
                event.character.encode("utf-8", "replace")
                if event.character else _key_bytes(event.key))
            event.prevent_default()
            return
        if self.query_one("#palette").display:
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
                    asyncio.create_task(self.router.emit(Intent.command(text)))
                event.prevent_default()
                return
            # All other keys go through to Input widget
            return
        if self.query_one("#help").display:
            if event.key == "escape" or event.key in ("?", "/"):
                self.query_one("#help").display = False
            return
        if event.key == "ctrl+k":
            self._toggle_palette()
            return
        if event.key == "v":
            asyncio.create_task(self.voice.toggle())
            return
        # map key -> Intent via keymap, else let built-in bindings handle it
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
            h.update(self._help_text())
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
        # Jarvis HUD: real token burn + spend + live agents from Hermes state.db
        hud = ""
        st = self.store.state.stats.get("stats")
        if st and st.get("ok"):
            from ..stats import human_tokens
            tot = st.get("in", 0) + st.get("out", 0) + st.get("reasoning", 0)
            cost = st.get("cost_usd", 0.0)
            live = st.get("live", 0)
            costs = f" ${cost:.2f}" if cost else ""
            hud = (f"  [{theme['accent']}]Σ{human_tokens(tot)}[/]"
                   f"[{theme['dim']}]/{st.get('window','today')}[/]"
                   f"[{theme['ok']}]{costs}[/]"
                   f"  [{theme['warn']}]◆{live} live[/]")
        self.query_one("#header", expect_type=Header).text = (
            f"[{theme['accent']}]{self.persona.name}[/]  "
            f"harness: [{theme['ok']}]{name}[/]  "
            f"{status}{hud}  [{theme['dim']}]{clock}{vmode}[/]"
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

    def _center_line(self, ws: str, it: dict, focused: bool, theme: dict) -> str:
        f = "▌" if focused else " "
        col = theme["accent"] if focused else theme["dim"]
        if ws == "models":
            mark = "●" if it["id"] == self.store.state.active_harness else " "
            return (f"[{col}]{f}{mark} {it['name']}[/]  "
                    f"[{theme['dim']}]tier:{it['tier']} vram:{it['vram']}MB run:{it['running']}[/]")
        if ws == "tasks":
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
        if ws == "memory":
            q = self.store.memory.query
            head = f" [filter: {q}]" if q else ""
            return (f"[{col}]{f}#{it['n']} {it['text']}[/]  "
                    f"[{theme['dim']}]{it['when']}{head}[/]")
        if ws == "hermes":
            status = it.get("status", "?")
            scol = {"done": theme["ok"], "ready": theme["warn"],
                    "blocked": theme["err"]}.get(status, theme["dim"])
            emoji = {"done": "✓", "ready": "▶", "blocked": "⊘",
                     "in_progress": "●"}.get(status, "○")
            return (f"[{col}]{f}{emoji} {it['title'][:50]}[/]\n"
                    f"  [{scol}]{status}[/] "
                    f"[{theme['dim']}]assignee: {it.get('assignee','-')}[/]")
        if ws == "skills":
            desc = it.get("description", "")[:60]
            return (f"[{col}]{f}{it.get('name','?')}[/]  "
                    f"[{theme['dim']}]{desc}[/]")
        if ws == "projects":
            return self._project_card(it, focused, theme)
        if ws == "settings":
            ep = it.get("endpoint", "")
            key = it.get("key_preview", "")
            return (f"[{col}]{f}{it['id']}[/]\n"
                    f"  [{theme['dim']}]{ep}[/]\n"
                    f"  [{theme['ok'] if key else theme['err']}]{key}[/]")
        if ws == "vault":
            return self._vault_line(it, focused, theme)
        if ws == "sys":
            return self._sys_panel(theme)
        if ws == "swarm":
            return self._swarm_panel(theme)
        if ws == "desktop":
            return self._desktop_panel(theme)
        if ws == "tasks":
            return self._tasks_panel(theme)
        if ws == "agent":
            return self._agent_panel(theme)
        # agent log
        tail = "\n".join(self.store.state.logs[-40:]) or "[agent] no output yet — run a harness"
        return f"[{theme['dim']}]{tail}[/]"

    def _project_card(self, it: dict, focused: bool, theme: dict) -> str:
        f = "▌" if focused else " "
        col = theme["accent"] if focused else theme["dim"]
        name = it.get("name", "?")
        if not it.get("exists"):
            return f"[{col}]{f}{name}[/]  [{theme['err']}]missing[/]"
        if not it.get("is_git"):
            return f"[{col}]{f}{name}[/]  [{theme['warn']}]{it.get('error','not git')}[/]"
        branch = it.get("branch", "?")
        dirty = it.get("dirty", 0)
        ahead, behind = it.get("ahead", 0), it.get("behind", 0)
        # status glyphs: clean vs dirty, ahead/behind arrows
        dcol = theme["warn"] if dirty else theme["ok"]
        dtxt = f"±{dirty}" if dirty else "clean"
        sync = ""
        if ahead:
            sync += f" [{theme['accent']}]↑{ahead}[/]"
        if behind:
            sync += f" [{theme['err']}]↓{behind}[/]"
        prs = it.get("open_prs")
        prtxt = f" [{theme['accent']}]PR:{prs}[/]" if prs else ""
        # session activity
        act = ""
        if it.get("sessions_today"):
            from ..stats import human_tokens
            act = (f" [{theme['dim']}]· {it['sessions_today']} sess "
                   f"{human_tokens(it.get('tokens_today',0))} tok today[/]")
        elif it.get("last_session_age_s") is not None:
            mins = int(it["last_session_age_s"] // 60)
            act = f" [{theme['dim']}]· last active {mins}′ ago[/]"
        # last commit + age
        lc = it.get("last_commit", "")
        cage = ""
        if it.get("last_commit_age_s") is not None:
            h = it["last_commit_age_s"] / 3600
            cage = f"{int(h)}h" if h < 48 else f"{int(h/24)}d"
        return (f"[{col}]{f}⬢ {name}[/] [{theme['dim']}]{branch}[/] "
                f"[{dcol}]{dtxt}[/]{sync}{prtxt}{act}\n"
                f"  [{theme['dim']}]{lc[:56]}[/] "
                f"[{theme['dim']}]{cage}[/]")

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
        st = self.store.state.stats
        blocks: list[str] = []

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

        return "\n\n".join(blocks)

    def _swarm_panel(self, theme: dict) -> str:
        """Render the multi-agent swarm dashboard."""
        from .gauges import hbar
        items = self.store._current_items()
        if not items or items[0].get("type") == "empty":
            return (f"[{theme['dim']}]No active swarm.[/]\n"
                    f"  [{theme['accent']}]swarm create <goal>[/] to start.\n"
                    f"  [{theme['dim']}]e.g. swarm create research and prototype a dashboard[/]")
        data = items[0].get("data", {})
        lines = []
        lines.append(f"[{theme['accent']}]╔══ SWARM ORCHESTRATOR ═══════════════════╗[/]")
        s = data
        counts = f"🧠 {s.get('working',0)}W · {s.get('waiting',0)}⏳ · {s.get('done',0)}✓ · {s.get('failed',0)}✗ · {s.get('blocked',0)}⊘"
        lines.append(f"  {counts}")
        lines.append(f"[{theme['dim']}]╠══ Agents ═══════════════════════════════╣[/]")
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
            lines.append(f"[{theme['dim']}]╠══ Plan ════════════════════════════════╣[/]")
            lines.append(f"  [{theme['accent']}]goal:[/] {plan.get('goal','')[:50]}")
            lines.append(f"  [{theme['dim']}]steps: {plan.get('steps',0)} · done: {plan.get('done',0)}[/]")
        lines.append(f"[{theme['accent']}]╚══════════════════════════════════════╝[/]")
        lines.append(f"[{theme['dim']}]Commands: swarm create|add <name> <goal>|run|status|stop[/]")
        return "\n".join(lines)

    def _agent_panel(self, theme: dict) -> str:
        """Render the inline LLM chat conversation."""
        items = self.store._current_items()
        a, di = theme["accent"], theme["dim"]
        if not items or not items[0].get("messages"):
            return f"[{di}]Agent workspace ready. Type a message in Ctrl-K or select a harness.[/]"
        msgs = items[0]["messages"]
        lines = [f"[{a}]╔══ AGENT CHAT ═══════════════════════╗[/]"]
        for msg in msgs[-20:]:
            role = msg.get("role", "?")
            content = msg.get("content", "")
            col = a if role == "assistant" else theme["ok"] if role == "user" else di
            name = "You" if role == "user" else "AI" if role == "assistant" else role
            lines.append(f" [{col}]{name}:[/]")
            while content:
                lines.append(f" [{di}]{content[:48]}[/]")
                content = content[48:]
        lines.append(f"[{a}]╚══════════════════════════════════╝[/]")
        lines.append(f"[{di}]Type 'search <q>' or a message in Ctrl-K to chat[/]")
        return "\n".join(lines)

    def _tasks_panel(self, theme: dict) -> str:
        """Full task-progress dashboard: active + history."""
        from .gauges import hbar
        tasks = sorted(self.store.registry.tasks.values(),
                       key=lambda t: t.created, reverse=True)
        lines = []
        lines.append(f"[{theme['accent']}]╔══ TASK PROGRESS ════════════════════════╗[/]")
        # Summary
        total = len(tasks)
        running = [t for t in tasks if t.state.value in ("running", "pending")]
        done = [t for t in tasks if t.state.value == "done"]
        failed = [t for t in tasks if t.state.value == "failed"]
        summ = f"▣ {total} total · ●{len(running)} active · ✓{len(done)} done · ✗{len(failed)} failed"
        lines.append(f"  [{theme['dim']}]{summ}[/]")
        # Active tasks (sort by progress desc)
        if running:
            lines.append(f"[{theme['accent']}]╠══ Active ═══════════════════════════════╣[/]")
            for t in sorted(running, key=lambda x: x.progress, reverse=True):
                icon = "⏸" if t.paused else "●"
                col = theme["warn"]
                bar_str = hbar(t.progress, width=12, color=col)
                lines.append(f"  [{col}]{icon}[/] [{theme['accent']}]{t.label[:22]:22s}[/]")
                lines.append(f"     {bar_str} [{theme['dim']}]{int(t.progress*100)}% · {t.harness}[/]")
        else:
            lines.append(f"[{theme['dim']}]╠══ Active ═══════════════════════════════╣[/]")
            lines.append(f"  [{theme['dim']}](no active tasks)[/]")
        # Recent history
        hist = self.store.state.task_history[-8:][::-1]
        if hist:
            lines.append(f"[{theme['accent']}]╠══ History ══════════════════════════════╣[/]")
            for h in hist:
                icon = "✓" if h["result"] == "done" else "✗" if h["result"] == "failed" else "—"
                col = theme["ok"] if h["result"] == "done" else theme["err"] if h["result"] == "failed" else theme["dim"]
                lines.append(f"  [{col}]{icon}[/] [{theme['dim']}]{h['label'][:32]:32s}[/] [{theme['dim']}]{h['harness']}[/]")
        lines.append(f"[{theme['accent']}]╚══════════════════════════════════════╝[/]")
        lines.append(f"[{theme['dim']}]Commands: run <h> <prompt> · tier <cheap|standard|premium>[/]")
        return "\n".join(lines)

    def _desktop_panel(self, theme: dict) -> str:
        """Agentic OS desktop — compact (~50 cols), 4 sections, no wrap."""
        from .gauges import hbar
        items = self.store._current_items()
        data = items[0].get("data", {}) if items else {}
        a, ok_c, wa, er, di = theme["accent"], theme["ok"], theme["warn"], theme["err"], theme["dim"]
        p = []

        # ─── STATUS ────────────────────────────────────────────────────────
        cpu = data.get("cpu_pct", 0); ram = data.get("ram_pct", 0)
        disk = data.get("disk_pct", 0)
        nd = (data.get("net_down") or "0 B")[:5]
        nu = (data.get("net_up") or "0 B")[:5]
        tr = data.get("tasks_running", 0); td = data.get("tasks_done", 0)
        tf = data.get("tasks_failed", 0)
        cc = er if cpu > 80 else wa if cpu > 50 else ok_c
        rc = er if ram > 80 else wa if ram > 50 else ok_c
        dc = er if disk > 80 else wa if disk > 50 else ok_c
        p.append(f"[{a}]─ STATUS ─[/]")
        p.append(f" [{di}]CPU[/][{cc}]{cpu:2.0f}%[/] [{di}]RAM[/][{rc}]{ram:2.0f}%[/] [{di}]DSK[/][{dc}]{disk:2.0f}%[/] [{di}]▼[/]{nd} [{di}]▲[/]{nu} [{ok_c}]●{tr}[/] [{ok_c}]✓{td}[/] [{er}]✗{tf}[/]")

        # ─── SYSTEM ────────────────────────────────────────────────────────
        p.append(f"[{a}]─ SYSTEM ─[/]")
        ru = data.get("ram_used_gb", 0); rt = data.get("ram_total_gb", 16)
        p.append(f" [{di}]CPU[/] {data.get('cpu_pct',0):.0f}%  [{di}]RAM[/] {hbar(ram/100,8,rc)} {ru:.0f}/{rt:.0f}")
        p.append(f" [{di}]DSK[/] {hbar(disk/100,8,dc)}  [{di}]NET[/] ▼{nd} ▲{nu}")
        vn = data.get("vault_notes", 0); mn = data.get("mem_count", 0)
        xs = []
        if vn: xs.append(f"[{di}]📓{vn}[/]")
        if mn: xs.append(f"[{di}]◎{mn}[/]")
        if xs: p.append(f" {' '.join(xs)}")

        # ─── TASKS ─────────────────────────────────────────────────────────
        p.append(f"[{a}]─ TASKS ─[/]")
        active = data.get("active_tasks", [])
        if active:
            for t in active[:4]:
                tc = ok_c if t["state"] == "done" else wa
                ic = "⏸" if t.get("paused") else "●" if t["state"] == "running" else "◆"
                pt = int(t["progress"] * 100)
                p.append(f" [{tc}]{ic}[/] [{di}]{t['label'][:20]}[/] {hbar(t['progress'],6,tc)} {pt}%")
        else:
            p.append(f" [{di}]idle — run demo hello or swarm create[/]")
        hist = data.get("task_history", [])
        if hist:
            for h in hist[:2]:
                ic = "✓" if h["result"] == "done" else "✗"
                cl = ok_c if h["result"] == "done" else er
                p.append(f" [{cl}]{ic}[/] [{di}]{h['label'][:28]}[/]")

        # ─── AGENTS ────────────────────────────────────────────────────────
        p.append(f"[{a}]─ AGENTS ─[/]")
        m = data.get("token_models", [])
        if m:
            for x in m[:2]:
                nm = x["model"].split("/")[-1][:12]
                t = x.get("tot", 0)
                p.append(f" [{di}]{nm}[/] {hbar(min(t/1e5,1),6,ok_c)} {t/1000:.0f}k")
        sw = data.get("swarm_agents", [])
        if sw:
            for x in sw[:2]:
                ic = {"idle":"○","working":"●","done":"✓","failed":"✗","blocked":"⊘"}.get(x.get("status","idle"),"?")
                cl = ok_c if x["status"]=="done" else wa if x["status"]=="working" else di
                p.append(f" [{cl}]{ic}[/] [{di}]{x['name'][:12]} {x.get('goal','')[:16]}[/]")
        if not m and not sw:
            p.append(f" [{di}]no data yet — stats harness populates[/]")

        # ─── QUICK ─────────────────────────────────────────────────────────
        p.append(f"[{a}]─ QUICK ─[/]")
        p.append(f" [{di}]Ctrl-K: run demo|swarm|mode|theme|note|mem|tier[/]")
        return "\n".join(p)

    def _render_right(self) -> None:
        theme = self.cfg["theme"]
        right = self.query_one("#right", expect_type=VerticalScroll)
        running = [t for t in self.store.registry.tasks.values()
                   if t.state.value in ("running", "pending")]
        lines = [f"[{theme['accent']}]LIVE TASKS[/]"]
        if not running:
            lines.append(f"[{theme['dim']}](idle)[/]")
        for t in running:
            lines.append(f"[{theme['accent']}]{t.id}[/] {t.label[:18]}")
            lines.append(f"  [{theme['warn']}]{t.state.value}{' ⏸' if t.paused else ''}[/] {bar(t.progress)}")
        tel = self.store.state.stats.get("telemetry")
        if tel:
            bits = []
            if "vram_bytes" in tel:
                bits.append(f"vram {tel['vram_bytes']//(1024*1024)}MB")
            if "loaded_models" in tel:
                bits.append(f"models {tel['loaded_models']}")
            if "gpu_util_pct" in tel:
                bits.append(f"gpu {tel['gpu_util_pct']}%")
            if "gpu_mem_mb" in tel:
                bits.append(f"gmem {tel['gpu_mem_mb']}MB")
            if bits:
                lines.append(f"[{theme['dim']}]{' · '.join(bits)}[/]")
        # ---- Jarvis HUD: per-model token burn + live agent census --------
        st = self.store.state.stats.get("stats")
        if st and st.get("ok"):
            from ..stats import human_tokens
            models = st.get("models", [])
            if models:
                lines.append("")
                lines.append(f"[{theme['accent']}]TOKENS · {st.get('window','today')}[/]")
                top = models[:5]
                maxtot = max((m["tot"] for m in top), default=1) or 1
                for m in top:
                    nm = m["model"].split("/")[-1][:16]
                    meter = bar(m["tot"] / maxtot, width=10, color=theme["accent"])
                    lines.append(f"[{theme['dim']}]{nm}[/]")
                    lines.append(f"  {meter} [{theme['dim']}]{human_tokens(m['tot'])}[/]")
            agents = st.get("agents", [])
            lines.append("")
            lines.append(f"[{theme['accent']}]LIVE AGENTS[/] [{theme['warn']}]◆{st.get('live',0)}[/]")
            if not agents:
                lines.append(f"[{theme['dim']}](none active)[/]")
            for a in agents[:6]:
                where = a.get("branch") or a.get("repo") or a["model"].split("/")[-1][:12]
                mins = a.get("age_s", 0) // 60
                lines.append(f"[{theme['ok']}]●[/] [{theme['dim']}]{where[:16]} "
                             f"{a.get('msgs',0)}m {mins}′[/]")
        elif st is not None and not st.get("ok"):
            lines.append("")
            lines.append(f"[{theme['dim']}](state.db unavailable)[/]")
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
        if not self._greeted:
            self._greeted = True
            greeting = self.persona.greeting()
            self.query_one("#bottom", expect_type=Static).update(
                f"[{theme['accent']}]{greeting}[/]"
            )
            return
        running = any(t.state.value == "running"
                      for t in self.store.registry.tasks.values())
        hint = "tasks in progress" if running else "awaiting your command"
        h = self.store.state.history[-1] if self.store.state.history else hint
        self.query_one("#bottom", expect_type=Static).update(f"[{theme['dim']}]{h}[/]")

    def _help_text(self) -> str:
        theme = self.cfg["theme"]
        ws_count = len(self.cfg["workspaces"])
        ws_keys = "/".join(str(i) for i in range(1, ws_count + 1))
        return (f"[{theme['accent']}]aion — shortcuts[/]\n"
                f"{ws_keys}         switch workspace\n"
                "↑↓ j/k         move selection\n"
                "←→ h/l         switch workspace\n"
                "Enter / Space  run harness / pause-resume task\n"
                "p              pause or resume focused task\n"
                "x              cancel focused task\n"
                "r              re-run interrupted/cancelled task\n"
                "Ctrl-K         command palette (optional)\n"
                "v              toggle offline voice control\n"
                "? / /          this help\n"
                "joystick: axis=navigate A=activate B=back C=context\n"
                "voice: 'go to models' · 'run demo hello' · 'stop'\n"
                "memory: 'note <fact>' · 'mem <query>' · 'forget <n>'\n"
                "vault: Obsidian-style notes graph (wikilinks + backlinks)\n"
                "system: Iron Man HUD — CPU/RAM/disk/net/GPU + real-life stats\n"
                "hermes: 'kanban' · 'mem' · 'gateway'\n"
                "skills: 'skill <name> <prompt>'\n"
                "deck: joy2=navigate · MODE=gamepad")


def main() -> None:
    AiOSApp().run()


if __name__ == "__main__":
    main()
