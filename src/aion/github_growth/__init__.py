"""github_growth — legitimate, explainable GitHub profile/project discovery and engagement.

Non-spam, approval-gated, with kill switch and rate-limit awareness.
All actions require explicit approval (dry-run flag, max actions/day, allowlist/blocklist).
"""
from __future__ import annotations

from .config import GrowthConfig
from .api import GitHubAPI
from .store_sqlite import GitHubStore
from .discover import SeedDiscoverer
from .enrich import ProfileEnricher
from .score import Scorer
from .queue import ActionQueue
from .actions import ActionExecutor
from .metrics import MetricsTracker
from .graph import NetworkGraph
from .report import ReportGenerator
from .cli import GitHubGrowthCLI

__all__ = [
    "GrowthConfig",
    "GitHubAPI",
    "GitHubStore",
    "SeedDiscoverer",
    "ProfileEnricher",
    "Scorer",
    "ActionQueue",
    "ActionExecutor",
    "MetricsTracker",
    "NetworkGraph",
    "ReportGenerator",
    "GitHubGrowthCLI",
]
