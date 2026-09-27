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


# Protocol v1 durable Work Status vocabulary is owned by devflow/.devflow/WORKFLOW.yaml.
# This runtime copy is recognition-only. Ordinary role/state compatibility is owned by
# the accepted devflow durable-candidate source contract (devflow #125 / PR #126).
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

MARKER_BEGIN = "<!-- DEVFLOW_EXECUTION_CANDIDATE_V1_BEGIN -->"
MARKER_END = "<!-- DEVFLOW_EXECUTION_CANDIDATE_V1_END -->"

_STATUS_TOKEN = re.compile(r"^[A-Z][A-Z0-9_]*$")
_STATUS_LINE = re.compile(
    r"^\s*-\s+Work Status:\s*(?:`([^`\r\n]+)`|([^\s`]+))\s*$",
    re.MULTILINE,
)
_HEADING = re.compile(r"^##\s+(.+?)\s*$")
_TASK_REF = re.compile(
    r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)#([1-9][0-9]*)$"
)
_DEVFLOW_REF = re.compile(r"^kinoko34077/devflow#([1-9][0-9]*)$")
_ENTRY_REF = re.compile(
    r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/(issues|pull)/([1-9][0-9]*)$"
)

_ROLE_STATUS = {
    Role.IMPLEMENTER: "READY_FOR_IMPLEMENTATION",
    Role.REVIEWER: "AWAITING_REVIEW",
    Role.VERIFIER: "AWAITING_REVIEW",
    Role.INTEGRATOR: "AWAITING_REVIEW",
}

_REQUIRED_MARKER_FIELDS = frozenset(
    {
        "schema_version",
        "task_ref",
        "entry_ref",
        "role",
        "scope_ready",
        "blocked",
        "requires_user_confirmation",
        "provenance",
    }
)
_OPTIONAL_MARKER_FIELDS = frozenset({"conflict_keys"})
_REQUIRED_PROVENANCE_FIELDS = frozenset({"control_ref"})
_OPTIONAL_PROVENANCE_FIELDS = frozenset({"work_order_ref"})


@dataclass(frozen=True, slots=True)
class DurableIssueSource:
    """Exact owning Issue reference selected outside discovery.

    Candidate role/readiness/conflict/provenance authority is deliberately not
    caller-supplied. Those values must come from the accepted v1 marker in the
    owning Issue itself.
    """

    repository: str
    issue_number: int

    def __post_init__(self) -> None:
        _validate_repository(self.repository)
        if self.issue_number < 1:
            raise ValueError("issue_number must be positive")


@dataclass(frozen=True, slots=True)
class IssueDocument:
    repository: str
    number: int
    state: str
    body: str
    html_url: str
    title: str = ""
    is_pull_request: bool = False


@dataclass(frozen=True, slots=True)
class DiscoveryFailure:
    source: DurableIssueSource
    reason: str


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    candidates: tuple[ClaimCandidate, ...]
    failures: tuple[DiscoveryFailure, ...]


@dataclass(frozen=True, slots=True)
class _CandidateMarker:
    task_ref: str
    entry_ref: str
    role: Role
    scope_ready: bool
    blocked: bool
    requires_user_confirmation: bool
    conflict_keys: tuple[str, ...]
    control_issue_number: int
    work_order_issue_number: int | None


class IssueReader(Protocol):
    def read_issue(self, repository: str, issue_number: int) -> IssueDocument: ...


class GitHubIssueReader:
    """Read exact GitHub Issues/PR issue-objects without mutation."""

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
        _validate_repository(repository)
        if issue_number < 1:
            raise ValueError("issue_number must be positive")
        owner, name = repository.split("/", 1)
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
        if not isinstance(number, int):
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
            title=title,
            is_pull_request="pull_request" in payload,
        )


def _validate_repository(repository: str) -> None:
    parts = repository.split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError("repository must be owner/name")
    if not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts):
        raise ValueError("repository must use GitHub owner/name characters")


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


def _strip_code_value(value: str) -> str:
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
            values.append(_strip_code_value(lines[0]))
        else:
            values.append("")
    if len(values) != 1:
        raise ValueError("Control Work Status must appear exactly once in a supported form")
    status = values[0]
    if not _STATUS_TOKEN.fullmatch(status):
        raise ValueError("Control Work Status must use a canonical uppercase token")
    if status not in SUPPORTED_WORK_STATES:
        raise ValueError(f"unsupported Control Work Status: {status}")
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


def _parse_devflow_ref(value: object, field: str) -> int:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a devflow Issue reference")
    match = _DEVFLOW_REF.fullmatch(value)
    if not match:
        raise ValueError(f"{field} must be kinoko34077/devflow#N")
    return int(match.group(1))


def _parse_entry_ref(value: object) -> tuple[str, str, int]:
    if not isinstance(value, str):
        raise ValueError("entry_ref must be a canonical GitHub URL")
    match = _ENTRY_REF.fullmatch(value)
    if not match:
        raise ValueError("entry_ref must be a canonical GitHub Issue or Pull Request URL")
    repository = f"{match.group(1)}/{match.group(2)}"
    return repository, match.group(3), int(match.group(4))


