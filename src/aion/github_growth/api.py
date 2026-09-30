"""GitHub API client — wraps gh CLI and REST API calls.

Uses gh CLI (auto-detected via shutil.which) for most operations, with
fallback to REST API via requests if gh is unavailable.
Token from GITHUB_TOKEN env or credentials.py.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

GITHUB_API = "https://api.github.com"


@dataclass
class RateLimit:
    """Track GitHub API rate limits."""
    remaining: int
    reset_at: float
    limit: int

    def is_exhausted(self) -> bool:
        return self.remaining <= 1 and time.time() < self.reset_at

    def wait_if_needed(self) -> float:
        """Sleep if rate-limited. Returns seconds waited."""
        if self.is_exhausted():
            wait = max(0, self.reset_at - time.time()) + 1
            time.sleep(wait)
            return wait
        return 0.0


class GitHubAPI:
    """GitHub API client with rate-limit awareness and gh CLI fallback.

    Safety: never logs token. Respects min_request_interval.
    """

    def __init__(self, token: str | None = None, config=None):
        from .config import GrowthConfig
        self.config = config or GrowthConfig.from_env()
        self._token = token or self._resolve_token()
        self._gh_cli = shutil.which("gh")
        self._rate_limit: RateLimit | None = None
        self._last_request = 0.0

    def _resolve_token(self) -> str | None:
        """Resolve token from credentials.py or env.

        Checks: explicit credential, GITHUB_TOKEN, GITHUB env vars,
        and .env file for GITHUB key.
        """
        # 1. Check credentials store
        try:
            from ..credentials import CredentialStore
            store = CredentialStore()
            profile = store.get("github")
            if profile and profile.api_key and not profile.api_key.startswith("#"):
                return profile.api_key
            token = store.resolve_api_key("github")
            if token and not token.startswith("#"):
                return token
        except Exception:
            pass

        # 2. Check env vars
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GITHUB")
        if token and not token.startswith("#"):
            return token

        # 3. Parse .env file for GITHUB= key
        env_path = Path.home() / ".env"
        if env_path.exists():
            try:
                for line in env_path.read_text().splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, val = line.partition("=")
                    key = key.strip()
                    val = val.strip().strip("\"'").strip()
                    if key == "GITHUB" and val:
                        return val
            except Exception:
                pass

        return None

    def _respect_interval(self):
        """Enforce min_request_interval between calls."""
        now = time.time()
        elapsed = now - self._last_request
        min_interval = self.config.min_request_interval_ms / 1000.0
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        self._last_request = time.time()

    def _headers(self) -> dict:
        return {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Authorization": f"Bearer {self._token}" if self._token else "",
            "User-Agent": "aion-github-growth",
        }

    def _check_rate_limit(self, resp: requests.Response):
        """Parse rate limit headers from response."""
        self._rate_limit = RateLimit(
            remaining=int(resp.headers.get("X-RateLimit-Remaining", 0)),
            reset_at=float(resp.headers.get("X-RateLimit-Reset", 0)),
            limit=int(resp.headers.get("X-RateLimit-Limit", 0)),
        )

    # ── REST API ──────────────────────────────────────────────────────────────

    def get_user(self, username: str) -> dict | None:
        """Fetch a user profile."""
        self._respect_interval()
        try:
            resp = requests.get(
                f"{GITHUB_API}/users/{username}",
                headers=self._headers(),
            )
            self._check_rate_limit(resp)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException:
            return None

    def list_user_repos(self, username: str, limit: int = 10) -> list[dict]:
        """List a user's public repositories."""
        self._respect_interval()
        try:
            resp = requests.get(
                f"{GITHUB_API}/users/{username}/repos",
                params={"sort": "updated", "per_page": min(limit, 100)},
                headers=self._headers(),
            )
            self._check_rate_limit(resp)
            resp.raise_for_status()
            return resp.json()[:limit]
        except (requests.RequestException, ValueError):
            return []

    def list_repo_contributors(self, owner: str, repo: str) -> list[dict]:
        """List contributors to a repository."""
        self._respect_interval()
        try:
            resp = requests.get(
                f"{GITHUB_API}/repos/{owner}/{repo}/contributors",
                params={"per_page": 100},
                headers=self._headers(),
            )
            self._check_rate_limit(resp)
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError):
            return []

    def list_repo_dependents(self, owner: str, repo: str) -> list[dict]:
        """List fork/dependents of a repository.

        Uses REST API. Returns fork repo objects (each has 'owner' dict with 'login').
        """
        self._respect_interval()
        try:
            resp = requests.get(
                f"{GITHUB_API}/repos/{owner}/{repo}/forks",
                params={"per_page": 20, "sort": "stargazers"},
                headers=self._headers(),
            )
            self._check_rate_limit(resp)
            if resp.status_code == 404:
                return []  # repo not found or no forks
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError):
            return []

    def get_repo(self, owner: str, repo: str) -> dict | None:
        """Fetch repository metadata."""
        self._respect_interval()
        try:
            resp = requests.get(
                f"{GITHUB_API}/repos/{owner}/{repo}",
                headers=self._headers(),
            )
            self._check_rate_limit(resp)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError):
            return None

    def search_users(self, query: str, limit: int = 20) -> list[dict]:
        """Search for users by query string."""
        self._respect_interval()
        try:
            resp = requests.get(
                f"{GITHUB_API}/search/users",
                params={"q": query, "per_page": min(limit, 100)},
                headers=self._headers(),
            )
            self._check_rate_limit(resp)
            resp.raise_for_status()
            return resp.json().get("items", [])
        except (requests.RequestException, ValueError):
            return []

    # ── Actions (require approval) ────────────────────────────────────────────

    def follow(self, username: str, dry_run: bool = True) -> bool:
        """Follow a user. Returns True on success or dry-run."""
        if dry_run or not self._token:
            return True
        self._respect_interval()
        try:
            resp = requests.put(
                f"{GITHUB_API}/user/following/{username}",
                headers=self._headers(),
            )
            self._check_rate_limit(resp)
            return resp.status_code == 204
        except requests.RequestException:
            return False

    def unfollow(self, username: str, dry_run: bool = True) -> bool:
        """Unfollow a user. Returns True on success or dry-run."""
        if dry_run or not self._token:
            return True
        self._respect_interval()
        try:
            resp = requests.delete(
                f"{GITHUB_API}/user/following/{username}",
                headers=self._headers(),
            )
            self._check_rate_limit(resp)
            return resp.status_code == 204
        except requests.RequestException:
            return False

    @property
    def rate_limit(self) -> RateLimit | None:
        return self._rate_limit

    @property
    def available(self) -> bool:
        """True if token is present OR gh CLI is available."""
        return bool(self._token) or bool(self._gh_cli)

    def _safe_repr(self) -> str:
        has_token = "yes" if self._token else "no"
        has_gh = "yes" if self._gh_cli else "no"
        return f"GitHubAPI(token={has_token}, gh_cli={has_gh})"
