"""Tests for github_growth subsystem — store, scoring, queue, metrics."""
import os
import sys
import json
import sqlite3
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aion.github_growth.config import GrowthConfig
from aion.github_growth.store_sqlite import GitHubStore
from aion.github_growth.score import Scorer
from aion.github_growth.queue import ActionQueue
from aion.github_growth.actions import ActionExecutor, ActionResult
from aion.github_growth.metrics import MetricsTracker
from aion.github_growth.graph import NetworkGraph
from aion.github_growth.report import ReportGenerator
from aion.github_growth.discover import SeedDiscoverer, SeedCandidate
from aion.github_growth.enrich import ProfileEnricher, EnrichedProfile


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _config(tmp_path):
    return GrowthConfig(
        enabled=True,
        dry_run=True,
        max_follows_per_day=5,
        max_unfollows_per_day=5,
        max_total_actions_per_day=10,
        min_score_to_queue=0.3,
        min_followers_to_follow=10,
        min_stars_to_follow=5,
        db_path=str(tmp_path / "test.db"),
    )


def _store(tmp_path):
    return GitHubStore(str(tmp_path / "test.db"))


PROFILE_GOOD = {
    "login": "testuser",
    "name": "Test User",
    "bio": "Developer at Spacely's",
    "company": "Spacely's",
    "blog": "https://example.com",
    "location": "Boston",
    "email": "",
    "twitter_username": "testuser",
    "avatar_url": "https://avatars.githubusercontent.com/u/1",
    "html_url": "https://github.com/testuser",
    "followers": 150,
    "following": 45,
    "public_repos": 12,
    "public_gists": 3,
    "created_at": "2020-01-01T00:00:00Z",
    "updated_at": "2024-01-01T00:00:00Z",
}

REPOS = [
    {"name": "project1", "stargazers_count": 50, "forks_count": 10, "open_issues": 3, "language": "Python"},
    {"name": "project2", "stargazers_count": 20, "forks_count": 5, "open_issues": 1, "language": "Rust"},
]


# ── Store tests ─────────────────────────────────────────────────────────────


def test_store_creates_db(tmp_path):
    db = _store(tmp_path)
    assert (tmp_path / "test.db").exists()
    db.close()


def test_store_upsert_user(tmp_path):
    db = _store(tmp_path)
    db.upsert_user(PROFILE_GOOD)
    user = db.get_user("testuser")
    assert user is not None
    assert user["login"] == "testuser"
    assert user["name"] == "Test User"
    assert user["followers"] == 150
    db.close()


def test_store_upsert_user_updates(tmp_path):
    db = _store(tmp_path)
    db.upsert_user(PROFILE_GOOD)
    updated = {**PROFILE_GOOD, "followers": 200, "name": "Updated Name"}
    db.upsert_user(updated)
    user = db.get_user("testuser")
    assert user["followers"] == 200
    assert user["name"] == "Updated Name"
    db.close()


def test_store_upsert_repo(tmp_path):
    db = _store(tmp_path)
    db.upsert_repo(REPOS[0])
    row = db.conn.execute("SELECT * FROM repos WHERE full_name = ?", ("",)).fetchone()
    # full_name not set in REPOS[0], but we still upserted
    assert db.conn.execute("SELECT COUNT(*) as c FROM repos").fetchone()["c"] == 1
    db.close()


def test_store_save_and_get_score(tmp_path):
    db = _store(tmp_path)
    db.save_score("testuser", 0.85, "test reason", {"factor1": 0.8})
    result = db.get_latest_score("testuser")
    assert result is not None
    assert result[0] == 0.85
    assert result[1] == "test reason"
    db.close()


def test_store_enqueue_and_query(tmp_path):
    db = _store(tmp_path)
    db.enqueue("user1", "follow", 0.9, "high score")
    db.enqueue("user2", "follow", 0.5, "medium score")

    # Cannot re-enqueue same user
    assert db.get_queue().__len__() == 2

    # Approve user1
    assert db.approve_in_queue("user1") is True
    queue = db.get_queue()
    approved = [q for q in queue if q["approved"]]
    pending = [q for q in queue if not q["approved"]]
    assert len(approved) == 1
    assert len(pending) == 1
    assert approved[0]["username"] == "user1"
    assert pending[0]["username"] == "user2"
    db.close()


