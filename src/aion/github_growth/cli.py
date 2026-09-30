"""CLI parser for github_growth subsystem.

Commands:
  github-growth discover          — discover seed candidates
  github-growth enrich <user>     — enrich a single user profile
  github-growth score <user>      — score a single user
  github-growth queue list        — list queued actions
  github-growth queue approve <u> — approve a specific user
  github-growth queue clear       — clear pending queue
  github-growth run               — full cycle: discover → enrich → score → queue
  github-growth follow <user>     — directly follow (dry-run by default)
  github-growth unfollow <user>   — directly unfollow (dry-run by default)
  github-growth execute           — execute approved actions
  github-growth report            — generate summary report
  github-growth report user <u>   — user profile report
  github-growth report metrics    — metrics report
  github-growth stats             — quick stats
  github-growth config            — show config

All commands support --dry-run (default) and --live (to actually execute).
"""
from __future__ import annotations

import argparse
from typing import Any, Sequence

from .api import GitHubAPI
from .config import GrowthConfig
from .enrich import ProfileEnricher
from .discover import SeedDiscoverer
from .score import Scorer
from .queue import ActionQueue
from .actions import ActionExecutor
from .metrics import MetricsTracker
from .graph import NetworkGraph
from .report import ReportGenerator
from .store_sqlite import GitHubStore


