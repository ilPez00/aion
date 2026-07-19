from pathlib import Path
from aion.agents import AgentEntity, AgentStatus, AgentStore, AgentMemory


def test_agent_entity_defaults():
    a = AgentEntity(name="Alice")
    assert a.name == "Alice"
    assert a.status == AgentStatus.IDLE
    assert a.goal == ""
    assert a.capabilities == []
    assert a.memory_entries == []
    assert a.current_task_id is None
    assert a.assigned_board is None
    assert len(a.id) == 12


def test_agent_entity_touch():
    a = AgentEntity(name="Bob")
    ts = a.updated
    a.touch()
    assert a.updated >= ts


def test_agent_entity_roundtrip():
    a = AgentEntity(name="Charlie", goal="research RAG",
                    capabilities=["coding", "writing"])
    a.status = AgentStatus.WORKING
    a.memory_entries.append(AgentMemory(ts=100.0, text="found interesting paper", kind="note"))
    d = a.as_dict()
    a2 = AgentEntity.from_dict(d)
    assert a2.name == "Charlie"
    assert a2.goal == "research RAG"
    assert a2.status == AgentStatus.WORKING
    assert a2.capabilities == ["coding", "writing"]
    assert len(a2.memory_entries) == 1
    assert a2.memory_entries[0].text == "found interesting paper"


def test_agent_store_create():
    store = AgentStore(path="/tmp/test_agents.json")
    a = store.create("Alice", "build agentic HUD")
    assert a.name == "Alice"
    assert a.goal == "build agentic HUD"
    assert store.get(a.id) is not None
    assert store.get_by_name("Alice") is not None
    store.path.unlink(missing_ok=True)


def test_agent_store_persist():
    path = Path("/tmp/test_agents_persist.json")
    path.unlink(missing_ok=True)
    store = AgentStore(path=path)
    a = store.create("Bob")
    store.save()
    store2 = AgentStore(path=path)
    assert store2.get(a.id) is not None
    assert store2.get_by_name("Bob") is not None
    path.unlink(missing_ok=True)


def test_agent_store_delete():
    store = AgentStore(path="/tmp/test_agents_del.json")
    a = store.create("Charlie")
    assert store.delete(a.id) is True
    assert store.get(a.id) is None
    assert store.delete("nonexistent") is False
    store.path.unlink(missing_ok=True)


def test_agent_store_assign_task():
    store = AgentStore(path="/tmp/test_agents_task.json")
    a = store.create("Dave")
    store.assign_task(a.id, "t0001")
    assert store.get(a.id).status == AgentStatus.WORKING
    assert store.get(a.id).current_task_id == "t0001"
    store.release_task(a.id)
    assert store.get(a.id).status == AgentStatus.IDLE
    assert store.get(a.id).current_task_id is None
    store.path.unlink(missing_ok=True)


def test_agent_store_add_memory():
    store = AgentStore(path="/tmp/test_agents_mem.json")
    a = store.create("Eve")
    store.add_memory(a.id, "remember this")
    assert len(store.get(a.id).memory_entries) == 1
    store.path.unlink(missing_ok=True)


def test_agent_store_set_goal():
    store = AgentStore(path="/tmp/test_agents_goal.json")
    a = store.create("Frank")
    store.set_goal(a.id, "new goal")
    assert store.get(a.id).goal == "new goal"
    store.path.unlink(missing_ok=True)


def test_agent_store_list_all():
    store = AgentStore(path="/tmp/test_agents_list.json")
    store.create("A1")
    store.create("A2")
    assert len(store.list_all()) == 2
    store.path.unlink(missing_ok=True)


def test_agent_memory_roundtrip():
    m = AgentMemory(ts=100.0, text="hello", kind="note")
    d = m.as_dict()
    m2 = AgentMemory.from_dict(d)
    assert m2.ts == 100.0
    assert m2.text == "hello"
    assert m2.kind == "note"


def test_agent_status_values():
    assert AgentStatus.IDLE.value == "idle"
    assert AgentStatus.WORKING.value == "working"
    assert AgentStatus.BLOCKED.value == "blocked"
