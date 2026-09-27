from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Iterable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from .github_state import GitHubApiError
from .model import Role
from .query import ClaimCandidate


MARKER_BEGIN = "<!-- DEVFLOW_EXECUTION_CANDIDATE_V1_BEGIN -->"
MARKER_END = "<!-- DEVFLOW_EXECUTION_CANDIDATE_V1_END -->"

SUPPORTED_WORK_STATES = frozenset(
    {
        "NEEDS_AUDIT",
        "AUDITED",
        "WORK_ORDER_READY",
        "READY_FOR_IMPLEMENTATION",
        "IMPLEMENTING",
        "AWAITING_REVIEW",
        "BLOCKED",
        "NEEDS_REAUDIT",
        "PARKED",
        "DONE",
    }
)
ROLE_WORK_STATUS = {
    Role.IMPLEMENTER: "READY_FOR_IMPLEMENTATION",
    Role.REVIEWER: "AWAITING_REVIEW",
    Role.VERIFIER: "AWAITING_REVIEW",
    Role.INTEGRATOR: "AWAITING_REVIEW",
}
_ALLOWED_MARKER_FIELDS = {
    "schema_version",
    "task_ref",
    "entry_ref",
    "role",
    "scope_ready",
    "blocked",
    "requires_user_confirmation",
    "conflict_keys",
    "provenance",
}
_REQUIRED_MARKER_FIELDS = _ALLOWED_MARKER_FIELDS - {"conflict_keys"}
_ALLOWED_PROVENANCE_FIELDS = {"control_ref", "work_order_ref"}
_STATUS_TOKEN = re.compile(r"^[A-Z][A-Z0-9_]*$")
_STATUS_LINE = re.compile(
    r"^\s*-\s+Work Status:\s*(?:`([^`\r\n]+)`|([^\s`]+))\s*$",
    re.MULTILINE,
)
_HEADING = re.compile(r"^##\s+(.+?)\s*$")
_TASK_REF = re.compile(r"^([^/#\s]+)/([^/#\s]+)#([1-9][0-9]*)$")
_DEVFLOW_REF = re.compile(r"^kinoko34077/devflow#([1-9][0-9]*)$")


class _NotDiscoverable(Exception):
    """Internal non-error result for valid Issues without an opt-in marker."""


@dataclass(frozen=True, slots=True)
class DurableIssueSource:
    """Exact owning-Issue identity. Candidate authority lives in the Issue marker."""

    repository: str
    issue_number: int

    def __post_init__(self) -> None:
        _repository_parts(self.repository)
        if type(self.issue_number) is not int or self.issue_number < 1:
            raise ValueError("issue_number must be a positive integer")


@dataclass(frozen=True, slots=True)
class IssueDocument:
    repository: str
    number: int
    state: str
    body: str
    html_url: str
    is_pull_request: bool = False
    title: str = ""


@dataclass(frozen=True, slots=True)
class DiscoveryFailure:
    source: DurableIssueSource
    reason: str


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    candidates: tuple[ClaimCandidate, ...]
    failures: tuple[DiscoveryFailure, ...]


class IssueReader(Protocol):
    def read_issue(self, repository: str, issue_number: int) -> IssueDocument: ...


class GitHubIssueReader:
    """GET-only reader for exact GitHub Issue references."""

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

    def _issue_url(self, repository: str, issue_number: int) -> str:
        owner, name = _repository_parts(repository)
        if type(issue_number) is not int or issue_number < 1:
            raise ValueError("issue_number must be a positive integer")
        return (
            f"{self._api_base_url}/repos/{quote(owner, safe='')}/"
            f"{quote(name, safe='')}/issues/{issue_number}"
        )

    def read_issue(self, repository: str, issue_number: int) -> IssueDocument:
        url = self._issue_url(repository, issue_number)
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
        title = payload.get("title", "")
        if type(number) is not int:
            raise GitHubApiError("GitHub issue response did not contain an integer number")
        if not isinstance(state, str):
            raise GitHubApiError("GitHub issue response did not contain a string state")
        if not isinstance(body, str):
            raise GitHubApiError("GitHub issue response did not contain a string body")
        if not isinstance(html_url, str):
            raise GitHubApiError("GitHub issue response did not contain a string html_url")
        if not isinstance(title, str):
            raise GitHubApiError("GitHub issue response did not contain a string title")
        return IssueDocument(
            repository=repository,
            number=number,
            state=state,
            body=body,
            html_url=html_url,
            is_pull_request="pull_request" in payload,
            title=title,
        )


def _repository_parts(repository: str) -> tuple[str, str]:
    if not isinstance(repository, str):
        raise ValueError("repository must be owner/name")
    parts = repository.split("/")
    if len(parts) != 2 or not all(part and not part.isspace() for part in parts):
        raise ValueError("repository must be owner/name")
    return parts[0], parts[1]


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


