from __future__ import annotations

import hashlib
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
TRUSTED_AUTHOR_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})

MARKER_BEGIN = "<!-- DEVFLOW_EXECUTION_CANDIDATES_V1_BEGIN -->"
MARKER_END = "<!-- DEVFLOW_EXECUTION_CANDIDATES_V1_END -->"

_STATUS_TOKEN = re.compile(r"^[A-Z][A-Z0-9_]*$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_TASK_REF = re.compile(r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([1-9][0-9]*)$")
_DEVFLOW_REF = re.compile(r"^kinoko34077/devflow#([1-9][0-9]*)$")
_ENTRY_REF = re.compile(
    r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/issues/([1-9][0-9]*)$"
)
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_HEADING = re.compile(r"^##\s+(.+?)\s*$")

_ROLE_ACTIONS: dict[Role, dict[str, frozenset[str]]] = {
    Role.IMPLEMENTER: {
        "READY_FOR_IMPLEMENTATION": frozenset({"SPECIFY", "IMPLEMENT"}),
    },
    Role.VERIFIER: {"AWAITING_REVIEW": frozenset({"VERIFY"})},
    Role.REVIEWER: {"AWAITING_REVIEW": frozenset({"REVIEW"})},
    Role.INTEGRATOR: {"AWAITING_REVIEW": frozenset({"MERGE"})},
}
_REQUIRED_OUTER_FIELDS = frozenset(
    {"schema_version", "source_ref", "repository", "candidates"}
)
_REQUIRED_TASK_FIELDS = frozenset(
    {
        "task",
        "task_body_sha256",
        "task_work_status",
        "entry_ref",
        "scope_ready",
        "blocked",
        "requires_user_confirmation",
        "roles",
    }
)
_OPTIONAL_TASK_FIELDS = frozenset({"conflict_keys", "work_order_ref"})


@dataclass(frozen=True, slots=True)
class DurableIssueSource:
    """Exact Repository Control identity selected by the live bootstrap."""

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
    author_association: str = ""
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
class _RoleProjection:
    role: Role
    next_action_tag: str


@dataclass(frozen=True, slots=True)
class _TaskEnvelope:
    task_repository: str
    task_number: int
    task_ref: str
    task_body_sha256: str
    task_work_status: str
    entry_ref: str
    scope_ready: bool
    blocked: bool
    requires_user_confirmation: bool
    conflict_keys: tuple[str, ...]
    work_order_ref: str | None
    roles: tuple[_RoleProjection, ...]


class IssueReader(Protocol):
    def read_issue(self, repository: str, issue_number: int) -> IssueDocument: ...


class GitHubIssueReader:
    """Read exact GitHub Issue objects without mutation."""

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
        request = Request(
            self._issue_url(repository, issue_number),
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
            raise GitHubApiError(f"GitHub API GET failed with HTTP {exc.code}") from exc
        except URLError as exc:
            raise GitHubApiError("GitHub API GET transport failure") from exc
        if not 200 <= status < 300:
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
        author_association = payload.get("author_association", "")
        if not isinstance(number, int):
            raise GitHubApiError("GitHub issue response did not contain an integer number")
        if not isinstance(state, str):
            raise GitHubApiError("GitHub issue response did not contain a string state")
        if body is None:
            body = ""
        if not isinstance(body, str):
            raise GitHubApiError("GitHub issue response did not contain a string body")
        if not isinstance(html_url, str):
            raise GitHubApiError("GitHub issue response did not contain a string html_url")
        if not isinstance(title, str):
            title = ""
        if not isinstance(author_association, str):
            author_association = ""
        return IssueDocument(
            repository=repository,
            number=number,
            state=state,
            body=body,
            html_url=html_url,
            title=title,
            author_association=author_association,
            is_pull_request="pull_request" in payload,
        )


def _validate_repository(repository: str) -> None:
    if not _REPOSITORY.fullmatch(repository):
        raise ValueError("repository must be owner/name")


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
    if value.startswith(chr(96)) and value.endswith(chr(96)) and len(value) >= 2:
        return value[1:-1].strip()
    return value


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


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"candidate projection contains duplicate key: {key}")
        result[key] = value
    return result


