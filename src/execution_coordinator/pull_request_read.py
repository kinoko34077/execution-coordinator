from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .github_state import GitHubApiError


_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_HTML_URL = re.compile(
    r"^https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)$"
)
_SHA = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True, slots=True)
class PullRequestSnapshot:
    repository: str
    number: int
    state: str
    head_sha: str
    html_url: str

    def __post_init__(self) -> None:
        if _REPOSITORY.fullmatch(self.repository) is None:
            raise ValueError("repository must be owner/name")
        if self.number < 1:
            raise ValueError("pull request number must be positive")
        if self.state not in {"open", "closed"}:
            raise ValueError("pull request state must be open or closed")
        if _SHA.fullmatch(self.head_sha) is None:
            raise ValueError("head_sha must be a lowercase full commit SHA")
        match = _HTML_URL.fullmatch(self.html_url)
        if (
            match is None
            or match.group(1) != self.repository
            or int(match.group(2)) != self.number
        ):
            raise ValueError("html_url must match the requested pull request")


class PullRequestReader(Protocol):
    def read_pull_request(
        self, repository: str, pull_number: int
    ) -> PullRequestSnapshot: ...


class GitHubPullRequestReader:
    """Read one exact GitHub pull request without mutation."""

    def __init__(
        self,
        *,
        token: str,
        api_base_url: str = "https://api.github.com",
        timeout_seconds: float = 20.0,
        opener=urlopen,
    ) -> None:
        self._token = token
        self._api_base_url = api_base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._opener = opener

    def _pull_url(self, repository: str, pull_number: int) -> str:
        if _REPOSITORY.fullmatch(repository) is None:
            raise ValueError("repository must be owner/name")
        if pull_number < 1:
            raise ValueError("pull request number must be positive")
        owner, name = repository.split("/", 1)
        return (
            f"{self._api_base_url}/repos/{quote(owner, safe='')}/"
            f"{quote(name, safe='')}/pulls/{pull_number}"
        )

    def read_pull_request(
        self, repository: str, pull_number: int
    ) -> PullRequestSnapshot:
        request = Request(
            self._pull_url(repository, pull_number),
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "execution-coordinator/0.1",
                "X-GitHub-Api-Version": "2022-11-28",
                **({"Authorization": f"Bearer {self._token}"} if self._token else {}),
            },
            method="GET",
        )
        try:
            with self._opener(request, timeout=self._timeout_seconds) as response:
                raw = response.read()
                status = response.status
        except HTTPError as exc:
            raise GitHubApiError(
                f"GitHub PR API GET failed with HTTP {exc.code}"
            ) from exc
        except URLError as exc:
            raise GitHubApiError("GitHub PR API GET transport failure") from exc
        if not 200 <= status < 300:
            raise GitHubApiError(f"GitHub PR API GET failed with HTTP {status}")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitHubApiError("GitHub PR API returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise GitHubApiError("GitHub PR response was not an object")

        number = payload.get("number")
        state = payload.get("state")
        html_url = payload.get("html_url")
        head = payload.get("head")
        base = payload.get("base")
        if number != pull_number:
            raise GitHubApiError("GitHub PR response number did not match")
        if state not in {"open", "closed"}:
            raise GitHubApiError("GitHub PR response state is invalid")
        if not isinstance(html_url, str):
            raise GitHubApiError("GitHub PR response html_url is invalid")
        match = _HTML_URL.fullmatch(html_url)
        if (
            match is None
            or match.group(1) != repository
            or int(match.group(2)) != pull_number
        ):
            raise GitHubApiError("GitHub PR response identity did not match")
        if not isinstance(base, dict):
            raise GitHubApiError("GitHub PR response base is invalid")
        base_repo = base.get("repo")
        if not isinstance(base_repo, dict) or base_repo.get("full_name") != repository:
            raise GitHubApiError("GitHub PR base repository did not match")
        if not isinstance(head, dict) or not isinstance(head.get("sha"), str):
            raise GitHubApiError("GitHub PR response head SHA is unavailable")
        head_sha = head["sha"].lower()
        if _SHA.fullmatch(head_sha) is None:
            raise GitHubApiError("GitHub PR response head SHA is invalid")

        return PullRequestSnapshot(
            repository=repository,
            number=pull_number,
            state=state,
            head_sha=head_sha,
            html_url=html_url,
        )