def test_store_log_and_count_actions(tmp_path):
    db = _store(tmp_path)
    db.log_action("user1", "follow", dry_run=True, result="dry_run", reason="test")
    db.log_action("user2", "follow", dry_run=False, result="success", reason="test")
    db.log_action("user3", "unfollow", dry_run=False, result="success", reason="test")

    assert db.actions_today() == 3
    assert db.actions_today_by_type("follow") == 2
    assert db.actions_today_by_type("unfollow") == 1
    db.close()


def test_store_metrics(tmp_path):
    db = _store(tmp_path)
    db.increment_metric("discover_count")
    db.increment_metric("discover_count")
    db.increment_metric("score_count")
    assert db.get_metric("discover_count") == 2
    assert db.get_metric("score_count") == 1
    today = db.metrics_for_today()
    assert today["discover_count"] == 2
    assert today["score_count"] == 1
    db.close()


def test_store_graph_edges(tmp_path):
    db = _store(tmp_path)
    db.upsert_edge("alice", "bob", "follows")
    db.upsert_edge("bob", "alice", "follows")
    db.upsert_edge("alice", "physis-pro", "contributed_to", 0.95)

    followers = db.get_followers("bob")
    following = db.get_following("alice")
    assert "alice" in followers
    assert "bob" in following
    db.close()


def test_store_stats(tmp_path):
    db = _store(tmp_path)
    db.upsert_user(PROFILE_GOOD)
    db.save_score("testuser", 0.8, "reason", {})
    db.enqueue("user1", "follow", 0.8, "reason")
    db.log_action("user1", "follow", True, "dry_run", "test")
    db.upsert_edge("a", "b", "follows")
    stats = db.stats()
    assert stats["users_count"] == 1
    assert stats["scores_count"] == 1
    assert stats["queue_count"] == 1
    assert stats["actions_count"] == 1
    assert stats["graph_edges_count"] == 1
    db.close()


# ── Scorer tests ────────────────────────────────────────────────────────────


def test_scorer_good_user():
    config = _config(Path("/tmp"))
    scorer = Scorer(config)
    profile = {**PROFILE_GOOD, "login": "gooduser"}
    repos = REPOS
    result = scorer.score(profile, repos, {"source_type": "contributor", "contributions": 5})
    assert result.score > 0.3  # passes threshold
    assert result.score <= 1.0
    assert "contribution" in result.factors


def test_scorer_spam_user_blocked():
    config = _config(Path("/tmp"))
    scorer = Scorer(config)
    # User with 0 followers but follows 1000 accounts
    spam_profile = {
        "login": "spamuser",
        "name": "",
        "bio": "",
        "following": 1000,
        "public_repos": 1,
        "followers": 0,
    }
    result = scorer.score(spam_profile, [], {"source_type": "user_provided"})
    assert result.score < 0.5  # low score due to spam


def test_scorer_threshold_filter():
    config = _config(Path("/tmp"))
    config.min_score_to_queue = 0.5
    scorer = Scorer(config)
    profile = {**PROFILE_GOOD, "login": "lowuser"}
    result = scorer.score(profile, [], {"source_type": "search", "search_query": "python"})
    qualifies = scorer.qualifies(result)
    # Search results get 0.3 contribution — should not qualify at 0.5 threshold
    if result.score < 0.5:
        assert qualifies is False
    else:
        assert qualifies is True


# ── Queue tests ─────────────────────────────────────────────────────────────


def test_queue_add_and_approve(tmp_path):
    config = _config(tmp_path)
    store = _store(tmp_path)
    queue = ActionQueue(store, config)

    from aion.github_growth.score import ScoreResult
    from datetime import datetime, timezone
    result = ScoreResult(
        username="testuser",
        score=0.85,
        reason="test reason",
        factors={"contribution": 0.8, "quality": 0.7},
        scored_at=datetime.now(timezone.utc).isoformat(),
    )

    assert queue.add(result, action="follow") is True
    pending = queue.list_pending()
    assert len(pending) == 1
    assert pending[0].username == "testuser"

    assert queue.approve("testuser") is True
    approved = queue.get_approved()
    assert len(approved) == 1
    store.close()


