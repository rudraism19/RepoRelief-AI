"""GitHub REST API client for RepoRelief AI.

This module provides read-only operations for fetching public GitHub issues,
parsing repository identifiers, and tracking API rate limits.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import requests


class GitHubClientError(Exception):
    """Base exception for GitHub client errors."""


class GitHubNotFoundError(GitHubClientError):
    """Raised when the repository does not exist or is private."""


class GitHubRateLimitError(GitHubClientError):
    """Raised when GitHub API rate limits have been exhausted."""

    def __init__(self, message: str, reset_timestamp: int | None = None) -> None:
        super().__init__(message)
        self.reset_timestamp = reset_timestamp


class GitHubAuthError(GitHubClientError):
    """Raised when an invalid authentication token is provided."""


class GitHubAPIError(GitHubClientError):
    """Raised for unexpected GitHub API or network errors."""


@dataclass(frozen=True)
class RepositoryRef:
    """Strongly-typed reference to a GitHub repository."""

    owner: str
    name: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass(frozen=True)
class GitHubRateLimit:
    """Current rate limit quota status."""

    limit: int
    remaining: int
    reset_timestamp: int

    @property
    def reset_time_formatted(self) -> str:
        try:
            dt = datetime.datetime.fromtimestamp(
                self.reset_timestamp, tz=datetime.timezone.utc
            )
            return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
        except (ValueError, OSError):
            return "Unknown"


@dataclass
class GitHubIssue:
    """Representation of an open GitHub issue (excluding pull requests)."""

    number: int
    title: str
    body: str
    author: str
    created_at: str
    updated_at: str
    html_url: str
    labels: list[str] = field(default_factory=list)
    comments_count: int = 0
    state: str = "open"

    @classmethod
    def from_api_dict(cls, data: dict[str, Any]) -> GitHubIssue:
        """Construct an issue instance from GitHub API JSON response."""
        labels = [
            lbl.get("name", "")
            for lbl in data.get("labels", [])
            if isinstance(lbl, dict) and lbl.get("name")
        ]
        user_info = data.get("user") or {}
        author = user_info.get("login", "unknown") if isinstance(user_info, dict) else "unknown"

        return cls(
            number=data.get("number", 0),
            title=data.get("title", "").strip(),
            body=(data.get("body") or "").strip(),
            author=author,
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            html_url=data.get("html_url", ""),
            labels=labels,
            comments_count=data.get("comments", 0),
            state=data.get("state", "open"),
        )


_OWNER_REGEX = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9]|-(?=[a-zA-Z0-9])){0,38}$")
_REPO_REGEX = re.compile(r"^[a-zA-Z0-9_.-]{1,100}$")


def parse_github_url(raw_input: str) -> RepositoryRef:
    """Parse and validate a GitHub repository URL or slug.

    Accepts formats:
      - 'owner/repo'
      - 'https://github.com/owner/repo'
      - 'http://github.com/owner/repo'
      - 'github.com/owner/repo'
      - 'git@github.com:owner/repo.git'
      - 'https://github.com/owner/repo/issues'

    Returns:
        RepositoryRef: Object containing validated owner and repository name.

    Raises:
        ValueError: If input format is invalid, uses an unsupported host, or contains illegal characters.
    """
    cleaned = (raw_input or "").strip()
    if not cleaned:
        raise ValueError("Repository identifier cannot be empty.")

    # Handle SSH git URLs (e.g. git@github.com:owner/repo.git)
    ssh_match = re.match(r"^git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$", cleaned)
    if ssh_match:
        owner, name = ssh_match.group(1), ssh_match.group(2)
        return _validate_repo_ref(owner, name)

    # Add scheme if missing for URL parsing
    if not cleaned.startswith(("http://", "https://")):
        # If it looks like 'github.com/owner/repo'
        if cleaned.startswith("github.com/"):
            cleaned = "https://" + cleaned
        elif "/" in cleaned and not cleaned.startswith(("/", "\\")):
            # Might be 'owner/repo' directly
            parts = [p for p in cleaned.split("/") if p]
            if len(parts) >= 2:
                owner, name = parts[0], parts[1]
                if name.endswith(".git"):
                    name = name[:-4]
                return _validate_repo_ref(owner, name)

    # Standard URL parsing
    try:
        parsed = urlparse(cleaned)
    except Exception as exc:
        raise ValueError(f"Could not parse repository URL: {exc}") from exc

    if parsed.netloc:
        netloc = parsed.netloc.lower()
        if netloc not in ("github.com", "www.github.com"):
            raise ValueError(
                f"Unsupported host '{parsed.netloc}'. RepoRelief AI only supports public repositories hosted on github.com."
            )

    path = parsed.path.strip("/")
    parts = [segment for segment in path.split("/") if segment]

    if len(parts) < 2:
        raise ValueError(
            f"Invalid repository path '{parsed.path}'. Expected format 'owner/repo' or 'https://github.com/owner/repo'."
        )

    owner = parts[0]
    name = parts[1]

    if name.endswith(".git"):
        name = name[:-4]

    return _validate_repo_ref(owner, name)


def _validate_repo_ref(owner: str, name: str) -> RepositoryRef:
    """Validate GitHub owner and repo name against standard constraints."""
    owner = owner.strip()
    name = name.strip()

    if not _OWNER_REGEX.match(owner):
        raise ValueError(
            f"Invalid GitHub owner name '{owner}'. Owner names must be 1-39 alphanumeric characters, cannot start or end with a hyphen."
        )

    if not _REPO_REGEX.match(name) or name in (".", ".."):
        raise ValueError(
            f"Invalid GitHub repository name '{name}'. Repository names must be 1-100 characters and cannot be '.' or '..'."
        )

    return RepositoryRef(owner=owner, name=name)


class GitHubClient:
    """Read-only client for interacting with the public GitHub REST API."""

    BASE_URL = "https://api.github.com"

    def __init__(
        self,
        token: str | None = None,
        timeout: float = 15.0,
        session: requests.Session | None = None,
    ) -> None:
        """Initialize the client.

        Args:
            token: Optional GitHub Personal Access Token for higher rate limits.
            timeout: Network timeout in seconds.
            session: Optional custom requests session (useful for test mocking).
        """
        self.token = token.strip() if token and token.strip() else None
        self.timeout = timeout
        self.session = session or requests.Session()
        self._last_rate_limit: GitHubRateLimit | None = None

    @property
    def last_rate_limit(self) -> GitHubRateLimit | None:
        """Return the most recently observed rate limit quota."""
        return self._last_rate_limit

    def _get_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "RepoRelief-AI/1.0 (Open-Source Issue Triage Assistant)",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _extract_rate_limit(self, headers: Any) -> GitHubRateLimit:
        try:
            limit = int(headers.get("X-RateLimit-Limit", 60))
            remaining = int(headers.get("X-RateLimit-Remaining", 60))
            reset_ts = int(headers.get("X-RateLimit-Reset", 0))
            rate_info = GitHubRateLimit(
                limit=limit, remaining=remaining, reset_timestamp=reset_ts
            )
            self._last_rate_limit = rate_info
            return rate_info
        except (ValueError, TypeError):
            fallback = GitHubRateLimit(limit=60, remaining=60, reset_timestamp=0)
            self._last_rate_limit = fallback
            return fallback

    def get_rate_limit(self) -> GitHubRateLimit:
        """Query the GitHub API for current rate limit quota.

        Returns:
            GitHubRateLimit object.

        Raises:
            GitHubClientError on communication error.
        """
        url = f"{self.BASE_URL}/rate_limit"
        try:
            response = self.session.get(
                url, headers=self._get_headers(), timeout=self.timeout
            )
            self._extract_rate_limit(response.headers)
            if response.status_code == 200:
                data = response.json()
                core = data.get("resources", {}).get("core", {})
                rate_info = GitHubRateLimit(
                    limit=core.get("limit", 60),
                    remaining=core.get("remaining", 60),
                    reset_timestamp=core.get("reset", 0),
                )
                self._last_rate_limit = rate_info
                return rate_info
            elif response.status_code == 401:
                raise GitHubAuthError("GitHub token provided is invalid or expired.")
            else:
                return self._last_rate_limit or GitHubRateLimit(60, 60, 0)
        except requests.exceptions.RequestException as exc:
            raise GitHubAPIError(f"Network error querying GitHub rate limits: {exc}") from exc

    def fetch_issues(
        self,
        owner: str,
        repo: str,
        max_issues: int = 30,
        state: str = "open",
    ) -> tuple[list[GitHubIssue], GitHubRateLimit]:
        """Fetch open issues for a public repository.

        Excludes pull requests (which GitHub returns on the issues endpoint).

        Args:
            owner: Repository owner/organization.
            repo: Repository name.
            max_issues: Maximum number of actual issues to retrieve.
            state: Issue state ('open', 'closed', 'all'). Defaults to 'open'.

        Returns:
            Tuple of (list of GitHubIssue, GitHubRateLimit).

        Raises:
            GitHubNotFoundError: If repository doesn't exist or is inaccessible.
            GitHubRateLimitError: If API rate limits are exhausted.
            GitHubAuthError: If authentication failed.
            GitHubAPIError: For other API or network errors.
        """
        issues: list[GitHubIssue] = []
        page = 1
        per_page = min(max(max_issues, 10), 100)
        rate_info = GitHubRateLimit(limit=60, remaining=60, reset_timestamp=0)

        while len(issues) < max_issues:
            url = f"{self.BASE_URL}/repos/{owner}/{repo}/issues"
            params = {
                "state": state,
                "per_page": per_page,
                "page": page,
                "sort": "updated",
                "direction": "desc",
            }

            try:
                response = self.session.get(
                    url,
                    headers=self._get_headers(),
                    params=params,
                    timeout=self.timeout,
                )
            except requests.exceptions.Timeout as exc:
                raise GitHubAPIError(
                    f"Connection timed out while fetching issues from {owner}/{repo}. Check your network."
                ) from exc
            except requests.exceptions.RequestException as exc:
                raise GitHubAPIError(
                    f"Network error fetching issues from {owner}/{repo}: {exc}"
                ) from exc

            rate_info = self._extract_rate_limit(response.headers)

            if response.status_code == 200:
                data = response.json()
                if not isinstance(data, list) or len(data) == 0:
                    # No more issues available
                    break

                for item in data:
                    if not isinstance(item, dict):
                        continue
                    # CRITICAL: GitHub's /issues endpoint returns both issues and pull requests.
                    # Pull requests always include a 'pull_request' key in the JSON object.
                    if "pull_request" in item:
                        continue

                    issues.append(GitHubIssue.from_api_dict(item))
                    if len(issues) >= max_issues:
                        break

                # If GitHub returns fewer items than the requested per_page, there are no more pages.
                if len(data) < per_page:
                    break

                page += 1

            elif response.status_code == 404:
                raise GitHubNotFoundError(
                    f"Repository '{owner}/{repo}' not found. Please verify the URL or ensure the repository is public."
                )
            elif response.status_code in (403, 429):
                err_msg = response.json().get("message", "API rate limit exceeded.") if response.content else "Rate limit exceeded"
                raise GitHubRateLimitError(
                    f"GitHub API rate limit reached: {err_msg}. "
                    f"Resets at {rate_info.reset_time_formatted}. "
                    "Consider providing an optional GitHub Personal Access Token to raise the limit.",
                    reset_timestamp=rate_info.reset_timestamp,
                )
            elif response.status_code == 401:
                raise GitHubAuthError(
                    "Invalid or expired GitHub Personal Access Token provided."
                )
            else:
                detail = response.text[:200] if response.text else "No details"
                raise GitHubAPIError(
                    f"GitHub API returned error HTTP {response.status_code}: {detail}"
                )

        return issues[:max_issues], rate_info