def _parse_work_status(body: str) -> str:
    sections = _sections(body)
    values: list[str] = []
    for match in _STATUS_LINE.finditer(body):
        values.append((match.group(1) or match.group(2) or "").strip())
    for content in sections.get("work status", []):
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        values.append(_strip_status_value(lines[0]) if len(lines) == 1 else "")
    if len(values) != 1:
        raise ValueError("Work Status must appear exactly once in a supported form")
    status = values[0]
    if not _STATUS_TOKEN.fullmatch(status) or status not in SUPPORTED_WORK_STATES:
        raise ValueError(f"unsupported Work Status: {status}")
    return status


def _optional_single_section(body: str, name: str) -> str:
    values = _sections(body).get(name.casefold(), [])
    if len(values) > 1:
        raise ValueError(f"{name} section must not be ambiguous")
    return values[0] if values else ""


def _parse_marker(body: str) -> tuple[dict[str, object], str]:
    begin_count = body.count(MARKER_BEGIN)
    end_count = body.count(MARKER_END)
    if begin_count == 0 and end_count == 0:
        raise _NotDiscoverable()
    if begin_count != 1 or end_count != 1:
        raise ValueError("durable candidate marker must appear exactly once")
    begin = body.find(MARKER_BEGIN)
    end = body.find(MARKER_END)
    if begin < 0 or end < 0 or begin >= end:
        raise ValueError("durable candidate marker boundaries are reversed or incomplete")
    raw = body[begin + len(MARKER_BEGIN) : end].strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("durable candidate marker contains malformed JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("durable candidate marker must contain one JSON object")
    non_marker_body = body[:begin] + body[end + len(MARKER_END) :]
    return payload, non_marker_body


def _validate_bool(payload: dict[str, object], name: str) -> bool:
    value = payload.get(name)
    if type(value) is not bool:
        raise ValueError(f"marker field {name} must be a boolean")
    return value


def _validate_marker_shape(payload: dict[str, object]) -> None:
    keys = set(payload)
    missing = _REQUIRED_MARKER_FIELDS - keys
    unknown = keys - _ALLOWED_MARKER_FIELDS
    if missing:
        raise ValueError(f"durable candidate marker is missing required fields: {sorted(missing)}")
    if unknown:
        raise ValueError(f"durable candidate marker contains unknown v1 fields: {sorted(unknown)}")
    if type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
        raise ValueError("unsupported durable candidate schema_version")
    for name in ("task_ref", "entry_ref", "role"):
        if not isinstance(payload.get(name), str) or not str(payload[name]).strip():
            raise ValueError(f"marker field {name} must be a non-empty string")
    _validate_bool(payload, "scope_ready")
    _validate_bool(payload, "blocked")
    _validate_bool(payload, "requires_user_confirmation")
    provenance = payload.get("provenance")
    if not isinstance(provenance, dict):
        raise ValueError("marker provenance must be an object")
    pkeys = set(provenance)
    if "control_ref" not in pkeys:
        raise ValueError("marker provenance requires control_ref")
    unknown_provenance = pkeys - _ALLOWED_PROVENANCE_FIELDS
    if unknown_provenance:
        raise ValueError(
            f"marker provenance contains unknown v1 fields: {sorted(unknown_provenance)}"
        )
    for name in pkeys:
        if not isinstance(provenance[name], str) or not provenance[name].strip():
            raise ValueError(f"marker provenance {name} must be a non-empty string")


def _parse_task_ref(value: str) -> tuple[str, int]:
    match = _TASK_REF.fullmatch(value)
    if not match:
        raise ValueError("task_ref must use owner/repository#issue_number")
    return f"{match.group(1)}/{match.group(2)}", int(match.group(3))


def _validate_entry_ref(value: str, repository: str) -> None:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("entry_ref must be a canonical https://github.com URL")
    parts = [part for part in parsed.path.split("/") if part]
    owner, name = _repository_parts(repository)
    if len(parts) < 3 or parts[0] != owner or parts[1] != name:
        raise ValueError("entry_ref must identify the same owning repository")


def _validate_conflict_keys(payload: dict[str, object]) -> tuple[str, ...]:
    value = payload.get("conflict_keys", [])
    if not isinstance(value, list):
        raise ValueError("conflict_keys must be an array")
    keys: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("conflict_keys must contain non-empty strings")
        keys.append(item)
    if len(set(keys)) != len(keys):
        raise ValueError("conflict_keys must be unique")
    return tuple(keys)


def _parse_devflow_ref(value: str, *, field: str) -> int:
    match = _DEVFLOW_REF.fullmatch(value)
    if not match:
        raise ValueError(f"{field} must use kinoko34077/devflow#N")
    return int(match.group(1))


def _validate_issue_identity(document: IssueDocument, source: DurableIssueSource) -> None:
    if document.repository != source.repository or document.number != source.issue_number:
        raise ValueError("GitHub Issue identity did not match the requested canonical source")
    if document.is_pull_request:
        raise ValueError("canonical source resolved to a pull request, not an Issue")
    if document.state.casefold() != "open":
        raise ValueError("canonical source must be an open Issue")


def _read_provenance_issue(reader: IssueReader, issue_number: int) -> IssueDocument:
    document = reader.read_issue("kinoko34077/devflow", issue_number)
    if document.repository != "kinoko34077/devflow" or document.number != issue_number:
        raise ValueError("devflow provenance identity mismatch")
    if document.is_pull_request:
        raise ValueError("devflow provenance must resolve to an Issue")
    if document.state.casefold() != "open":
        raise ValueError("devflow provenance Issue must be open")
    return document


def _validate_control(document: IssueDocument, owning_repository: str) -> None:
    if document.title != f"[REPO] {owning_repository}":
        raise ValueError("control_ref must identify the matching Repository Control")
    repository_values = _sections(document.body).get("repository", [])
    if len(repository_values) != 1:
        raise ValueError("Repository Control must contain one Repository section")
    value = _strip_status_value(repository_values[0])
    if value != owning_repository:
        raise ValueError("Repository Control repository does not match the owning repository")


def _validate_work_order(document: IssueDocument) -> None:
    if not document.title.startswith("[WORK ORDER]"):
        raise ValueError("work_order_ref must identify an open [WORK ORDER] Issue")


def _normalize(
    source: DurableIssueSource, document: IssueDocument, reader: IssueReader
) -> ClaimCandidate:
    _validate_issue_identity(document, source)
    payload, non_marker_body = _parse_marker(document.body)
    _validate_marker_shape(payload)

    task_repository, task_number = _parse_task_ref(str(payload["task_ref"]))
    if task_repository != source.repository or task_number != source.issue_number:
        raise ValueError("task_ref contradicts the containing owning Issue")

    try:
        role = Role(str(payload["role"]))
    except ValueError as exc:
        raise ValueError("marker role is not supported by Protocol v1") from exc

    entry_ref = str(payload["entry_ref"])
    _validate_entry_ref(entry_ref, source.repository)
    conflict_keys = _validate_conflict_keys(payload)
    scope_ready = _validate_bool(payload, "scope_ready")
    blocked = _validate_bool(payload, "blocked")
    requires_user_confirmation = _validate_bool(payload, "requires_user_confirmation")

    status = _parse_work_status(non_marker_body)
    if status == "BLOCKED" and not blocked:
        raise ValueError("Work Status BLOCKED contradicts marker blocked=false")
    required_status = ROLE_WORK_STATUS[role]
    if status != required_status:
        raise ValueError(
            f"Work Status {status} is not eligible for ordinary {role.value} discovery"
        )

    next_action = _optional_single_section(non_marker_body, "next action")
    has_user_decision = "[USER_DECISION]" in next_action
    has_human_gate = "[HUMAN_GATE]" in non_marker_body
    if (has_user_decision or has_human_gate) and not requires_user_confirmation:
        raise ValueError("recognized human gate contradicts requires_user_confirmation=false")

    provenance = payload["provenance"]
    assert isinstance(provenance, dict)
    control_number = _parse_devflow_ref(str(provenance["control_ref"]), field="control_ref")
    control = _read_provenance_issue(reader, control_number)
    _validate_control(control, source.repository)
    if "work_order_ref" in provenance:
        work_order_number = _parse_devflow_ref(
            str(provenance["work_order_ref"]), field="work_order_ref"
        )
        work_order = _read_provenance_issue(reader, work_order_number)
        _validate_work_order(work_order)

    return ClaimCandidate(
        task=str(payload["task_ref"]),
        role=role,
        entry_ref=entry_ref,
        conflict_keys=conflict_keys,
        scope_ready=scope_ready,
        blocked=blocked,
        requires_user_confirmation=requires_user_confirmation,
    )


def discover_claim_candidates(
    sources: Iterable[DurableIssueSource], reader: IssueReader
) -> DiscoveryResult:
    """Normalize only marker-authorized exact owning Issues, without mutation/ranking."""

    candidates: list[ClaimCandidate] = []
    failures: list[DiscoveryFailure] = []
    for source in sources:
        try:
            document = reader.read_issue(source.repository, source.issue_number)
            candidates.append(_normalize(source, document, reader))
        except _NotDiscoverable:
            continue
        except (GitHubApiError, ValueError, KeyError) as exc:
            failures.append(DiscoveryFailure(source=source, reason=str(exc)))
    return DiscoveryResult(candidates=tuple(candidates), failures=tuple(failures))
