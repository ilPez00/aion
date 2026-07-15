"""Integration test: agent_run executes a tool call and re-prompts (Cycle 10)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aion.agent import ToolEnv
from aion.llm import ChatSession, agent_run


class FakeNet:
    """Stand-in for the LLM: first reply emits a tool, second replies naturally."""
    def __init__(self):
        self.calls = 0

    def reply(self, session, message, timeout=30):
        self.calls += 1
        if self.calls == 1:
            return "Sure, checking state. <tool state></tool>"
        return "All good — state retrieved."


def test_agent_run_executes_tool_and_continues():
    env = ToolEnv(state=lambda: "tasks=3 running=1 failed=0")
    net = FakeNet()
    # patch chat_send used inside agent_run
    import aion.llm as L
    orig = L.chat_send
    L.chat_send = net.reply
    try:
        s = ChatSession()
        out = agent_run(s, env, max_steps=3)
    finally:
        L.chat_send = orig
    assert net.calls == 2, f"expected 2 LLM calls (tool + follow-up), got {net.calls}"
    assert "state retrieved" in out
    # the tool observation was folded into context
    assert any("tasks=3" in m.content for m in s.messages)


def test_agent_run_no_tools_is_plain_chat():
    env = ToolEnv()
    net = FakeNet()
    import aion.llm as L
    orig = L.chat_send
    L.chat_send = lambda s, m, timeout=30: "just talking"
    try:
        out = agent_run(ChatSession(), env)
    finally:
        L.chat_send = orig
    assert out == "just talking"
