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

from ..core import Bus, Intent, IntentType, TOPIC_INTENT, load_config
from ..harnesses import build_harnesses, TelemetryHarness, TIER_CHEAP, TIER_STANDARD, TIER_PREMIUM
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
        # telemetry pollers
        for h in self.harnesses.values():
            if isinstance(h, TelemetryHarness):
                asyncio.create_task(h.start())
        self._render_all()
        self.router.register(JoystickInput())
        asyncio.create_task(self.router.start_all())
        self.title = self.cfg["app_name"]
        self.sub_title = "multi-harness · stats visualizer"
        # route bus -> store (store is the brain, app just re-renders)
        self.bus.subscribe(TOPIC_INTENT, self._on_intent)

    def _tick(self) -> None:
        self._render_header()
        self._push_deck_hud()

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
        # let the store own all state mutation
        self.store.handle(intent)
        self._render_all()

    def on_key(self, event: events.Key) -> None:
        if self.query_one("#palette").display:
            if event.key == "escape":
                self.query_one("#palette").display = False
                self.set_focus(None)
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

    async def on_submit(self, event: Input.Submitted) -> None:
        if event.input.id == "palette":
            text = event.value.strip()
            self.query_one("#palette").display = False
            if text:
                await self.router.emit(Intent.command(text))

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
        self.query_one("#header", expect_type=Header).text = (
            f"[{theme['accent']}]{self.persona.name}[/]  "
            f"harness: [{theme['ok']}]{name}[/]  "
            f"{status}  [{theme['dim']}]{clock}{vmode}[/]"
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
        # agent log
        tail = "\n".join(self.store.state.logs[-40:]) or "[agent] no output yet — run a harness"
        return f"[{theme['dim']}]{tail}[/]"

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
        return (f"[{theme['accent']}]aion — shortcuts[/]\n"
                "1/2/3          switch workspace (Models/Tasks/Agent)\n"
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
                "deck: joy2=navigate · MODE=gamepad")


def main() -> None:
    AiOSApp().run()


if __name__ == "__main__":
    main()
