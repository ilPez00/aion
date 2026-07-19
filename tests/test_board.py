from pathlib import Path
from aion.board import Card, Board, BoardStore


def test_card_defaults():
    c = Card(title="test card")
    assert c.title == "test card"
    assert c.description == ""
    assert c.column == "backlog"
    assert c.agent_id is None
    assert c.task_id is None
    assert c.priority == 0
    assert len(c.id) == 8


def test_card_roundtrip():
    c = Card(title="read paper", description="important paper",
             column="active", agent_id="agent123", task_id="t001", priority=2)
    d = c.as_dict()
    c2 = Card.from_dict(d)
    assert c2.title == "read paper"
    assert c2.description == "important paper"
    assert c2.column == "active"
    assert c2.agent_id == "agent123"
    assert c2.task_id == "t001"
    assert c2.priority == 2


def test_board_defaults():
    b = Board(title="Research")
    assert b.title == "Research"
    assert b.columns == ["backlog", "active", "done"]
    assert b.cards == []
    assert len(b.id) == 8


def test_board_add_card():
    b = Board(title="Dev")
    c = b.add_card("build feature", "new feature description")
    assert c.title == "build feature"
    assert c.column == "backlog"
    assert len(b.cards) == 1


def test_board_move_card():
    b = Board(title="Test")
    c = b.add_card("task")
    b.move_card(c.id, "active")
    assert b.get_card(c.id).column == "active"


def test_board_move_card_invalid_column():
    b = Board(title="Test")
    c = b.add_card("task")
    result = b.move_card(c.id, "nonexistent")
    assert result is None
    assert b.get_card(c.id).column == "backlog"


def test_board_assign_card():
    b = Board(title="Test")
    c = b.add_card("task")
    b.assign_card(c.id, "agent_x")
    assert b.get_card(c.id).agent_id == "agent_x"
    assert b.get_card(c.id).column == "active"


def test_board_cards_in_column():
    b = Board(title="Test")
    c1 = b.add_card("task1")
    c2 = b.add_card("task2")
    b.move_card(c2.id, "active")
    assert len(b.cards_in_column("backlog")) == 1
    assert len(b.cards_in_column("active")) == 1


def test_board_roundtrip():
    b = Board(title="Project", columns=["todo", "doing", "done"])
    b.add_card("task1")
    c2 = b.add_card("task2", column="doing")
    d = b.as_dict()
    b2 = Board.from_dict(d)
    assert b2.title == "Project"
    assert b2.columns == ["todo", "doing", "done"]
    assert len(b2.cards) == 2
    assert b2.get_card(c2.id).column == "doing"


def test_board_store_create():
    store = BoardStore(path="/tmp/test_boards.json")
    b = store.create("Research")
    assert b.title == "Research"
    assert store.get(b.id) is not None
    assert store.get_by_title("Research") is not None
    store.path.unlink(missing_ok=True)


def test_board_store_persist():
    path = Path("/tmp/test_boards_persist.json")
    path.unlink(missing_ok=True)
    store = BoardStore(path=path)
    b = store.create("Dev")
    store.add_card(b.id, "fix bug")
    store.save()
    store2 = BoardStore(path=path)
    assert store2.get(b.id) is not None
    assert len(store2.get(b.id).cards) == 1
    path.unlink(missing_ok=True)


def test_board_store_add_card():
    store = BoardStore(path="/tmp/test_boards_add.json")
    b = store.create("Project")
    c = store.add_card(b.id, "new task", "details")
    assert c is not None
    assert c.title == "new task"
    store.path.unlink(missing_ok=True)


def test_board_store_move_card():
    store = BoardStore(path="/tmp/test_boards_move.json")
    b = store.create("Project")
    c = store.add_card(b.id, "task")
    result = store.move_card(b.id, c.id, "active")
    assert result is not None
    assert result.column == "active"
    store.path.unlink(missing_ok=True)


def test_board_store_assign_card():
    store = BoardStore(path="/tmp/test_boards_assign.json")
    b = store.create("Project")
    c = store.add_card(b.id, "task")
    result = store.assign_card(b.id, c.id, "agent1")
    assert result is not None
    assert result.agent_id == "agent1"
    store.path.unlink(missing_ok=True)


def test_board_store_list_all():
    store = BoardStore(path="/tmp/test_boards_list.json")
    store.create("B1")
    store.create("B2")
    assert len(store.list_all()) == 2
    store.path.unlink(missing_ok=True)


def test_board_store_delete():
    store = BoardStore(path="/tmp/test_boards_del.json")
    b = store.create("Temp")
    assert store.delete(b.id) is True
    assert store.get(b.id) is None
    store.path.unlink(missing_ok=True)