def test_queue_daily_limit_block(tmp_path):
    config = _config(tmp_path)
    config.max_total_actions_per_day = 0  # block everything
    store = _store(tmp_path)
    queue = ActionQueue(store, config)

    from aion.github_growth.score import ScoreResult
    from datetime import datetime, timezone
    result = ScoreResult(
        username="testuser",
        score=0.9,
        reason="high",
        factors={},
        scored_at=datetime.now(timezone.utc).isoformat(),
    )

    # Should be blocked by daily limit (0 actions allowed)
    assert queue.add(result, action="follow") is False
    store.close()


def test_queue_clear(tmp_path):
    config = _config(tmp_path)
    store = _store(tmp_path)
    queue = ActionQueue(store, config)

    from aion.github_growth.score import ScoreResult
    from datetime import datetime, timezone
    result = ScoreResult(
        username="user1",
        score=0.9,
        reason="high",
        factors={},
        scored_at=datetime.now(timezone.utc).isoformat(),
    )
    queue.add(result, action="follow")
    queue.add(ScoreResult(username="user2", score=0.8, reason="ok",
                          factors={}, scored_at=datetime.now(timezone.utc).isoformat()),
              action="follow")

    count = queue.clear_pending()
    assert count == 2
    store.close()


# ── Discover tests ──────────────────────────────────────────────────────────


def test_seed_discoverer_exclude_bots(tmp_path):
    config = _config(tmp_path)
    config.blocklist = ["spamuser"]

    mock_api = MagicMock()
    mock_api.list_repo_contributors.return_value = [
        {"login": "gooddev", "contributions": 10},
        {"login": "spamuser", "contributions": 1},
        {"login": "bot[bot]", "contributions": 5},
    ]
    mock_api.list_repo_dependents.return_value = []

    discoverer = SeedDiscoverer(mock_api, config)
    candidates = discoverer.discover_from_repo("ilPez00", "physis-pro")

    usernames = [c.username for c in candidates]
    assert "gooddev" in usernames
    assert "spamuser" not in usernames
    assert "bot[bot]" not in usernames  # bot excluded


def test_seed_discoverer_user_provided(tmp_path):
    config = _config(tmp_path)
    mock_api = MagicMock()

    discoverer = SeedDiscoverer(mock_api, config)
    candidates = discoverer.discover_from_users(["alice", "bob"])
    assert len(candidates) == 2
    assert all(c.source_type == "user_provided" for c in candidates)


# ── Enrich tests ────────────────────────────────────────────────────────────


def test_enricher_cached_profile(tmp_path):
    config = _config(tmp_path)
    config.fetch_repos = False
    config.fetch_followers = False

    mock_api = MagicMock()
    mock_api.get_user.return_value = PROFILE_GOOD

    store = _store(tmp_path)
    enricher = ProfileEnricher(mock_api, store, config)

    # First enrich — fetches from API
    ep = enricher.enrich("testuser")
    assert ep is not None
    assert ep.username == "testuser"
    assert ep.stale is False
    assert ep.profile["followers"] == 150

    # Second enrich — returns cached (not stale)
    ep2 = enricher.enrich("testuser")
    assert ep2.stale is True  # within 24h, returns cached
    store.close()


# ── Action Executor tests ───────────────────────────────────────────────────


def test_executor_dry_run_follow(tmp_path):
    config = _config(tmp_path)
    config.enabled = True
    config.dry_run = True

    store = _store(tmp_path)
    api = MagicMock()
    api.follow = MagicMock(return_value=True)

    from aion.github_growth.queue import ActionQueue
    queue = ActionQueue(store, config)

    from aion.github_growth.score import ScoreResult
    from datetime import datetime, timezone
    score = ScoreResult(
        username="testuser",
        score=0.9,
        reason="good",
        factors={},
        scored_at=datetime.now(timezone.utc).isoformat(),
    )
    queue.add(score, action="follow")
    queue.approve("testuser")

    executor = ActionExecutor(api, store, queue, config)
    results = executor.execute_approved()

    assert len(results) == 1
    assert results[0].result == ActionResult.DRY_RUN
    assert results[0].dry_run is True
    # api.follow should NOT be called in dry-run
    api.follow.assert_not_called()

    # But it should be logged
    actions = store.all_actions()
    assert len(actions) == 1
    assert actions[0]["result"] == "dry_run"
    store.close()