def _parse_json_block(body: str) -> object | None:
    begin_count = body.count(MARKER_BEGIN)
    end_count = body.count(MARKER_END)
    if begin_count == 0 and end_count == 0:
        return None
    if begin_count != 1 or end_count != 1:
        raise ValueError("candidate projection must contain exactly one begin/end pair")
    begin = body.find(MARKER_BEGIN)
    end = body.find(MARKER_END)
    if end < begin + len(MARKER_BEGIN):
        raise ValueError("candidate projection framing is reversed or malformed")
    raw = body[begin + len(MARKER_BEGIN) : end].strip()
    if not raw:
        raise ValueError("candidate projection JSON is empty")
    try:
        return json.loads(raw, object_pairs_hook=_reject_duplicate_json_keys)
    except json.JSONDecodeError as exc:
        raise ValueError("candidate projection JSON is malformed") from exc


def _parse_repository(value: object, field: str) -> str:
    if not isinstance(value, str) or not _REPOSITORY.fullmatch(value):
        raise ValueError(f"{field} must be owner/name")
    return value


def _parse_task_ref(value: object, field: str = "task") -> tuple[str, int]:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be owner/repository#N")
    match = _TASK_REF.fullmatch(value)
    if match is None:
        raise ValueError(f"{field} must be owner/repository#N")
    return match.group(1), int(match.group(2))


def _parse_devflow_ref(value: object, field: str) -> tuple[str, int]:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be kinoko34077/devflow#N")
    match = _DEVFLOW_REF.fullmatch(value)
    if match is None:
        raise ValueError(f"{field} must be kinoko34077/devflow#N")
    return "kinoko34077/devflow", int(match.group(1))


def _parse_entry_ref(value: object) -> tuple[str, int]:
    if not isinstance(value, str):
        raise ValueError("entry_ref must be a canonical GitHub Issue URL")
    match = _ENTRY_REF.fullmatch(value)
    if match is None:
        raise ValueError("entry_ref must be a canonical GitHub Issue URL")
    return f"{match.group(1)}/{match.group(2)}", int(match.group(3))


