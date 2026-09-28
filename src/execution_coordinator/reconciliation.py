from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from .discovery import (
    DurableIssueSource,
    DiscoveryFailure,
    DiscoveryResult,
    IssueDocument,
    IssueReader,
    _canonical_digest,
    _parse_entry_ref,
    _parse_repository,
    _parse_task_ref,
    _require_trusted,
    _sections,
    _validate_control,
)
from .github_state import GitHubApiError
from .model import Role
from .query import ClaimCandidate


SCHEMA_VERSION = "development-reconciliation-work.v1"
SOURCE_CONTRACT_VERSION = "development-reconciliation.v1"
MARKER_BEGIN = "<!-- DEVFLOW_RECONCILIATION_WORK_V1_BEGIN -->"
MARKER_END = "<!-- DEVFLOW_RECONCILIATION_WORK_V1_END -->"

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_RECOVERY_TRANSITIONS = frozenset(
    {
        "RECOVERY_ASSESSMENT",
        "SUCCESSOR_ELIGIBILITY_EVALUATION",
    }
)
_PUBLICATION_FIELDS = frozenset(
    {
        "schema_version",
        "publication_id",
        "task_ref",
        "task_body_sha256",
        "entry_ref",
        "role",
        "disposition",
        "reason_codes",
        "scope",
        "requires_user_confirmation",
        "observed_at",
        "freshness",
        "context",
    }
)
_PROJECTION_FIELDS = frozenset({"schema_version", "repository", "publications"})


@dataclass(frozen=True, slots=True)
class ReviewerDemandContext:
    pr_number: int
    pr_head_sha: str


@dataclass(frozen=True, slots=True)
class RecoveryDemandContext:
    predecessor_session_id: str
    checkpoint: str
    next_action: str
    recovery_transition: str
    artifact_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReconciliationClaimCandidate(ClaimCandidate):
    """Transient runtime candidate derived from one canonical reconciliation demand.

    This metadata is read-only execution input. It is not durable task truth and
    is never copied into the runtime state snapshot merely by discovery.
    """

    publication_id: str = ""
    disposition: str = ""
    reason_codes: tuple[str, ...] = ()
    scope: str = ""
    observed_at: str = ""
    task_body_sha256: str = ""
    context: ReviewerDemandContext | RecoveryDemandContext | None = None


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"reconciliation projection contains duplicate JSON key: {key}")
        result[key] = value
    return result


def _parse_projection_block(body: str) -> object | None:
    begin_count = body.count(MARKER_BEGIN)
    end_count = body.count(MARKER_END)
    if begin_count == 0 and end_count == 0:
        return None
    if begin_count != 1 or end_count != 1:
        raise ValueError("reconciliation projection must contain exactly one marker pair")
    begin = body.find(MARKER_BEGIN)
    end = body.find(MARKER_END)
    if end < begin + len(MARKER_BEGIN):
        raise ValueError("reconciliation projection markers are malformed")
    raw = body[begin + len(MARKER_BEGIN) : end].strip()
    if not raw:
        raise ValueError("reconciliation projection JSON is empty")
    try:
        return json.loads(raw, object_pairs_hook=_reject_duplicate_json_keys)
    except json.JSONDecodeError as exc:
        raise ValueError("reconciliation projection JSON is malformed") from exc


def _nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _digest(value: object, field: str) -> str:
    digest = _nonempty(value, field)
    if _DIGEST.fullmatch(digest) is None:
        raise ValueError(f"{field} must be sha256:<64 lowercase hex>")
    return digest


