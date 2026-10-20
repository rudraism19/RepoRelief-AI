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
        with pytest.raises(GitHubAuthError, match="invalid or expired"):
            client.fetch_issues("owner", "repo")

    def test_fetch_issues_network_timeout(self):
        mock_session = MagicMock()
        mock_session.get.side_effect = requests.exceptions.Timeout("Connection timed out")

        client = GitHubClient(session=mock_session)
        with pytest.raises(GitHubAPIError, match="timed out"):
            client.fetch_issues("owner", "repo")

    def test_fetch_issues_connection_error(self):
        mock_session = MagicMock()
        mock_session.get.side_effect = requests.exceptions.ConnectionError("Failed to resolve host")

        client = GitHubClient(session=mock_session)
        with pytest.raises(GitHubAPIError, match="Failed to connect"):
            client.fetch_issues("owner", "repo")

    def test_fetch_issues_empty_repository(self):
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"X-RateLimit-Limit": "60", "X-RateLimit-Remaining": "59"}
        mock_response.json.return_value = []
        mock_session.get.return_value = mock_response

        client = GitHubClient(session=mock_session)
        issues, rate_info = client.fetch_issues("owner", "empty-repo")
        assert issues == []
        assert rate_info.remaining == 59

    def test_fetch_issues_only_pull_requests(self):
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"X-RateLimit-Limit": "60", "X-RateLimit-Remaining": "59"}
        # Both items are pull requests
        mock_response.json.return_value = [
            {"number": 1, "title": "PR 1", "body": "fix", "pull_request": {}},
            {"number": 2, "title": "PR 2", "body": "doc", "pull_request": {}},
        ]
        mock_session.get.return_value = mock_response

        client = GitHubClient(session=mock_session)
        issues, rate_info = client.fetch_issues("owner", "repo")
        assert issues == []

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