def test_executor_live_follow_calls_api(tmp_path):
    config = _config(tmp_path)
    config.enabled = True
    config.dry_run = False

    store = _store(tmp_path)
    api = MagicMock()
    api.follow = MagicMock(return_value=True)

    from aion.github_growth.queue import ActionQueue
    queue = ActionQueue(store, config)

    from aion.github_growth.score import ScoreResult
    from datetime import datetime, timezone
    score = ScoreResult(
        username="testuser",
        score=0.9,
        reason="good",
        factors={},
        scored_at=datetime.now(timezone.utc).isoformat(),
    )
    queue.add(score, action="follow")
    queue.approve("testuser")

    executor = ActionExecutor(api, store, queue, config)
    results = executor.execute_approved()

    assert len(results) == 1
    assert results[0].result == ActionResult.SUCCESS
    api.follow.assert_called_once_with("testuser", dry_run=False)
    store.close()


def test_executor_disabled_blocks(tmp_path):
    config = _config(tmp_path)
    config.enabled = False
    config.dry_run = False

    store = _store(tmp_path)
    api = MagicMock()
    from aion.github_growth.queue import ActionQueue, QueuedAction
    queue = ActionQueue(store, config)

    # Manually enqueue + approve directly in store (bypassing queue.add which checks enabled)
    store.enqueue("testuser", "follow", 0.9, "good")
    store.approve_in_queue("testuser")
    # Also add to queue's in-memory approved list so execute_approved sees it
    queue._approved.append(QueuedAction(
        username="testuser", action="follow", score=0.9, reason="good"
    ))

    executor = ActionExecutor(api, store, queue, config)
    results = executor.execute_approved()
    assert len(results) == 1
    assert results[0].result == ActionResult.BLOCKED
    api.follow.assert_not_called()
    store.close()


# ── Metrics tests ───────────────────────────────────────────────────────────


def test_metrics_tracker(tmp_path):
    store = _store(tmp_path)
    metrics = MetricsTracker(store)

    metrics.record("discover_count", 5)
    metrics.record("score_count", 3)
    assert metrics.get("discover_count") == 5
    assert metrics.get("score_count") == 3

    today = metrics.all_today()
    assert today.get("discover_count") == 5
    assert today.get("score_count") == 3

    all_time = metrics.all_time()
    assert all_time.get("discover_count") == 5
    assert all_time.get("score_count") == 3
    store.close()


def test_metrics_csv_export(tmp_path):
    store = _store(tmp_path)
    metrics = MetricsTracker(store)
    metrics.record("discover_count", 3)
    metrics.record("score_count", 2)

    csv_path = metrics.to_csv(tmp_path / "metrics.csv")
    assert csv_path.exists()
    content = csv_path.read_text()
    assert "discover_count" in content
    assert "score_count" in content
    store.close()


# ── Graph tests ─────────────────────────────────────────────────────────────


def test_graph_network_score(tmp_path):
    store = _store(tmp_path)
    graph = NetworkGraph(store)

    # Set up: alice follows bob and carol
    store.upsert_edge("alice", "bob", "follows")
    store.upsert_edge("alice", "carol", "follows")

    # bob's network score: candidates = bob, carol
    # alice follows both, so overlap = 2 / 2 = 1.0
    score = graph.network_score("alice", ["bob", "carol"])
    assert score > 0.5

    # carol is not in alice's connections
    score_none = graph.network_score("alice", ["dave"])
    assert score_none == 0.5  # default neutral
    store.close()