def _observed_at(value: object) -> str:
    observed_at = _nonempty(value, "observed_at")
    try:
        parsed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("observed_at must be an RFC-3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("observed_at must include an RFC-3339 timezone offset")
    return observed_at


def _reason_codes(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("reason_codes must be a non-empty array")
    normalized: list[str] = []
    for item in value:
        code = _nonempty(item, "reason_codes item")
        if code not in normalized:
            normalized.append(code)
    return tuple(sorted(normalized))


def _reviewer_context(value: object) -> tuple[ReviewerDemandContext, dict[str, object]]:
    if not isinstance(value, dict) or set(value) != {"pr_number", "pr_head_sha"}:
        raise ValueError("reviewer context has unknown or missing fields")
    pr_number = value["pr_number"]
    if not isinstance(pr_number, int) or isinstance(pr_number, bool) or pr_number < 1:
        raise ValueError("reviewer context pr_number must be a positive integer")
    head = _nonempty(value["pr_head_sha"], "pr_head_sha")
    if _SHA.fullmatch(head) is None:
        raise ValueError("pr_head_sha must be a full commit SHA")
    normalized = {"pr_number": pr_number, "pr_head_sha": head.lower()}
    return ReviewerDemandContext(pr_number, head.lower()), normalized


def _recovery_context(value: object) -> tuple[RecoveryDemandContext, dict[str, object]]:
    required = {
        "predecessor_session_id",
        "checkpoint",
        "next_action",
        "recovery_transition",
        "artifact_refs",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("recovery context has unknown or missing fields")
    predecessor = _nonempty(value["predecessor_session_id"], "predecessor_session_id")
    checkpoint = _nonempty(value["checkpoint"], "checkpoint")
    next_action = _nonempty(value["next_action"], "next_action")
    transition = _nonempty(value["recovery_transition"], "recovery_transition")
    if transition not in _RECOVERY_TRANSITIONS:
        raise ValueError("unsupported recovery transition")
    raw_refs = value["artifact_refs"]
    if not isinstance(raw_refs, list):
        raise ValueError("artifact_refs must be an array")
    refs: list[str] = []
    for item in raw_refs:
        ref = _nonempty(item, "artifact_refs item")
        if ref not in refs:
            refs.append(ref)
    refs.sort()
    context = RecoveryDemandContext(
        predecessor_session_id=predecessor,
        checkpoint=checkpoint,
        next_action=next_action,
        recovery_transition=transition,
        artifact_refs=tuple(refs),
    )
    normalized = {
        "predecessor_session_id": predecessor,
        "checkpoint": checkpoint,
        "next_action": next_action,
        "recovery_transition": transition,
        "artifact_refs": refs,
    }
    return context, normalized


def _logical_publication_id(identity: dict[str, object]) -> str:
    encoded = json.dumps(
        identity,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _task_has_current_human_gate(document: IssueDocument) -> bool:
    sections = _sections(document.body)
    next_actions = sections.get("next action", [])
    if not next_actions:
        return False
    current = next_actions[-1]
    return "[USER_DECISION]" in current or "[HUMAN_GATE]" in current


def _validate_task(
    document: IssueDocument,
    *,
    task_repository: str,
    task_number: int,
    entry_ref: str,
    task_digest: str,
) -> None:
    if document.repository != task_repository or document.number != task_number:
        raise ValueError("owning task identity did not match the reconciliation publication")
    if document.is_pull_request or document.state.casefold() != "open":
        raise ValueError("reconciliation owning task must be an open Issue")
    _require_trusted(document, "owning task")
    if not document.body.strip():
        raise ValueError("owning task body must be non-empty")
    if document.html_url != entry_ref:
        raise ValueError("entry_ref does not match the owning task URL")
    if _canonical_digest(document.body) != task_digest:
        raise ValueError("task_body_sha256 digest mismatch")
    if _task_has_current_human_gate(document):
        raise ValueError("owning task Next Action contains a current user or Human Gate")


def _parse_publication(
    value: object,
    *,
    repository: str,
) -> tuple[ReconciliationClaimCandidate, str, Role]:
    if not isinstance(value, dict):
        raise ValueError("reconciliation publication must be an object")
    keys = set(value)
    missing = _PUBLICATION_FIELDS - keys
    unknown = keys - _PUBLICATION_FIELDS
    if missing:
        raise ValueError(f"reconciliation publication missing fields: {sorted(missing)}")
    if unknown:
        raise ValueError(f"reconciliation publication has unknown fields: {sorted(unknown)}")
    if value["schema_version"] != SCHEMA_VERSION:
        raise ValueError("reconciliation publication has unsupported schema_version")

    publication_id = _digest(value["publication_id"], "publication_id")
    task_ref = _nonempty(value["task_ref"], "task_ref")
    task_repository, task_number = _parse_task_ref(task_ref, "task_ref")
    if task_repository != repository:
        raise ValueError("publication task_ref must belong to the projected repository")
    task_digest = _digest(value["task_body_sha256"], "task_body_sha256")
    entry_ref = _nonempty(value["entry_ref"], "entry_ref")
    entry_repository, entry_number = _parse_entry_ref(entry_ref)
    if entry_repository != task_repository or entry_number != task_number:
        raise ValueError("entry_ref must identify the exact publication task")

    role_raw = _nonempty(value["role"], "role")
    disposition = _nonempty(value["disposition"], "disposition")
    if role_raw == "reviewer":
        if disposition != "NEEDS_REVIEWER":
            raise ValueError("reviewer publication requires NEEDS_REVIEWER disposition")
        role = Role.REVIEWER
        context, normalized_context = _reviewer_context(value["context"])
    elif role_raw == "recovery":
        if disposition != "NEEDS_RECOVERY":
            raise ValueError("recovery publication requires NEEDS_RECOVERY disposition")
        role = Role.RECOVERY
        context, normalized_context = _recovery_context(value["context"])
    else:
        raise ValueError("reconciliation publication role is unsupported")

    if value["requires_user_confirmation"] is not False:
        raise ValueError("publishable reconciliation work must not require user confirmation")
    reasons = _reason_codes(value["reason_codes"])
    if role is Role.REVIEWER and "DIFFERENT_REVIEWER_REQUIRED" not in reasons:
        raise ValueError(
            "reviewer publication requires explicit different-reviewer reason evidence"
        )
    scope = _nonempty(value["scope"], "scope")
    observed_at = _observed_at(value["observed_at"])

    freshness = value["freshness"]
    if not isinstance(freshness, dict) or set(freshness) != {
        "source_contract_version",
        "task_body_sha256",
    }:
        raise ValueError("publication freshness has unknown or missing fields")
    if freshness["source_contract_version"] != SOURCE_CONTRACT_VERSION:
        raise ValueError("publication freshness source contract is unsupported")
    if freshness["task_body_sha256"] != task_digest:
        raise ValueError("publication freshness digest must match task_body_sha256")

    logical_identity: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "source_contract_version": SOURCE_CONTRACT_VERSION,
        "task_ref": task_ref,
        "task_body_sha256": task_digest,
        "entry_ref": entry_ref,
        "role": role_raw,
        "disposition": disposition,
        "reason_codes": list(reasons),
        "scope": scope,
        "context": normalized_context,
    }
    if _logical_publication_id(logical_identity) != publication_id:
        raise ValueError("publication_id does not match canonical logical evidence")

    candidate = ReconciliationClaimCandidate(
        task=task_ref,
        role=role,
        entry_ref=entry_ref,
        conflict_keys=(),
        scope_ready=True,
        blocked=False,
        requires_user_confirmation=False,
        publication_id=publication_id,
        disposition=disposition,
        reason_codes=reasons,
        scope=scope,
        observed_at=observed_at,
        task_body_sha256=task_digest,
        context=context,
    )
    return candidate, task_ref, role


def _validate_projection(
    source: DurableIssueSource,
    control: IssueDocument,
    raw_projection: object,
    reader: IssueReader,
) -> tuple[ReconciliationClaimCandidate, ...]:
    if not isinstance(raw_projection, dict):
        raise ValueError("reconciliation projection must be a JSON object")
    keys = set(raw_projection)
    missing = _PROJECTION_FIELDS - keys
    unknown = keys - _PROJECTION_FIELDS
    if missing:
        raise ValueError(f"reconciliation projection missing fields: {sorted(missing)}")
    if unknown:
        raise ValueError(f"reconciliation projection has unknown fields: {sorted(unknown)}")
    if raw_projection["schema_version"] != SCHEMA_VERSION:
        raise ValueError("reconciliation projection has unsupported schema_version")
    repository = _parse_repository(raw_projection["repository"], "repository")

    _validate_control(
        source,
        control,
        repository,
        f"{source.repository}#{source.issue_number}",
    )

    raw_publications = raw_projection["publications"]
    if not isinstance(raw_publications, list):
        raise ValueError("reconciliation projection publications must be an array")

    candidates: list[ReconciliationClaimCandidate] = []
    seen_publication_ids: set[str] = set()
    seen_task_roles: set[tuple[str, Role]] = set()
    for raw in raw_publications:
        if isinstance(raw, dict):
            raw_task = raw.get("task_ref")
            raw_role = raw.get("role")
            if (
                isinstance(raw_task, str)
                and isinstance(raw_role, str)
                and raw_role in {"reviewer", "recovery"}
            ):
                projected_role = Role.REVIEWER if raw_role == "reviewer" else Role.RECOVERY
                boundary = (raw_task, projected_role)
                if boundary in seen_task_roles:
                    raise ValueError("reconciliation projection contains duplicate task/role demand")
                seen_task_roles.add(boundary)

        candidate, task_ref, role = _parse_publication(raw, repository=repository)
        if candidate.publication_id in seen_publication_ids:
            raise ValueError("reconciliation projection publication IDs must be unique")
        seen_publication_ids.add(candidate.publication_id)

        task_repository, task_number = _parse_task_ref(task_ref, "task_ref")
        task = reader.read_issue(task_repository, task_number)
        _validate_task(
            task,
            task_repository=task_repository,
            task_number=task_number,
            entry_ref=candidate.entry_ref or "",
            task_digest=candidate.task_body_sha256,
        )
        candidates.append(candidate)

    return tuple(candidates)


def discover_reconciliation_candidates(
    sources: Iterable[DurableIssueSource],
    reader: IssueReader,
) -> DiscoveryResult:
    """Project canonical devflow reconciliation demand into runtime candidates.

    Discovery is GET-only and fail-closed. It does not claim work, mutate the
    Repository Control, choose a worker/provider, or update durable task state.
    Returned candidates enter the existing read-only claimability projection and
    serialized runtime claim authority like other normalized candidates.
    """

    candidates: list[ClaimCandidate] = []
    failures: list[DiscoveryFailure] = []
    for source in sources:
        try:
            control = reader.read_issue(source.repository, source.issue_number)
            raw_projection = _parse_projection_block(control.body)
            if raw_projection is None:
                continue
            candidates.extend(_validate_projection(source, control, raw_projection, reader))
        except (GitHubApiError, KeyError, ValueError) as exc:
            failures.append(DiscoveryFailure(source=source, reason=str(exc)))
    return DiscoveryResult(candidates=tuple(candidates), failures=tuple(failures))
