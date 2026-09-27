from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Iterable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .github_state import GitHubApiError
from .model import Role
from .query import ClaimCandidate


_STATUS_TOKEN = re.compile(r"^[A-Z][A-Z0-9_]*$")
_STATUS_LINE = re.compile(
    r"^\s*-\s+Work Status:\s*(?:`([^`\r\n]+)`|([^\s`]+))\s*$",
    re.MULTILINE,
)
_HEADING = re.compile(r"^##\s+(.+?)\s*$")


@dataclass(frozen=True, slots=True)
class DurableIssueSource:
    """Exact canonical Issue reference plus caller-owned role policy."""

    repository: str
    issue_number: int
    role: Role
    eligible_work_states: tuple[str, ...]
    conflict_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        parts = self.repository.split("/")
        if len(parts) != 2 or not all(parts):
            raise ValueError("repository must be owner/name")
        if self.issue_number < 1:
            raise ValueError("issue_number must be positive")
        if not self.eligible_work_states:
            raise ValueError("eligible_work_states must not be empty")
        for state in self.eligible_work_states:
            if not _STATUS_TOKEN.fullmatch(state):
                raise ValueError("eligible_work_states must use canonical status tokens")


@dataclass(frozen=True, slots=True)
class IssueDocument:
    repository: str
    number: int
    state: str
    body: str
    html_url: str
    is_pull_request: bool = False


@dataclass(frozen=True, slots=True)
class DiscoveryFailure:
    source: DurableIssueSource
    reason: str


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    candidates: tuple[ClaimCandidate, ...]
    failures: tuple[DiscoveryFailure, ...]


class IssueReader(Protocol):
    def read_issue(self, source: DurableIssueSource) -> IssueDocument: ...


class GitHubIssueReader:
    """Read exact GitHub Issues without mutating repository or runtime state."""

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

    def _issue_url(self, source: DurableIssueSource) -> str:
        owner, name = source.repository.split("/", 1)
        return (
            f"{self._api_base_url}/repos/{quote(owner, safe='')}/"
            f"{quote(name, safe='')}/issues/{source.issue_number}"
        )

    def read_issue(self, source: DurableIssueSource) -> IssueDocument:
        url = self._issue_url(source)
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "execution-coordinator/0.1",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        request = Request(url, headers=headers, method="GET")
        try:
            with self._opener(request, timeout=self._timeout_seconds) as response:
                raw = response.read()
                status = response.status
        except HTTPError as exc:
            raise GitHubApiError(f"GitHub API GET failed with HTTP {exc.code}") from exc
        except URLError as exc:
            raise GitHubApiError("GitHub API GET transport failure") from exc
        if not (200 <= status < 300):
            raise GitHubApiError(f"GitHub API GET failed with HTTP {status}")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitHubApiError("GitHub API returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise GitHubApiError("GitHub issue response was not an object")
        number = payload.get("number")
        state = payload.get("state")
        body = payload.get("body")
        html_url = payload.get("html_url")
        if not isinstance(number, int):
            raise GitHubApiError("GitHub issue response did not contain an integer number")
        if not isinstance(state, str):
            raise GitHubApiError("GitHub issue response did not contain a string state")
        if not isinstance(body, str):
            raise GitHubApiError("GitHub issue response did not contain a string body")
        if not isinstance(html_url, str):
            raise GitHubApiError("GitHub issue response did not contain a string html_url")
        return IssueDocument(
            repository=source.repository,
            number=number,
            state=state,
            body=body,
            html_url=html_url,
            is_pull_request="pull_request" in payload,
        )


def _sections(body: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        nonlocal buffer
        if current is not None:
            sections.setdefault(current, []).append("\n".join(buffer).strip())
        buffer = []

    for line in body.splitlines():
        match = _HEADING.fullmatch(line)
        if match:
            flush()
            current = " ".join(match.group(1).strip().casefold().split())
        elif current is not None:
            buffer.append(line)
    flush()
    return sections


def _strip_status_value(value: str) -> str:
    value = value.strip()
    if value.startswith("`") and value.endswith("`") and len(value) >= 2:
        value = value[1:-1].strip()
    return value


def _parse_work_status(body: str, sections: dict[str, list[str]]) -> str:
    values: list[str] = []
    for match in _STATUS_LINE.finditer(body):
        values.append((match.group(1) or match.group(2) or "").strip())
    for content in sections.get("work status", []):
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        if len(lines) == 1:
            values.append(_strip_status_value(lines[0]))
        else:
            values.append("")
    if len(values) != 1:
        raise ValueError("Work Status must appear exactly once in a supported form")
    status = values[0]
    if not _STATUS_TOKEN.fullmatch(status):
        raise ValueError("Work Status must use a canonical uppercase token")
    return status


def _required_section(
    sections: dict[str, list[str]], aliases: tuple[str, ...], label: str
) -> str:
    found: list[str] = []
    for alias in aliases:
        found.extend(sections.get(alias, ()))
    found = [value for value in found if value.strip()]
    if len(found) != 1:
        raise ValueError(f"{label} section must appear exactly once and be non-empty")
    return found[0]


def _optional_single_section(
    sections: dict[str, list[str]], aliases: tuple[str, ...], label: str
) -> str:
    found: list[str] = []
    for alias in aliases:
        found.extend(sections.get(alias, ()))
    if len(found) > 1:
        raise ValueError(f"{label} section must not be ambiguous")
    return found[0] if found else ""


def _normalize(source: DurableIssueSource, document: IssueDocument) -> ClaimCandidate:
    if document.repository != source.repository or document.number != source.issue_number:
        raise ValueError("GitHub Issue identity did not match the requested canonical source")
    if document.is_pull_request:
        raise ValueError("canonical source resolved to a pull request, not an Issue")
    if document.state.casefold() != "open":
        raise ValueError("canonical source must be an open Issue")
    if not document.html_url.strip():
        raise ValueError("canonical Issue URL is missing")

    sections = _sections(document.body)
    status = _parse_work_status(document.body, sections)
    _required_section(sections, ("objective",), "Objective")
    _required_section(sections, ("scope", "design / scope", "design/scope"), "Scope")
    _required_section(sections, ("acceptance criteria",), "Acceptance criteria")
    next_action = _optional_single_section(sections, ("next action",), "Next Action")

    return ClaimCandidate(
        task=f"{source.repository}#{source.issue_number}",
        role=source.role,
        entry_ref=document.html_url,
        conflict_keys=source.conflict_keys,
        scope_ready=status in source.eligible_work_states,
        blocked=status == "BLOCKED",
        requires_user_confirmation="[USER_DECISION]" in next_action,
    )


def discover_claim_candidates(
    sources: Iterable[DurableIssueSource], reader: IssueReader
) -> DiscoveryResult:
    """Read exact canonical Issues and normalize them without mutation or ranking."""

    candidates: list[ClaimCandidate] = []
    failures: list[DiscoveryFailure] = []
    for source in sources:
        try:
            document = reader.read_issue(source)
            candidates.append(_normalize(source, document))
        except (GitHubApiError, ValueError) as exc:
            failures.append(DiscoveryFailure(source=source, reason=str(exc)))
    return DiscoveryResult(candidates=tuple(candidates), failures=tuple(failures))