class GitHubGrowthCLI:
    """Command-line interface for github_growth subsystem.

    Usage:
        cli = GitHubGrowthCLI()
        result = cli.run("github-growth run --limit 5")
        # result = {"ok": True, "output": "...", "action": "run"}
    """

    def __init__(self):
        self.parser = self._build_parser()

    def _build_parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(
            prog="github-growth",
            description="Legitimate, approval-gated GitHub profile discovery and engagement.",
        )
        parser.add_argument("--live", action="store_true",
                            help="Actually execute actions (default: dry-run)")
        parser.add_argument("--enabled", action="store_true",
                            help="Override config.enabled (normally false)")

        sub = parser.add_subparsers(dest="command", required=True)

        # discover
        p = sub.add_parser("discover", help="Discover seed candidates")
        p.add_argument("--repo", help="Specific repo to discover from (owner/repo)")

        # enrich
        p = sub.add_parser("enrich", help="Enrich a single user profile")
        p.add_argument("username", help="GitHub username to enrich")

        # score
        p = sub.add_parser("score", help="Score a single user")
        p.add_argument("username", help="GitHub username to score")

        # queue
        pq = sub.add_parser("queue", help="Queue management")
        pq_sub = pq.add_subparsers(dest="queue_command", required=True)
        pq_sub.add_parser("list", help="List queued actions")
        ppa = pq_sub.add_parser("approve", help="Approve a queued user")
        ppa.add_argument("username", help="Username to approve")
        pq_sub.add_parser("clear", help="Clear pending queue")

        # run (full cycle)
        p = sub.add_parser("run", help="Full cycle: discover → enrich → score → queue")
        p.add_argument("--users", nargs="+", help="Extra user seeds")
        p.add_argument("--search", help="Search query for additional seeds")
        p.add_argument("--limit", type=int, default=10, help="Max candidates to process")

        # follow / unfollow
        for cmd in ("follow", "unfollow"):
            p = sub.add_parser(cmd, help=f"Directly {cmd} a user")
            p.add_argument("username", help="GitHub username")

        # execute
        sub.add_parser("execute", help="Execute all approved actions in queue")

        # report
        pr = sub.add_parser("report", help="Generate reports")
        pr_sub = pr.add_subparsers(dest="report_command", required=True)
        pr_sub.add_parser("summary", help="Summary report")
        pru = pr_sub.add_parser("user", help="User profile report")
        pru.add_argument("username", help="Username")
        prm = pr_sub.add_parser("metrics", help="Metrics report")
        prm.add_argument("--days", type=int, default=7, help="Days of metrics to show")

        # stats
        sub.add_parser("stats", help="Quick stats")

        # config
        sub.add_parser("config", help="Show current configuration")

        return parser

    def _make_components(self, args: argparse.Namespace) -> dict[str, Any]:
        """Build all components from config."""
        config = GrowthConfig.from_env()
        if args.live:
            config.dry_run = False
        if args.enabled:
            config.enabled = True

        token = None
        try:
            from ..credentials import CredentialStore
            cred_store = CredentialStore()
            profile = cred_store.get("github")
            if profile and profile.api_key and not profile.api_key.startswith("#"):
                token = profile.api_key
            else:
                token = cred_store.resolve_api_key("github")
                if token and not token.startswith("#"):
                    pass
                else:
                    token = None
        except Exception:
            pass
        if not token:
            import os
            from pathlib import Path
            token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GITHUB")
            if not token or token.startswith("#"):
                env_path = Path.home() / ".env"
                if env_path.exists():
                    try:
                        for line in env_path.read_text().splitlines():
                            line = line.strip()
                            if not line or line.startswith("#") or "=" not in line:
                                continue
                            key, _, val = line.partition("=")
                            if key.strip() == "GITHUB":
                                token = val.strip().strip("\"'").strip()
                                break
                    except Exception:
                        pass

        api = GitHubAPI(token=token, config=config)
        store = GitHubStore(config.db_path)
        enricher = ProfileEnricher(api, store, config)
        discoverer = SeedDiscoverer(api, config)
        scorer = Scorer(config)
        queue = ActionQueue(store, config)
        executor = ActionExecutor(api, store, queue, config)
        metrics = MetricsTracker(store)
        graph = NetworkGraph(store)
        reporter = ReportGenerator(store)

        return {
            "config": config,
            "api": api,
            "store": store,
            "enricher": enricher,
            "discoverer": discoverer,
            "scorer": scorer,
            "queue": queue,
            "executor": executor,
            "metrics": metrics,
            "graph": graph,
            "reporter": reporter,
        }

    def run(self, text: str) -> dict[str, Any]:
        """Parse and execute a command string.

        Returns: {"ok": bool, "output": str, "action": str, "error": str (if !ok)}
        """
        # The first token is "github-growth" — strip it for argparse
        parts = text.split(None, 1)
        if parts and parts[0] in ("github-growth", "github_growth"):
            sub_text = parts[1] if len(parts) > 1 else ""
        else:
            sub_text = text

        try:
            args = self.parser.parse_args(sub_text.split() if sub_text else [])
        except SystemExit:
            return {"ok": False, "output": "", "action": "parse", "error": "invalid arguments"}

        comps = self._make_components(args)

        try:
            if args.command == "discover":
                result = self._cmd_discover(args, comps)
            elif args.command == "enrich":
                result = self._cmd_enrich(args, comps)
            elif args.command == "score":
                result = self._cmd_score(args, comps)
            elif args.command == "queue":
                result = self._cmd_queue(args, comps)
            elif args.command == "run":
                result = self._cmd_run(args, comps)
            elif args.command in ("follow", "unfollow"):
                result = self._cmd_follow_unfollow(args, comps)
            elif args.command == "execute":
                result = self._cmd_execute(args, comps)
            elif args.command == "report":
                result = self._cmd_report(args, comps)
            elif args.command == "stats":
                result = self._cmd_stats(args, comps)
            elif args.command == "config":
                result = self._cmd_config(args, comps)
            else:
                result = {"ok": False, "output": f"Unknown command: {args.command}", "action": "dispatch", "error": "unknown command"}
        finally:
            comps["store"].close()

        return result

    # ── Command handlers ──────────────────────────────────────────────────────

    def _cmd_discover(self, args: argparse.Namespace, comps: dict) -> dict[str, Any]:
        config = comps["config"]
        discoverer = comps["discoverer"]
        metrics = comps["metrics"]

        if args.repo:
            parts = args.repo.split("/")
            if len(parts) != 2:
                return {"ok": False, "output": "", "action": "discover",
                        "error": f"Invalid repo format: {args.repo} (expected owner/repo)"}
            candidates = discoverer.discover_from_repo(parts[0], parts[1])
        else:
            candidates = discoverer.discover_from_seeds()

        metrics.record("discover_count", len(candidates))
        lines = [f"Discovered {len(candidates)} candidates:"]
        for c in candidates[:20]:
            extra = c.context.get("contributions", c.context.get("fork_name", ""))
            lines.append(f"  {c.username:20s} via {c.source_type} ({extra})")
        if len(candidates) > 20:
            lines.append(f"  ... and {len(candidates) - 20} more")
        return {"ok": True, "output": "\n".join(lines), "action": "discover"}

    def _cmd_enrich(self, args: argparse.Namespace, comps: dict) -> dict[str, Any]:
        enricher = comps["enricher"]
        metrics = comps["metrics"]
        profile = enricher.enrich(args.username)
        if profile is None:
            return {"ok": False, "output": "", "action": "enrich",
                    "error": f"User '{args.username}' not found or API error"}
        metrics.record("enrich_count")
        p = profile.profile
        lines = [
            f"Profile: {p.get('login', args.username)}",
            f"  Name:       {p.get('name', '(none)')}",
            f"  Bio:        {p.get('bio', '(none')}",
            f"  Followers:  {p.get('followers', 0)}",
            f"  Following:  {p.get('following', 0)}",
            f"  Repos:      {p.get('public_repos', 0)}",
            f"  Fetched:    {profile.fetched_at}",
        ]
        return {"ok": True, "output": "\n".join(lines), "action": "enrich"}

    def _cmd_score(self, args: argparse.Namespace, comps: dict) -> dict[str, Any]:
        scorer = comps["scorer"]
        store = comps["store"]
        metrics = comps["metrics"]
        enricher = comps["enricher"]
        profile = enricher.enrich(args.username)
        if profile is None:
            return {"ok": False, "output": "", "action": "score",
                    "error": f"User '{args.username}' not found"}
        result = scorer.score(profile.profile, profile.repos)
        store.save_score(result.username, result.score, result.reason, result.factors)
        metrics.record("score_count")
        factors_str = "\n".join(f"    {k:15s} {v:.3f}" for k, v in result.factors.items())
        qualifies = scorer.qualifies(result)
        lines = [
            f"Score for {result.username}: {result.score:.3f}",
            f"  Reason: {result.reason}",
            "  Factors:",
            factors_str,
            f"  Qualifies (>={scorer.config.min_score_to_queue}): {qualifies}",
        ]
        return {"ok": True, "output": "\n".join(lines), "action": "score"}

    def _cmd_queue(self, args: argparse.Namespace, comps: dict) -> dict[str, Any]:
        store = comps["store"]
        action = args.queue_command

        if action == "list":
            queued = store.get_queue()
            if not queued:
                return {"ok": True, "output": "Queue is empty", "action": "queue_list"}
            lines = [f"{'Score':>6s}  {'Action':<8s}  {'Approved':<8s}  {'Username':<20s}  Reason"]
            lines.append("-" * 90)
            for q in queued:
                approved = "yes" if q.get("approved") else "no"
                reason = q["reason"][:40]
                lines.append(f"{q['score']:6.3f}  {q['action']:<8s}  {approved:<8s}  {q['username']:<20s}  {reason}")
            return {"ok": True, "output": "\n".join(lines), "action": "queue_list"}

        elif action == "approve":
            username = args.username
            rows = store.conn.execute(
                "SELECT 1 FROM queue WHERE username = ?", (username,)
            ).fetchone()
            if not rows:
                return {"ok": False, "output": "", "action": "queue_approve",
                        "error": f"User '{username}' not in queue"}
            store.approve_in_queue(username)
            return {"ok": True, "output": f"Approved '{username}' for action", "action": "queue_approve"}

        elif action == "clear":
            count = store.clear_queue()
            return {"ok": True, "output": f"Cleared {count} items from queue", "action": "queue_clear"}

        return {"ok": False, "output": "", "action": "queue", "error": "unknown queue subcommand"}

    def _cmd_run(self, args: argparse.Namespace, comps: dict) -> dict[str, Any]:
        config = comps["config"]
        discoverer = comps["discoverer"]
        enricher = comps["enricher"]
        scorer = comps["scorer"]
        queue = comps["queue"]
        metrics = comps["metrics"]
        store = comps["store"]

        lines = []

        # 1. Discover seeds
        candidates = discoverer.discover_from_seeds()
        if args.users:
            user_seeds = discoverer.discover_from_users(args.users)
            candidates.extend(user_seeds)
        if args.search:
            search_seeds = discoverer.discover_from_search(args.search, limit=20)
            candidates.extend(search_seeds)

        seen: set[str] = set()
        unique: list = []
        for c in candidates:
            if c.username not in seen:
                seen.add(c.username)
                unique.append(c)
        candidates = unique[:args.limit]

        metrics.record("discover_count", len(candidates))
        lines.append(f"Discovered {len(candidates)} unique candidates")

        # 2-3. Enrich + Score
        scored_count = 0
        queued_count = 0
        for c in candidates:
            ep = enricher.enrich(c.username)
            if ep is None or ep.profile.get("login") is None:
                continue
            factors = c.context
            result = scorer.score(ep.profile, ep.repos, factors)
            store.save_score(result.username, result.score, result.reason, result.factors)
            scored_count += 1
            metrics.record("score_count")
            if scorer.qualifies(result) and queue.add(result, action="follow", context=factors):
                queued_count += 1
                metrics.record("queue_count")

        metrics.record("enrich_count", scored_count)
        lines.append(f"Enriched + scored {scored_count} profiles")
        lines.append(f"Queued {queued_count} candidates for approval")

        pending = queue.list_pending()
        if pending:
            lines.append("\nTop 10 pending:")
            for q in pending[:10]:
                lines.append(f"  [{q.score:.3f}] {q.username:20s} {q.reason[:60]}")

        return {"ok": True, "output": "\n".join(lines), "action": "run"}

    def _cmd_follow_unfollow(self, args: argparse.Namespace, comps: dict) -> dict[str, Any]:
        executor = comps["executor"]
        action = args.command
        result = executor.follow(args.username, reason=action) if action == "follow" else \
                 executor.unfollow(args.username, reason=action)
        status = "✓" if result.result.value == "success" else "✗"
        suffix = " (DRY RUN)" if result.dry_run else ""
        return {
            "ok": result.result.value in ("success", "dry_run"),
            "output": f"{status} {action} {args.username}: {result.result.value}{suffix}\n  reason: {result.reason}",
            "action": action,
        }

    def _cmd_execute(self, args: argparse.Namespace, comps: dict) -> dict[str, Any]:
        executor = comps["executor"]
        results = executor.execute_approved()
        if not results:
            return {"ok": True, "output": "No approved actions in queue", "action": "execute"}
        lines = [f"Executed {len(results)} actions:"]
        for r in results:
            status = "✓" if r.result.value == "success" else "✗"
            suffix = " (dry-run)" if r.dry_run else ""
            lines.append(f"  {status} {r.action} {r.username}: {r.result.value}{suffix}")
            lines.append(f"    reason: {r.reason}")
        return {"ok": True, "output": "\n".join(lines), "action": "execute"}

    def _cmd_report(self, args: argparse.Namespace, comps: dict) -> dict[str, Any]:
        reporter = comps["reporter"]
        if args.report_command == "user":
            return {"ok": True, "output": reporter.generate_user_profile(args.username), "action": "report_user"}
        elif args.report_command == "metrics":
            return {"ok": True, "output": reporter.generate_metrics_report(args.days), "action": "report_metrics"}
        else:
            return {"ok": True, "output": reporter.generate_summary(), "action": "report_summary"}

    def _cmd_stats(self, args: argparse.Namespace, comps: dict) -> dict[str, Any]:
        store = comps["store"]
        metrics = comps["metrics"]
        stats = store.stats()
        lines = ["GitHub Growth Stats", "=" * 40]
        for key, val in stats.items():
            lines.append(f"  {key:20s} {val}")
        lines.append("\nMetrics Today:")
        today = metrics.all_today()
        for k in sorted(today.keys()):
            lines.append(f"  {k:25s} {today[k]}")
        return {"ok": True, "output": "\n".join(lines), "action": "stats"}

    def _cmd_config(self, args: argparse.Namespace, comps: dict) -> dict[str, Any]:
        config = comps["config"]
        api = comps["api"]
        lines = ["GitHub Growth Configuration", "=" * 40]
        d = config.to_dict()
        for k, v in d.items():
            lines.append(f"  {k:25s} {v}")
        lines.append(f"\n  enabled:                   {config.enabled}")
        lines.append(f"  api_available:             {api.available}")
        return {"ok": True, "output": "\n".join(lines), "action": "config"}