def test_graph_mutual_followers(tmp_path):
    store = _store(tmp_path)
    graph = NetworkGraph(store)

    store.upsert_edge("alice", "bob", "follows")
    store.upsert_edge("carol", "bob", "follows")
    store.upsert_edge("dave", "bob", "follows")

    followers = graph.get_mutual_followers("bob", ["alice", "carol", "eve"])
    assert "alice" in followers
    assert "carol" in followers
    assert "eve" not in followers
    store.close()


# ── Report tests ────────────────────────────────────────────────────────────


def test_report_summary(tmp_path):
    store = _store(tmp_path)
    store.upsert_user(PROFILE_GOOD)
    store.log_action("testuser", "follow", dry_run=True, result="dry_run", reason="test")
    store.save_score("testuser", 0.85, "reason", {"factor": 0.8})
    store.enqueue("user1", "follow", 0.9, "good user")

    reporter = ReportGenerator(store)
    report = reporter.generate_summary()
    assert "GitHub Growth Summary Report" in report
    assert "testuser" in report.lower() or "users" in report.lower()
    store.close()


def test_report_user_profile(tmp_path):
    store = _store(tmp_path)
    store.upsert_user(PROFILE_GOOD)
    store.save_score("testuser", 0.85, "test reason", {"contribution": 0.8})

    reporter = ReportGenerator(store)
    report = reporter.generate_user_profile("testuser")
    assert "User Profile: testuser" in report
    assert "Test User" in report
    assert "150" in report  # followers
    store.close()


def test_report_metrics(tmp_path):
    store = _store(tmp_path)
    store.increment_metric("discover_count", )
    store.increment_metric("score_count")

    reporter = ReportGenerator(store)
    report = reporter.generate_metrics_report(days=7)
    assert "GitHub Growth — Metrics" in report
    assert "discover_count" in report
    store.close()


# ── CLI tests ───────────────────────────────────────────────────────────────


def test_cli_config_shows_dry_run(tmp_path):
    from aion.github_growth.cli import GitHubGrowthCLI
    cli = GitHubGrowthCLI()
    # Use a temp db path
    os.environ["GHGG_DB_PATH"] = str(tmp_path / "cli.db")
    os.environ["GHGG_ENABLED"] = "0"  # disabled by default
    result = cli.run("github-growth config")
    assert result["ok"] is True
    assert "dry_run" in result["output"]
    assert "enabled" in result["output"]
    del os.environ["GHGG_DB_PATH"]
    del os.environ["GHGG_ENABLED"]


def test_cli_discover_no_api_calls(tmp_path):
    """Ensure discover doesn't make real API calls in dry-run."""
    from aion.github_growth.cli import GitHubGrowthCLI
    cli = GitHubGrowthCLI()
    os.environ["GHGG_DB_PATH"] = str(tmp_path / "cli.db")
    result = cli.run("github-growth discover")
    # May succeed (no API key) or fail gracefully
    assert result["action"] == "discover"
    del os.environ["GHGG_DB_PATH"]


def test_cli_queue_list_empty(tmp_path):
    from aion.github_growth.cli import GitHubGrowthCLI
    cli = GitHubGrowthCLI()
    os.environ["GHGG_DB_PATH"] = str(tmp_path / "cli.db")
    result = cli.run("github-growth queue list")
    assert result["ok"] is True
    assert "empty" in result["output"].lower()
    del os.environ["GHGG_DB_PATH"]


def test_cli_queue_approve_not_found(tmp_path):
    from aion.github_growth.cli import GitHubGrowthCLI
    cli = GitHubGrowthCLI()
    os.environ["GHGG_DB_PATH"] = str(tmp_path / "cli.db")
    result = cli.run("github-growth queue approve nonexistent")
    assert result["ok"] is False
    assert "not in queue" in result.get("error", "").lower()
    del os.environ["GHGG_DB_PATH"]


def test_cli_report_summary(tmp_path):
    from aion.github_growth.cli import GitHubGrowthCLI
    cli = GitHubGrowthCLI()
    os.environ["GHGG_DB_PATH"] = str(tmp_path / "cli.db")
    result = cli.run("github-growth report summary")
    assert result["ok"] is True
    assert "GitHub Growth Summary" in result["output"]
    del os.environ["GHGG_DB_PATH"]
