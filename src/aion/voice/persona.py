"""
persona.py — aion's character.

Maps events → spoken responses with personality.
Configurable verbosity, formality, name.
"""
from __future__ import annotations

import json
import random
import time
from pathlib import Path
from typing import Literal


EventType = Literal[
    "startup", "task_done", "task_failed", "task_cancelled",
    "task_paused", "task_resumed", "voice_toggle_on", "voice_toggle_off",
    "idle", "proactive_suggestion", "command_accepted", "mode_switch",
    "error",
]


class Persona:
    """Character engine for aion's voice and text responses."""

    def __init__(
        self,
        name: str = "aion",
        verbosity: Literal["terse", "normal", "chatty"] = "normal",
        formality: Literal["formal", "casual"] = "casual",
        path: str | Path | None = None,
    ) -> None:
        self.name = name
        self.verbosity = verbosity
        self.formality = formality
        self._path = Path(path or (Path.home() / ".aion" / "persona.json"))
        self._load()

    # --- response templates ---
    # keyed by (event_type, verbosity, formality) -> list of strings
    _TEMPLATES: dict[str, dict[str, list[str]]] = {
        "startup": {
            "terse_formal": ["{name} online. All systems nominal."],
            "terse_casual": ["Online. Ready to go."],
            "normal_formal": ["Good {time}. {name} online. All systems nominal."],
            "normal_casual": ["Good {time}. Ready when you are."],
            "chatty_formal": ["Good {time}. {name} is online. All systems checked and ready."],
            "chatty_casual": ["Hey! {name} here, all systems looking good. What's on your mind?"],
        },
        "task_done": {
            "terse_formal": ["Done."],
            "terse_casual": ["Done."],
            "normal_formal": ["Done, sir."],
            "normal_casual": ["All done."],
            "chatty_formal": ["Task complete. Anything else I can help with?"],
            "chatty_casual": ["Finished! What's next?"],
        },
        "task_failed": {
            "terse_formal": ["Task failed."],
            "terse_casual": ["Failed. Trying another way."],
            "normal_formal": ["I encountered a problem. Let me try a different approach."],
            "normal_casual": ["Hit a snag. Let me try another route."],
            "chatty_formal": ["The task did not complete as expected. I will attempt an alternative strategy."],
            "chatty_casual": ["That didn't work out. Let me pivot and try something else."],
        },
        "task_cancelled": {
            "terse_formal": ["Cancelled."],
            "terse_casual": ["Cancelled."],
            "normal_formal": ["Cancelled as requested."],
            "normal_casual": ["Stopped."],
            "chatty_formal": ["The task has been cancelled as you requested."],
            "chatty_casual": ["Cancelled, no problem."],
        },
        "task_paused": {
            "terse_formal": ["Paused."],
            "terse_casual": ["Paused."],
            "normal_formal": ["Paused. Press resume when ready."],
            "normal_casual": ["On hold. Say the word to resume."],
            "chatty_formal": ["I have paused the task. It will resume when you are ready."],
            "chatty_casual": ["Paused and waiting for your signal."],
        },
        "task_resumed": {
            "terse_formal": ["Resumed."],
            "terse_casual": ["Back at it."],
            "normal_formal": ["Resuming."],
            "normal_casual": ["Continuing where we left off."],
            "chatty_formal": ["Resuming the task now."],
            "chatty_casual": ["Picking up where we left off!"],
        },
        "voice_toggle_on": {
            "terse_formal": ["Voice on."],
            "terse_casual": ["Listening."],
            "normal_formal": ["Voice control activated."],
            "normal_casual": ["I'm listening."],
            "chatty_formal": ["Voice control is now active. I am listening for your command."],
            "chatty_casual": ["Listening! Go ahead."],
        },
        "voice_toggle_off": {
            "terse_formal": ["Voice off."],
            "terse_casual": ["Stopped listening."],
            "normal_formal": ["Voice control deactivated."],
            "normal_casual": ["Voice off."],
            "chatty_formal": ["Voice control is now off. You can reactivate with the voice key."],
            "chatty_casual": ["Voice off. Use the key to toggle back on."],
        },
        "idle": {
            "terse_formal": ["Standing by."],
            "terse_casual": ["Still here."],
            "normal_formal": ["Standing by, sir."],
            "normal_casual": ["Waiting for your command."],
            "chatty_formal": ["I am here when you need me, sir."],
            "chatty_casual": ["Just hanging out. Say when."],
        },
        "command_accepted": {
            "terse_formal": ["On it."],
            "terse_casual": ["On it."],
            "normal_formal": ["Right away."],
            "normal_casual": ["On it."],
            "chatty_formal": ["Processing your request now."],
            "chatty_casual": ["You got it. Working on it now."],
        },
        "mode_switch": {
            "terse_formal": ["Switched to {mode}."],
            "terse_casual": ["{mode} mode."],
            "normal_formal": ["Switching to {mode} mode."],
            "normal_casual": ["Entering {mode} mode."],
            "chatty_formal": ["Switching the workspace to {mode}."],
            "chatty_casual": ["Headed to {mode}."],
        },
        "error": {
            "terse_formal": ["Error: {detail}"],
            "terse_casual": ["Oops: {detail}"],
            "normal_formal": ["An error occurred: {detail}"],
            "normal_casual": ["Something went wrong: {detail}"],
            "chatty_formal": ["I apologise, but I encountered an error: {detail}"],
            "chatty_casual": ["Uh oh, ran into a problem: {detail}"],
        },
        "proactive_suggestion": {
            "terse_formal": ["{suggestion}"],
            "terse_casual": ["{suggestion}"],
            "normal_formal": ["Sir, {suggestion}"],
            "normal_casual": ["Hey, {suggestion}"],
            "chatty_formal": ["I noticed something: {suggestion}"],
            "chatty_casual": ["Quick thought: {suggestion}"],
        },
    }

    def greeting(self) -> str:
        hour = time.localtime().tm_hour
        tod = "morning" if hour < 12 else "afternoon" if hour < 18 else "evening"
        return self.respond("startup", time=tod)

    def respond(self, event: EventType, **kwargs) -> str:
        key = f"{self.verbosity}_{self.formality}"
        templates = self._TEMPLATES.get(event, {}).get(key)
        if not templates:
            templates = self._TEMPLATES.get(event, {}).get("normal_casual", ["{name}: {event}"])
        text = random.choice(templates)
        kwargs.setdefault("name", self.name)
        try:
            return text.format(**kwargs)
        except KeyError:
            return f"{self.name}: {event}"

    # --- persistence ---
    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
            self.verbosity = data.get("verbosity", self.verbosity)
            self.formality = data.get("formality", self.formality)
            self.name = data.get("name", self.name)
        except Exception:
            pass

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps({
            "name": self.name,
            "verbosity": self.verbosity,
            "formality": self.formality,
        }, indent=2))
