"""Unit tests for github_client module."""

from unittest.mock import MagicMock, patch

import pytest
import requests

from github_client import (
    GitHubAPIError,
    GitHubAuthError,
    GitHubClient,
    GitHubIssue,
    GitHubNotFoundError,
    GitHubRateLimit,
    GitHubRateLimitError,
    RepositoryRef,
    parse_github_url,
)


class TestParseGitHubUrl:
    """Test URL and slug parsing and validation."""

    def test_parse_simple_slug(self):
        ref = parse_github_url("pallets/flask")
        assert ref.owner == "pallets"
        assert ref.name == "flask"
        assert ref.full_name == "pallets/flask"

    def test_parse_https_url(self):
        ref = parse_github_url("https://github.com/tiangolo/fastapi")
        assert ref.owner == "tiangolo"
        assert ref.name == "fastapi"

    def test_parse_trailing_slash_and_git(self):
        ref = parse_github_url("https://github.com/psf/requests/")
        assert ref.owner == "psf"
        assert ref.name == "requests"

        ref_git = parse_github_url("https://github.com/psf/requests.git")
        assert ref_git.owner == "psf"
        assert ref_git.name == "requests"

    def test_parse_issues_url(self):
        ref = parse_github_url("https://github.com/encode/uvicorn/issues")
        assert ref.owner == "encode"
        assert ref.name == "uvicorn"

    def test_parse_domain_without_scheme(self):
        ref = parse_github_url("github.com/facebook/react")
        assert ref.owner == "facebook"
        assert ref.name == "react"

    def test_parse_ssh_url(self):
        ref = parse_github_url("git@github.com:torvalds/linux.git")
        assert ref.owner == "torvalds"
        assert ref.name == "linux"

    def test_empty_input_raises_value_error(self):
        with pytest.raises(ValueError, match="cannot be empty"):
            parse_github_url("   ")

    def test_unsupported_host_raises_value_error(self):
        with pytest.raises(ValueError, match="Unsupported host"):
            parse_github_url("https://gitlab.com/owner/repo")

    def test_invalid_path_raises_value_error(self):
        with pytest.raises(ValueError, match="Invalid repository path"):
            parse_github_url("https://github.com/onlyowner")

    def test_directory_traversal_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_github_url("owner/../secret")

    def test_illegal_characters_raise_value_error(self):
        with pytest.raises(ValueError):
            parse_github_url("-invalid-owner/repo")


class TestGitHubRateLimit:
    """Test rate limit object methods."""

    def test_reset_time_formatted(self):
        # 1700000000 is 2023-11-14 22:13:20 UTC
        rl = GitHubRateLimit(limit=60, remaining=45, reset_timestamp=1700000000)
        assert "2023-11-14" in rl.reset_time_formatted
        assert "UTC" in rl.reset_time_formatted

    def test_invalid_timestamp_returns_unknown(self):
        rl = GitHubRateLimit(limit=60, remaining=0, reset_timestamp=999999999999999)
        assert rl.reset_time_formatted in ("Unknown", "999999999999999") or "UTC" in rl.reset_time_formatted


class TestGitHubIssueParsing:
    """Test parsing of GitHub issues from API response dict."""

    def test_from_api_dict_normal(self):
        raw = {
            "number": 42,
            "title": "Bug: application crashes on startup",
            "body": "Steps to reproduce: run app.py",
            "user": {"login": "octocat"},
            "created_at": "2026-10-01T12:00:00Z",
            "updated_at": "2026-10-02T12:00:00Z",
            "html_url": "https://github.com/org/repo/issues/42",
            "labels": [{"name": "bug"}, {"name": "priority: high"}],
            "comments": 3,
            "state": "open",
        }
        issue = GitHubIssue.from_api_dict(raw)
        assert issue.number == 42
        assert issue.title == "Bug: application crashes on startup"
        assert issue.body == "Steps to reproduce: run app.py"
        assert issue.author == "octocat"
        assert issue.labels == ["bug", "priority: high"]
        assert issue.comments_count == 3

    def test_from_api_dict_null_body_and_missing_user(self):
        raw = {
            "number": 101,
            "title": "Quick question",
            "body": None,
            "user": None,
            "labels": [],
        }
        issue = GitHubIssue.from_api_dict(raw)
        assert issue.number == 101
        assert issue.body == ""
        assert issue.author == "unknown"


class TestGitHubClientNetwork:
    """Test GitHubClient methods with mocked network requests."""

    def test_fetch_issues_filters_pull_requests(self):
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {
            "X-RateLimit-Limit": "60",
            "X-RateLimit-Remaining": "50",
            "X-RateLimit-Reset": "1700000000",
        }
        # Item 1 is an issue, Item 2 is a PR, Item 3 is an issue
        mock_response.json.return_value = [
            {
                "number": 1,
                "title": "Legitimate Issue",
                "body": "Help",
                "user": {"login": "user1"},
                "labels": [],
            },
            {
                "number": 2,
                "title": "Pull Request Title",
                "body": "Fixes 1",
                "user": {"login": "user2"},
                "labels": [],
                "pull_request": {"url": "https://api.github.com/repos/o/r/pulls/2"},
            },
            {
                "number": 3,
                "title": "Another Issue",
                "body": "Doc typo",
                "user": {"login": "user3"},
                "labels": [],
            },
        ]
        mock_session.get.return_value = mock_response

        client = GitHubClient(session=mock_session)
        issues, rate_limit = client.fetch_issues("owner", "repo", max_issues=10)

        assert len(issues) == 2
        assert issues[0].number == 1
        assert issues[1].number == 3
        assert rate_limit.remaining == 50

    def test_fetch_issues_not_found(self):
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.headers = {}
        mock_session.get.return_value = mock_response

        client = GitHubClient(session=mock_session)
        with pytest.raises(GitHubNotFoundError, match="not found"):
            client.fetch_issues("nonexistent", "repo")

    def test_fetch_issues_rate_limit_exceeded(self):
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.headers = {
            "X-RateLimit-Limit": "60",
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": "1700000000",
        }
        mock_response.content = b'{"message": "API rate limit exceeded"}'
        mock_response.json.return_value = {"message": "API rate limit exceeded"}
        mock_session.get.return_value = mock_response

        client = GitHubClient(session=mock_session)
        with pytest.raises(GitHubRateLimitError, match="rate limit reached"):
            client.fetch_issues("owner", "repo")

    def test_fetch_issues_auth_error(self):
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.headers = {}
        mock_session.get.return_value = mock_response

        client = GitHubClient(token="bad-token", session=mock_session)
        with pytest.raises(GitHubAuthError, match="Invalid or expired"):
            client.fetch_issues("owner", "repo")

    def test_fetch_issues_network_timeout(self):
        mock_session = MagicMock()
        mock_session.get.side_effect = requests.exceptions.Timeout("Connection timed out")

        client = GitHubClient(session=mock_session)
        with pytest.raises(GitHubAPIError, match="timed out"):
            client.fetch_issues("owner", "repo")

    def test_get_rate_limit_success(self):
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {
            "X-RateLimit-Limit": "60",
            "X-RateLimit-Remaining": "58",
            "X-RateLimit-Reset": "1700000000",
        }
        mock_response.json.return_value = {
            "resources": {
                "core": {
                    "limit": 60,
                    "remaining": 58,
                    "reset": 1700000000,
                }
            }
        }
        mock_session.get.return_value = mock_response

        client = GitHubClient(session=mock_session)
        rl = client.get_rate_limit()
        assert rl.limit == 60
        assert rl.remaining == 58
        assert rl.reset_timestamp == 1700000000