def _parse_marker(body: str) -> _CandidateMarker | None:
    begin_count = body.count(MARKER_BEGIN)
    end_count = body.count(MARKER_END)
    if begin_count == 0 and end_count == 0:
        return None
    if begin_count != 1 or end_count != 1:
        raise ValueError("candidate marker must contain exactly one begin/end pair")

    begin = body.find(MARKER_BEGIN)
    end = body.find(MARKER_END)
    if end < begin + len(MARKER_BEGIN):
        raise ValueError("candidate marker framing is reversed or malformed")
    raw = body[begin + len(MARKER_BEGIN) : end].strip()
    if not raw:
        raise ValueError("candidate marker JSON is empty")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("candidate marker JSON is malformed") from exc
    if not isinstance(payload, dict):
        raise ValueError("candidate marker JSON must be an object")

    keys = set(payload)
    missing = _REQUIRED_MARKER_FIELDS - keys
    unknown = keys - _REQUIRED_MARKER_FIELDS - _OPTIONAL_MARKER_FIELDS
    if missing:
        raise ValueError(f"candidate marker missing required fields: {sorted(missing)}")
    if unknown:
        raise ValueError(f"candidate marker has unknown v1 fields: {sorted(unknown)}")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise ValueError("candidate marker schema_version must equal 1")

    task_ref = payload["task_ref"]
    if not isinstance(task_ref, str) or not _TASK_REF.fullmatch(task_ref):
        raise ValueError("task_ref must be owner/repository#N")

    entry_ref = payload["entry_ref"]
    _parse_entry_ref(entry_ref)

    role_raw = payload["role"]
    try:
        role = Role(role_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("candidate marker role is unsupported") from exc

    for field in ("scope_ready", "blocked", "requires_user_confirmation"):
        if type(payload[field]) is not bool:
            raise ValueError(f"candidate marker {field} must be boolean")

    conflict_raw = payload.get("conflict_keys", [])
    if not isinstance(conflict_raw, list):
        raise ValueError("candidate marker conflict_keys must be an array")
    if any(not isinstance(item, str) or not item.strip() for item in conflict_raw):
        raise ValueError("candidate marker conflict_keys must contain non-empty strings")
    if len(conflict_raw) != len(set(conflict_raw)):
        raise ValueError("candidate marker conflict_keys must be unique")

    provenance = payload["provenance"]
    if not isinstance(provenance, dict):
        raise ValueError("candidate marker provenance must be an object")
    provenance_keys = set(provenance)
    missing_provenance = _REQUIRED_PROVENANCE_FIELDS - provenance_keys
    unknown_provenance = (
        provenance_keys - _REQUIRED_PROVENANCE_FIELDS - _OPTIONAL_PROVENANCE_FIELDS
    )
    if missing_provenance:
        raise ValueError(
            f"candidate marker provenance missing required fields: {sorted(missing_provenance)}"
        )
    if unknown_provenance:
        raise ValueError(
            f"candidate marker provenance has unknown v1 fields: {sorted(unknown_provenance)}"
        )
    control_issue_number = _parse_devflow_ref(
        provenance["control_ref"], "provenance.control_ref"
    )
    work_order_issue_number = None
    if "work_order_ref" in provenance:
        work_order_issue_number = _parse_devflow_ref(
            provenance["work_order_ref"], "provenance.work_order_ref"
        )

    return _CandidateMarker(
        task_ref=task_ref,
        entry_ref=entry_ref,
        role=role,
        scope_ready=payload["scope_ready"],
        blocked=payload["blocked"],
        requires_user_confirmation=payload["requires_user_confirmation"],
        conflict_keys=tuple(conflict_raw),
        control_issue_number=control_issue_number,
        work_order_issue_number=work_order_issue_number,
    )


def _validate_source_document(
    source: DurableIssueSource, document: IssueDocument
) -> None:
    if document.repository != source.repository or document.number != source.issue_number:
        raise ValueError("GitHub Issue identity did not match the requested canonical source")
    if document.is_pull_request:
        raise ValueError("canonical source resolved to a pull request, not an Issue")
    if document.state.casefold() != "open":
        raise ValueError("canonical source must be an open Issue")


def _validate_control(
    marker: _CandidateMarker,
    source: DurableIssueSource,
    reader: IssueReader,
) -> None:
    control = reader.read_issue("kinoko34077/devflow", marker.control_issue_number)
    if control.repository != "kinoko34077/devflow" or control.number != marker.control_issue_number:
        raise ValueError("control_ref resolved to the wrong GitHub Issue identity")
    if control.is_pull_request or control.state.casefold() != "open":
        raise ValueError("control_ref must resolve to an open devflow Repository Control Issue")

    repository_name = source.repository.split("/", 1)[1]
    if control.title.strip() != f"[REPO] {repository_name}":
        raise ValueError("control_ref must resolve to the matching [REPO] Repository Control")

    sections = _sections(control.body)
    repository_value = _strip_code_value(
        _required_section(sections, ("repository",), "Control Repository")
    )
    if repository_value != source.repository:
        raise ValueError("control_ref Repository does not match the owning task repository")

    status = _parse_work_status(control.body, sections)
    next_action = _required_section(sections, ("next action",), "Control Next Action")
    has_user_gate = "[USER_DECISION]" in next_action or "[HUMAN_GATE]" in next_action
    if has_user_gate and not marker.requires_user_confirmation:
        raise ValueError(
            "Control human gate contradicts requires_user_confirmation=false"
        )
    if status == "BLOCKED" and not marker.blocked:
        raise ValueError("Control BLOCKED state contradicts candidate blocked=false")

    required_status = _ROLE_STATUS[marker.role]
    if status != required_status:
        raise ValueError(
            f"Control Work Status {status} does not permit ordinary {marker.role.value} discovery"
        )


def _validate_work_order(marker: _CandidateMarker, reader: IssueReader) -> None:
    if marker.work_order_issue_number is None:
        return
    work_order = reader.read_issue("kinoko34077/devflow", marker.work_order_issue_number)
    if (
        work_order.repository != "kinoko34077/devflow"
        or work_order.number != marker.work_order_issue_number
    ):
        raise ValueError("work_order_ref resolved to the wrong GitHub Issue identity")
    if work_order.is_pull_request or work_order.state.casefold() != "open":
        raise ValueError("work_order_ref must resolve to an open devflow Issue")
    if not work_order.title.strip().startswith("[WORK ORDER]"):
        raise ValueError("work_order_ref must resolve to a [WORK ORDER] devflow Issue")


def _validate_entry(
    marker: _CandidateMarker,
    source: DurableIssueSource,
    source_document: IssueDocument,
    reader: IssueReader,
) -> None:
    repository, kind, number = _parse_entry_ref(marker.entry_ref)
    if repository != source.repository:
        raise ValueError("entry_ref must identify the same repository as task_ref")

    if kind == "issues" and number == source.issue_number:
        entry = source_document
    else:
        entry = reader.read_issue(repository, number)

    if entry.repository != repository or entry.number != number:
        raise ValueError("entry_ref resolved to the wrong GitHub object identity")
    if entry.state.casefold() != "open":
        raise ValueError("entry_ref must resolve to an open GitHub object")
    if entry.html_url != marker.entry_ref:
        raise ValueError("entry_ref does not match the resolved canonical GitHub URL")
    if kind == "issues" and entry.is_pull_request:
        raise ValueError("entry_ref declared an Issue but resolved to a Pull Request")
    if kind == "pull" and not entry.is_pull_request:
        raise ValueError("entry_ref declared a Pull Request but resolved to an Issue")


def _normalize(
    source: DurableIssueSource,
    document: IssueDocument,
    marker: _CandidateMarker,
    reader: IssueReader,
) -> ClaimCandidate:
    _validate_source_document(source, document)

    task_match = _TASK_REF.fullmatch(marker.task_ref)
    if task_match is None:  # already structurally checked; keeps type reasoning explicit
        raise ValueError("task_ref must be owner/repository#N")
    task_repository = f"{task_match.group(1)}/{task_match.group(2)}"
    task_number = int(task_match.group(3))
    if task_repository != source.repository or task_number != source.issue_number:
        raise ValueError("task_ref must identify the Issue containing the candidate marker")

    _validate_control(marker, source, reader)
    _validate_work_order(marker, reader)
    _validate_entry(marker, source, document, reader)

    return ClaimCandidate(
        task=marker.task_ref,
        role=marker.role,
        entry_ref=marker.entry_ref,
        conflict_keys=marker.conflict_keys,
        scope_ready=marker.scope_ready,
        blocked=marker.blocked,
        requires_user_confirmation=marker.requires_user_confirmation,
    )


def discover_claim_candidates(
    sources: Iterable[DurableIssueSource], reader: IssueReader
) -> DiscoveryResult:
    """Normalize explicit v1 candidate markers from exact owning Issue refs.

    Discovery is deterministic and read-only. Marker absence is a valid
    non-discoverable Issue, not an error. Each malformed or contradictory
    marked source becomes an explicit failure without hiding valid siblings.
    """

    candidates: list[ClaimCandidate] = []
    failures: list[DiscoveryFailure] = []
    for source in sources:
        try:
            document = reader.read_issue(source.repository, source.issue_number)
            _validate_source_document(source, document)
            marker = _parse_marker(document.body)
            if marker is None:
                continue
            candidates.append(_normalize(source, document, marker, reader))
        except (GitHubApiError, ValueError) as exc:
            failures.append(DiscoveryFailure(source=source, reason=str(exc)))
    return DiscoveryResult(candidates=tuple(candidates), failures=tuple(failures))
