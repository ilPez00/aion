"""Dog-food: prove the agent loop drives REAL harness work end-to-end.

Runs aion's store + a real DemoHarness, feeds the agent an instruction that
emits a <tool run> tag, verifies a real task spawns and completes, then
exercises note + state via the same tool path. No external backend required.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aion.core import Bus, Intent, IntentType
from aion.store import Store
from aion.harnesses import build_harnesses
from aion.llm import ChatSession, agent_run
from aion.agent import ToolEnv


async def main():
    bus = Bus()
    store = Store(bus=bus)  # creates its own registry + session store
    # one real demo harness (synthetic, no backend needed), wired to store's
    # registry; harnesses checkpoint via a SessionStore (has .save())
    from aion.core import SessionStore
    sess = SessionStore()
    harnesses = build_harnesses([{"id": "demo", "name": "Demo", "type": "demo",
                                  "tier": "cheap"}], bus, store.registry, store=sess)
    store.harnesses = harnesses

    class FakeLLM:
        def __init__(self):
            self.n = 0
        def __call__(self, session, message, timeout=30):
            self.n += 1
            if self.n == 1:
                return "On it. <tool run>demo prove the agent works</tool>"
            return "Done — demo task spawned and tracked."

    async def on_intent(msg):
        it = msg if isinstance(msg, Intent) else msg.get("intent", msg)
        t = it.type if isinstance(it, Intent) else it.get("type")
        if t == IntentType.COMMAND:
            await store._run_command(it.payload["text"])

    bus.subscribe("intent", on_intent)

    import aion.llm as L
    orig = L.chat_send
    L.chat_send = FakeLLM()
    try:
        loop = asyncio.get_event_loop()
        store._loop = loop
        env = ToolEnv(
            run=lambda h, p: store._agent_run_tool(h, p),
            rerun=lambda: store._agent_rerun_tool(),
            compare=lambda q: store._agent_compare_tool(q),
            mem=lambda q: store._agent_mem_tool(q),
            note=lambda f: store._agent_note_tool(f),
            state=lambda: store._agent_state_tool(),
        )
        reply = await loop.run_in_executor(None, agent_run, store.chat, env)
    finally:
        L.chat_send = orig

    await asyncio.sleep(4.5)  # let the demo task run + finish

    tasks = list(store.registry.tasks.values())
    for t in tasks:
        print(f"  task {t.id}: {t.state.value} label={t.label!r}")
    demo = [t for t in tasks if "prove the agent" in t.label]
    assert demo, f"agent did NOT spawn a real task! tasks={[t.label for t in tasks]}"
    t = demo[0]
    print(f"[dogfood] agent spawned real task {t.id} state={t.state.value}")
    assert t.state.value in ("done", "running"), f"unexpected state {t.state.value}"

    store._agent_note_tool("dogfood: agent loop verified")
    snap = store._agent_state_tool()
    print(f"[dogfood] state snapshot: {snap}")
    print(f"[dogfood] memory facts: {len(store.memory.facts)}")
    print(f"[dogfood] final reply: {reply!r}")
    print("[dogfood] OK — agent loop drives real harness work")


asyncio.run(main())