class TestAuthenticationModes:
    """Mocked tests for both authenticated and unauthenticated request flows."""

    def test_unauthenticated_request_headers_and_params(self):
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"X-RateLimit-Limit": "60", "X-RateLimit-Remaining": "59"}
        mock_resp.json.return_value = []
        mock_session.get.return_value = mock_resp

        # Explicitly initialize with no token
        client = GitHubClient(token=None, session=mock_session)
        assert client.is_authenticated is False
        assert client.token is None

        client.fetch_issues("pallets", "flask", max_issues=10)

        mock_session.get.assert_called_once()
        called_args, called_kwargs = mock_session.get.call_args

        # Verify URL, GET params, and headers
        assert called_args[0] == "https://api.github.com/repos/pallets/flask/issues"
        params = called_kwargs.get("params", {})
        assert params.get("state") == "open"
        assert params.get("per_page") == 100

        headers = called_kwargs.get("headers", {})
        assert "Authorization" not in headers
        assert "User-Agent" in headers
        assert headers.get("Accept") == "application/vnd.github.v3+json"

    def test_authenticated_request_with_token(self):
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"X-RateLimit-Limit": "5000", "X-RateLimit-Remaining": "4999"}
        mock_resp.json.return_value = []
        mock_session.get.return_value = mock_resp

        client = GitHubClient(token="ghp_fake_token_for_test", session=mock_session)
        assert client.is_authenticated is True
        assert client.token == "ghp_fake_token_for_test"

        client.fetch_issues("psf", "requests", max_issues=10)

        called_args, called_kwargs = mock_session.get.call_args
        headers = called_kwargs.get("headers", {})
        assert headers.get("Authorization") == "Bearer ghp_fake_token_for_test"

    def test_env_token_used_when_available(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_env_mock_token")
        client = GitHubClient()
        assert client.is_authenticated is True
        assert client.token == "ghp_env_mock_token"

    def test_no_env_token_defaults_to_unauthenticated(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        client = GitHubClient()
        assert client.is_authenticated is False
        assert client.token is None

    def test_empty_string_token_treated_as_unauthenticated(self):
        client_empty = GitHubClient(token="")
        assert client_empty.is_authenticated is False
        assert client_empty.token is None
        assert "Authorization" not in client_empty._get_headers()

        client_whitespace = GitHubClient(token="   ")
        assert client_whitespace.is_authenticated is False
        assert client_whitespace.token is None
        assert "Authorization" not in client_whitespace._get_headers()

    def test_unauthenticated_request_handles_rate_limit_error(self):
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.headers = {
            "X-RateLimit-Limit": "60",
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": "1700000000",
        }
        mock_resp.content = b'{"message": "API rate limit exceeded for unauthenticated user"}'
        mock_resp.json.return_value = {"message": "API rate limit exceeded for unauthenticated user"}
        mock_session.get.return_value = mock_resp

        client = GitHubClient(token=None, session=mock_session)
        with pytest.raises(GitHubRateLimitError, match="rate limit reached"):
            client.fetch_issues("pallets", "flask")

    def test_unauthenticated_request_handles_not_found(self):
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.headers = {}
        mock_session.get.return_value = mock_resp

        client = GitHubClient(token=None, session=mock_session)
        with pytest.raises(GitHubNotFoundError, match="was not found"):
            client.fetch_issues("nonexistent_user", "nonexistent_repo")


class TestArbitraryRepositoriesAndPagination:
    """Tests verifying support for any arbitrary public repository and multi-page pagination."""

    @pytest.mark.parametrize(
        "raw_input,expected_owner,expected_repo",
        [
            ("torvalds/linux", "torvalds", "linux"),
            ("facebook/react", "facebook", "react"),
            ("rust-lang/rust", "rust-lang", "rust"),
            ("kubernetes/kubernetes", "kubernetes", "kubernetes"),
            ("my-custom-org/my-public-repo", "my-custom-org", "my-public-repo"),
            ("user123/dot.project.name", "user123", "dot.project.name"),
            ("https://github.com/microsoft/vscode", "microsoft", "vscode"),
            ("https://github.com/angular/angular/issues", "angular", "angular"),
        ],
    )
    def test_parse_arbitrary_public_repositories(self, raw_input, expected_owner, expected_repo):
        ref = parse_github_url(raw_input)
        assert ref.owner == expected_owner
        assert ref.name == expected_repo
        assert ref.full_name == f"{expected_owner}/{expected_repo}"

    def test_fetch_issues_arbitrary_custom_repo(self):
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"X-RateLimit-Limit": "60", "X-RateLimit-Remaining": "59"}
        mock_resp.json.return_value = [{"number": 101, "title": "Custom Bug", "body": "Details"}]
        mock_session.get.return_value = mock_resp

        client = GitHubClient(token=None, session=mock_session)
        issues, _ = client.fetch_issues("any-user-worldwide", "any-custom-project")

        assert len(issues) == 1
        assert issues[0].number == 101
        call_url = mock_session.get.call_args[0][0]
        assert call_url == "https://api.github.com/repos/any-user-worldwide/any-custom-project/issues"

    def test_pagination_more_than_100_issues(self):
        mock_session = MagicMock()

        # Generate page 1 with 100 items (all issues)
        page1_data = [{"number": i, "title": f"Issue {i}", "body": "Body"} for i in range(1, 101)]
        # Generate page 2 with 100 items (all issues)
        page2_data = [{"number": i, "title": f"Issue {i}", "body": "Body"} for i in range(101, 201)]

        resp1 = MagicMock()
        resp1.status_code = 200
        resp1.headers = {"X-RateLimit-Limit": "60", "X-RateLimit-Remaining": "50"}
        resp1.json.return_value = page1_data

        resp2 = MagicMock()
        resp2.status_code = 200
        resp2.headers = {"X-RateLimit-Limit": "60", "X-RateLimit-Remaining": "49"}
        resp2.json.return_value = page2_data

        mock_session.get.side_effect = [resp1, resp2]

        client = GitHubClient(token=None, session=mock_session)
        # Request 150 issues (requires 2 pages)
        issues, _ = client.fetch_issues("large-org", "massive-repo", max_issues=150)

        assert len(issues) == 150
        assert mock_session.get.call_count == 2

        # Verify first call had page=1 and per_page=100
        first_call_params = mock_session.get.call_args_list[0][1]["params"]
        assert first_call_params["page"] == 1
        assert first_call_params["per_page"] == 100
        assert first_call_params["state"] == "open"

        # Verify second call had page=2 and per_page=100
        second_call_params = mock_session.get.call_args_list[1][1]["params"]
        assert second_call_params["page"] == 2
        assert second_call_params["per_page"] == 100
        assert second_call_params["state"] == "open"

    def test_pagination_breaks_on_partial_page(self):
        mock_session = MagicMock()

        # Page 1: 100 items
        page1 = [{"number": i, "title": f"Issue {i}", "body": "Body"} for i in range(1, 101)]
        # Page 2: 30 items (< 100, signifies last page)
        page2 = [{"number": i, "title": f"Issue {i}", "body": "Body"} for i in range(101, 131)]

        resp1 = MagicMock(status_code=200, headers={}, json=lambda: page1)
        resp2 = MagicMock(status_code=200, headers={}, json=lambda: page2)
        mock_session.get.side_effect = [resp1, resp2]

        client = GitHubClient(token=None, session=mock_session)
        # Requesting 200, but only 130 exist in repo
        issues, _ = client.fetch_issues("org", "repo", max_issues=200)

        assert len(issues) == 130
        assert mock_session.get.call_count == 2