def run(text: str) -> dict[str, Any]:
    """Convenience function for direct invocation."""
    cli = GitHubGrowthCLI()
    return cli.run(text)


def main():
    """Console entry point for `github-growth` command."""
    import sys
    import argparse
    from .config import GrowthConfig

    # Parse top-level flags --live, --enabled, then pass rest to subcommand parser
    parser = argparse.ArgumentParser(
        prog="github-growth",
        description="Legitimate, approval-gated GitHub profile discovery and engagement.",
    )
    parser.add_argument("--live", action="store_true",
                        help="Actually execute actions (default: dry-run)")
    parser.add_argument("--enabled", action="store_true",
                        help="Override config.enabled (normally false)")
    sub = parser.add_subparsers(dest="command", required=True)

    # discover
    p = sub.add_parser("discover", help="Discover seed candidates")
    p.add_argument("--repo", help="Specific repo to discover from (owner/repo)")

    # enrich
    p = sub.add_parser("enrich", help="Enrich a single user profile")
    p.add_argument("username", help="GitHub username to enrich")

    # score
    p = sub.add_parser("score", help="Score a single user")
    p.add_argument("username", help="GitHub username to score")

    # queue
    pq = sub.add_parser("queue", help="Queue management")
    pq_sub = pq.add_subparsers(dest="queue_command", required=True)
    pq_sub.add_parser("list", help="List queued actions")
    ppa = pq_sub.add_parser("approve", help="Approve a queued user")
    ppa.add_argument("username", help="Username to approve")
    pq_sub.add_parser("clear", help="Clear pending queue")

    # run (full cycle)
    p = sub.add_parser("run", help="Full cycle: discover → enrich → score → queue")
    p.add_argument("--users", nargs="+", help="Extra user seeds")
    p.add_argument("--search", help="Search query for additional seeds")
    p.add_argument("--limit", type=int, default=10, help="Max candidates to process")

    # follow / unfollow
    for cmd in ("follow", "unfollow"):
        p = sub.add_parser(cmd, help=f"Directly {cmd} a user")
        p.add_argument("username", help="GitHub username")

    # execute
    sub.add_parser("execute", help="Execute all approved actions in queue")

    # report
    pr = sub.add_parser("report", help="Generate reports")
    pr_sub = pr.add_subparsers(dest="report_command", required=True)
    pr_sub.add_parser("summary", help="Summary report")
    pru = pr_sub.add_parser("user", help="User profile report")
    pru.add_argument("username", help="Username")
    prm = pr_sub.add_parser("metrics", help="Metrics report")
    prm.add_argument("--days", type=int, default=7, help="Days of metrics to show")

    # stats
    sub.add_parser("stats", help="Quick stats")

    # config
    sub.add_parser("config", help="Show current configuration")

    args = parser.parse_args()

    # Override config from flags
    config = GrowthConfig.from_env()
    if args.live:
        config.dry_run = False
    if args.enabled:
        config.enabled = True

    # Now run through the CLI class with a synthetic text
    cli = GitHubGrowthCLI()
    # Re-parse with the cli's parser to get the full namespace
    cli_args = cli.parser.parse_args(sys.argv[1:])

    # Apply our overrides
    cli_args.live = args.live
    cli_args.enabled = args.enabled

    comps = cli._make_components(cli_args)
    try:
        if cli_args.command == "discover":
            result = cli._cmd_discover(cli_args, comps)
        elif cli_args.command == "enrich":
            result = cli._cmd_enrich(cli_args, comps)
        elif cli_args.command == "score":
            result = cli._cmd_score(cli_args, comps)
        elif cli_args.command == "queue":
            result = cli._cmd_queue(cli_args, comps)
        elif cli_args.command == "run":
            result = cli._cmd_run(cli_args, comps)
        elif cli_args.command in ("follow", "unfollow"):
            result = cli._cmd_follow_unfollow(cli_args, comps)
        elif cli_args.command == "execute":
            result = cli._cmd_execute(cli_args, comps)
        elif cli_args.command == "report":
            result = cli._cmd_report(cli_args, comps)
        elif cli_args.command == "stats":
            result = cli._cmd_stats(cli_args, comps)
        elif cli_args.command == "config":
            result = cli._cmd_config(cli_args, comps)
        else:
            result = {"ok": False, "output": f"Unknown command: {cli_args.command}", "action": "dispatch", "error": "unknown command"}
    finally:
        comps["store"].close()

    if result["ok"]:
        print(result.get("output", ""))
        sys.exit(0)
    else:
        print(f"error: {result.get('error', 'unknown')}", file=sys.stderr)
        sys.exit(1)