def _parse_roles(value: object, task_number: int) -> tuple[_RoleProjection, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"task #{task_number} roles must be a non-empty array")
    roles: list[_RoleProjection] = []
    seen: set[Role] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError(f"task #{task_number} role entry must be an object")
        if set(item) != {"role", "next_action_tag"}:
            raise ValueError(f"task #{task_number} role entry has unknown or missing fields")
        try:
            role = Role(item["role"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"task #{task_number} role is unsupported") from exc
        if role in seen:
            raise ValueError(f"task #{task_number} roles must be unique")
        seen.add(role)
        action = item["next_action_tag"]
        if not isinstance(action, str) or not _STATUS_TOKEN.fullmatch(action):
            raise ValueError(f"task #{task_number} next_action_tag must be an uppercase token")
        roles.append(_RoleProjection(role=role, next_action_tag=action))
    return tuple(roles)


def _parse_conflict_keys(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("conflict_keys must be an array")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError("conflict_keys must contain non-empty strings")
    if len(value) != len(set(value)):
        raise ValueError("conflict_keys must be unique")
    return tuple(value)


def _parse_envelope(value: object, repository: str) -> _TaskEnvelope:
    if not isinstance(value, dict):
        raise ValueError("candidate task envelope must be an object")
    keys = set(value)
    allowed = _REQUIRED_TASK_FIELDS | _OPTIONAL_TASK_FIELDS
    missing = _REQUIRED_TASK_FIELDS - keys
    unknown = keys - allowed
    if missing:
        raise ValueError(f"candidate task envelope missing fields: {sorted(missing)}")
    if unknown:
        raise ValueError(f"candidate task envelope has unknown fields: {sorted(unknown)}")

    task_repository, task_number = _parse_task_ref(value["task"])
    if task_repository != repository:
        raise ValueError("task repository does not match the projection repository")
    digest = value["task_body_sha256"]
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise ValueError("task_body_sha256 must be sha256 plus 64 lowercase hex characters")
    status = value["task_work_status"]
    if not isinstance(status, str) or status not in SUPPORTED_WORK_STATES:
        raise ValueError("task_work_status is unsupported")
    entry_repository, entry_number = _parse_entry_ref(value["entry_ref"])
    if entry_repository != task_repository or entry_number != task_number:
        raise ValueError("entry_ref must identify the exact task Issue")
    for field in ("scope_ready", "blocked", "requires_user_confirmation"):
        if type(value[field]) is not bool:
            raise ValueError(f"{field} must be boolean")
    roles = _parse_roles(value["roles"], task_number)
    conflict_keys = _parse_conflict_keys(value.get("conflict_keys"))
    work_order_ref = value.get("work_order_ref")
    if work_order_ref is not None:
        _parse_devflow_ref(work_order_ref, "work_order_ref")
    return _TaskEnvelope(
        task_repository=task_repository,
        task_number=task_number,
        task_ref=value["task"],
        task_body_sha256=digest,
        task_work_status=status,
        entry_ref=value["entry_ref"],
        scope_ready=value["scope_ready"],
        blocked=value["blocked"],
        requires_user_confirmation=value["requires_user_confirmation"],
        conflict_keys=conflict_keys,
        work_order_ref=work_order_ref,
        roles=roles,
    )


def _canonical_digest(body: str) -> str:
    normalized = body.replace("\r\n", "\n").replace("\r", "\n")
    return "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _require_trusted(document: IssueDocument, label: str) -> None:
    if document.author_association not in TRUSTED_AUTHOR_ASSOCIATIONS:
        raise ValueError(f"{label} author is not trusted")


def _validate_control(
    source: DurableIssueSource,
    document: IssueDocument,
    projection_repository: str,
    source_ref: str,
) -> None:
    if source.repository != "kinoko34077/devflow":
        raise ValueError("Repository Control source must be in kinoko34077/devflow")
    if document.repository != source.repository or document.number != source.issue_number:
        raise ValueError("Repository Control identity did not match the requested source")
    if document.is_pull_request or document.state.casefold() != "open":
        raise ValueError("Repository Control must be an open Issue")
    _require_trusted(document, "Repository Control")
    if document.title.strip() != f"[REPO] {projection_repository.split('/', 1)[1]}":
        raise ValueError("source Issue is not the canonical [REPO] Repository Control")

    sections = _sections(document.body)
    repository = _strip_code_value(
        _required_section(sections, ("repository",), "Control Repository")
    )
    if repository != projection_repository:
        raise ValueError("Control Repository does not match candidate projection")
    repository_state = _strip_code_value(
        _required_section(sections, ("repository state",), "Control Repository State")
    )
    if repository_state != "ACTIVE":
        raise ValueError("Control Repository State is not ACTIVE")
    next_action = _required_section(sections, ("next action",), "Control Next Action")
    if "[USER_DECISION]" in next_action or "[HUMAN_GATE]" in next_action:
        raise ValueError("Control Next Action contains a current user or Human Gate")
    expected_source_ref = f"{source.repository}#{source.issue_number}"
    if source_ref != expected_source_ref:
        raise ValueError("candidate projection source_ref does not identify this Control")


def _validate_task(envelope: _TaskEnvelope, document: IssueDocument) -> None:
    if (
        document.repository != envelope.task_repository
        or document.number != envelope.task_number
    ):
        raise ValueError("task identity did not match the candidate envelope")
    if document.is_pull_request or document.state.casefold() != "open":
        raise ValueError("candidate task must be an open Issue")
    _require_trusted(document, "owning task")
    if not document.body.strip():
        raise ValueError("owning task body must be non-empty")
    if _canonical_digest(document.body) != envelope.task_body_sha256:
        raise ValueError("task_body_sha256 digest mismatch")


def _validate_work_order(work_order_ref: str | None, reader: IssueReader) -> None:
    if work_order_ref is None:
        return
    repository, number = _parse_devflow_ref(work_order_ref, "work_order_ref")
    document = reader.read_issue(repository, number)
    if document.is_pull_request or document.state.casefold() != "open":
        raise ValueError("work_order_ref must resolve to an open Issue")
    _require_trusted(document, "work_order_ref")
    if not document.title.strip().startswith("[WORK ORDER]"):
        raise ValueError("work_order_ref must resolve to a [WORK ORDER] Issue")


def _validate_role_status(envelope: _TaskEnvelope) -> None:
    for role in envelope.roles:
        allowed_actions = _ROLE_ACTIONS.get(role.role, {}).get(envelope.task_work_status)
        if allowed_actions is None or role.next_action_tag not in allowed_actions:
            raise ValueError(
                f"role/status/action combination is unsupported for {role.role.value}"
            )


def _candidate_for_role(envelope: _TaskEnvelope, role: _RoleProjection) -> ClaimCandidate:
    return ClaimCandidate(
        task=envelope.task_ref,
        role=role.role,
        entry_ref=envelope.entry_ref,
        conflict_keys=envelope.conflict_keys,
        scope_ready=envelope.scope_ready,
        blocked=envelope.blocked,
        requires_user_confirmation=envelope.requires_user_confirmation,
    )


def _validate_projection(
    source: DurableIssueSource,
    control: IssueDocument,
    raw_projection: object,
    reader: IssueReader,
) -> tuple[ClaimCandidate, ...]:
    if not isinstance(raw_projection, dict):
        raise ValueError("candidate projection must be a JSON object")
    keys = set(raw_projection)
    missing = _REQUIRED_OUTER_FIELDS - keys
    unknown = keys - _REQUIRED_OUTER_FIELDS
    if missing:
        raise ValueError(f"candidate projection missing fields: {sorted(missing)}")
    if unknown:
        raise ValueError(f"candidate projection has unknown fields: {sorted(unknown)}")
    if type(raw_projection["schema_version"]) is not int or raw_projection["schema_version"] != 1:
        raise ValueError("candidate projection schema_version must equal 1")
    source_ref = raw_projection["source_ref"]
    source_repository, source_number = _parse_devflow_ref(source_ref, "source_ref")
    if source_repository != source.repository or source_number != source.issue_number:
        raise ValueError("candidate projection source_ref does not identify this Control")
    projection_repository = _parse_repository(raw_projection["repository"], "repository")
    _validate_control(source, control, projection_repository, source_ref)

    raw_candidates = raw_projection["candidates"]
    if not isinstance(raw_candidates, list):
        raise ValueError("candidate projection candidates must be an array")
    envelopes: list[_TaskEnvelope] = []
    seen_tasks: set[str] = set()
    for raw in raw_candidates:
        envelope = _parse_envelope(raw, projection_repository)
        if envelope.task_ref in seen_tasks:
            raise ValueError("candidate projection contains duplicate task envelope")
        seen_tasks.add(envelope.task_ref)
        envelopes.append(envelope)

    candidates: list[ClaimCandidate] = []
    for envelope in envelopes:
        task = reader.read_issue(envelope.task_repository, envelope.task_number)
        _validate_task(envelope, task)
        _validate_work_order(envelope.work_order_ref, reader)
        _validate_role_status(envelope)
        candidates.extend(_candidate_for_role(envelope, role) for role in envelope.roles)
    return tuple(candidates)


def discover_claim_candidates(
    sources: Iterable[DurableIssueSource], reader: IssueReader
) -> DiscoveryResult:
    """Read explicit Control projections with GET-only, fail-closed semantics.

    Each source is the exact Control identity returned by live bootstrap. The
    deprecated owning-Issue marker is intentionally ignored.
    """

    candidates: list[ClaimCandidate] = []
    failures: list[DiscoveryFailure] = []
    for source in sources:
        try:
            control = reader.read_issue(source.repository, source.issue_number)
            raw_projection = _parse_json_block(control.body)
            if raw_projection is None:
                continue
            candidates.extend(
                _validate_projection(source, control, raw_projection, reader)
            )
        except (GitHubApiError, ValueError) as exc:
            failures.append(DiscoveryFailure(source=source, reason=str(exc)))
    return DiscoveryResult(candidates=tuple(candidates), failures=tuple(failures))
