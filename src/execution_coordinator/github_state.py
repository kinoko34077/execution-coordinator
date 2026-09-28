from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener


class GitHubApiError(RuntimeError):
    """GitHub API request failed without exposing credentials."""


class StateStoreError(GitHubApiError):
    """Runtime state authority identity could not be proven safely."""


@dataclass(frozen=True, slots=True)
class IssueBodyRead:
    """One read-only GitHub Issue body plus its source freshness timestamp."""

    body: str
    updated_at: str


class _NoRedirectHandler(HTTPRedirectHandler):
    """Fail closed instead of following authority-changing GitHub redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _validate_updated_at(value: object) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise GitHubApiError("GitHub issue response did not contain a valid updated_at")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise GitHubApiError("GitHub issue response did not contain a valid updated_at") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise GitHubApiError("GitHub issue response did not contain a valid updated_at")
    return value


def _validate_issue_identity(
    payload: dict[str, object],
    *,
    repository: str,
    issue_number: int,
    api_base_url: str,
) -> None:
    expected_html_url = f"https://github.com/{repository}/issues/{issue_number}"
    expected_repository_url = f"{api_base_url}/repos/{repository}"
    if payload.get("number") != issue_number:
        raise StateStoreError("GitHub state authority issue number did not match")
    if payload.get("html_url") != expected_html_url:
        raise StateStoreError("GitHub state authority repository identity did not match")
    if payload.get("repository_url") != expected_repository_url:
        raise StateStoreError("GitHub state authority repository identity did not match")


def _validate_comment_identity(
    payload: dict[str, object],
    *,
    repository: str,
    issue_number: int,
    issue_url: str,
) -> None:
    expected_html_prefix = f"https://github.com/{repository}/issues/{issue_number}#issuecomment-"
    if payload.get("issue_url") != issue_url:
        raise StateStoreError("GitHub state authority comment target did not match")
    html_url = payload.get("html_url")
    if not isinstance(html_url, str) or not html_url.startswith(expected_html_prefix):
        raise StateStoreError("GitHub state authority comment target did not match")


class GitHubStateStore:
    def __init__(
        self,
        *,
        token: str,
        repository: str,
        issue_number: int,
        api_base_url: str = "https://api.github.com",
        timeout_seconds: float = 20.0,
    ) -> None:
        if "/" not in repository:
            raise ValueError("repository must be owner/name")
        if issue_number < 1:
            raise ValueError("issue_number must be positive")
        self._token = token
        self._repository = repository
        self._issue_number = issue_number
        self._api_base_url = api_base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._opener = build_opener(_NoRedirectHandler()).open

    @property
    def issue_url(self) -> str:
        owner, name = self._repository.split("/", 1)
        return (
            f"{self._api_base_url}/repos/{quote(owner, safe='')}/"
            f"{quote(name, safe='')}/issues/{self._issue_number}"
        )

    def _request(self, method: str, url: str, payload: dict[str, object] | None = None) -> object:
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "execution-coordinator/0.1",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener(request, timeout=self._timeout_seconds) as response:
                raw = response.read()
                status = response.status
                final_url = response.geturl()
        except HTTPError as exc:
            if 300 <= exc.code < 400:
                raise StateStoreError(
                    "GitHub API redirected away from the configured state authority"
                ) from exc
            raise GitHubApiError(f"GitHub API {method} failed with HTTP {exc.code}") from exc
        except URLError as exc:
            raise GitHubApiError(f"GitHub API {method} transport failure") from exc
        if not (200 <= status < 300):
            raise GitHubApiError(f"GitHub API {method} failed with HTTP {status}")
        if final_url != url:
            raise StateStoreError("GitHub API final URL did not match the configured state authority")
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitHubApiError("GitHub API returned invalid JSON") from exc

    def _validate_issue_payload(self, payload: object) -> dict[str, object]:
        if not isinstance(payload, dict):
            raise GitHubApiError("GitHub issue response was not an object")
        _validate_issue_identity(
            payload,
            repository=self._repository,
            issue_number=self._issue_number,
            api_base_url=self._api_base_url,
        )
        return payload

    def load_body(self) -> str:
        payload = self._validate_issue_payload(self._request("GET", self.issue_url))
        if not isinstance(payload.get("body"), str):
            raise GitHubApiError("GitHub issue response did not contain a string body")
        return payload["body"]

    def load_body_with_metadata(self) -> IssueBodyRead:
        payload = self._validate_issue_payload(self._request("GET", self.issue_url))
        if not isinstance(payload.get("body"), str):
            raise GitHubApiError("GitHub issue response did not contain a string body")
        return IssueBodyRead(
            body=payload["body"],
            updated_at=_validate_updated_at(payload.get("updated_at")),
        )

    def save_body(self, body: str) -> None:
        payload = self._validate_issue_payload(
            self._request("PATCH", self.issue_url, {"body": body})
        )
        if payload.get("body") != body:
            raise GitHubApiError("GitHub issue update did not confirm the requested body")

    def add_comment(self, body: str) -> None:
        url = f"{self.issue_url}/comments"
        payload = self._request("POST", url, {"body": body})
        if not isinstance(payload, dict):
            raise GitHubApiError("GitHub comment response was not an object")
        _validate_comment_identity(
            payload,
            repository=self._repository,
            issue_number=self._issue_number,
            issue_url=self.issue_url,
        )
        if not isinstance(payload.get("id"), int):
            raise GitHubApiError("GitHub comment response did not confirm creation")
