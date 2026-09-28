from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Iterable

from .discovery import (
    DiscoveryFailure,
    DiscoveryResult,
    DurableIssueSource,
    IssueDocument,
    IssueReader,
    TRUSTED_AUTHOR_ASSOCIATIONS,
)
from .github_state import GitHubApiError
from .model import Role
from .query import ClaimCandidate


SCHEMA_VERSION = "development-reconciliation-work.v1"
SOURCE_CONTRACT_VERSION = "development-reconciliation.v1"
MARKER_BEGIN = "<!-- DEVFLOW_RECONCILIATION_WORK_V1_BEGIN -->"
MARKER_END = "<!-- DEVFLOW_RECONCILIATION_WORK_V1_END -->"

_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_TASK_REF = re.compile(r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([1-9][0-9]*)$")
_ENTRY_REF = re.compile(
    r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/issues/([1-9][0-9]*)$"
)
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_RECOVERY_TRANSITIONS = frozenset(
    {"RECOVERY_ASSESSMENT", "SUCCESSOR_ELIGIBILITY_EVALUATION"}
)
_REQUIRED_PUBLICATION_FIELDS = frozenset(
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


def _canonical_digest(body: str) -> str:
    normalized = body.replace("\r\n", "\n").replace("\r", "\n")
    return "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"reconciliation projection contains duplicate JSON key: {key}")
        result[key] = value
    return result


def _require_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _parse_timestamp(value: object) -> str:
    observed_at = _require_string(value, "observed_at")
    try:
        parsed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("observed_at must be an RFC-3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("observed_at must include a timezone offset")
    return observed_at


def _parse_task_ref(value: object) -> tuple[str, int, str]:
    task_ref = _require_string(value, "task_ref")
    match = _TASK_REF.fullmatch(task_ref)
    if match is None:
        raise ValueError("task_ref must be owner/repository#N")
    return match.group(1), int(match.group(2)), task_ref


def _parse_entry_ref(value: object) -> tuple[str, int, str]:
    entry_ref = _require_string(value, "entry_ref")
    match = _ENTRY_REF.fullmatch(entry_ref)
    if match is None:
        raise ValueError("entry_ref must be a canonical GitHub Issue URL")
    return f"{match.group(1)}/{match.group(2)}", int(match.group(3)), entry_ref


def _parse_digest(value: object, field: str) -> str:
    digest = _require_string(value, field)
    if _DIGEST.fullmatch(digest) is None:
        raise ValueError(f"{field} must be sha256:<64 lowercase hex>")
    return digest


def _parse_reason_codes(value: object) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError("reason_codes must be a non-empty array")
    reasons = [_require_string(item, "reason_codes item") for item in value]
    normalized = sorted(set(reasons))
    if reasons != normalized:
        raise ValueError("reason_codes must be sorted and unique")
    return reasons


def _reviewer_context(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"pr_number", "pr_head_sha"}:
        raise ValueError("reviewer context has unknown or missing fields")
    pr_number = value.get("pr_number")
    if not isinstance(pr_number, int) or isinstance(pr_number, bool) or pr_number < 1:
        raise ValueError("reviewer context requires a positive pr_number")
    head = _require_string(value.get("pr_head_sha"), "pr_head_sha")
    if _SHA.fullmatch(head) is None:
        raise ValueError("pr_head_sha must be a lowercase full commit SHA")
    return {"pr_number": pr_number, "pr_head_sha": head}


def _recovery_context(value: object) -> dict[str, Any]:
    required = {
        "predecessor_session_id",
        "checkpoint",
        "next_action",
        "recovery_transition",
        "artifact_refs",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("recovery context has unknown or missing fields")
    transition = _require_string(value.get("recovery_transition"), "recovery_transition")
    if transition not in _RECOVERY_TRANSITIONS:
        raise ValueError("unsupported recovery transition")
    raw_refs = value.get("artifact_refs")
    if not isinstance(raw_refs, list):
        raise ValueError("artifact_refs must be an array")
    refs = [_require_string(item, "artifact_refs item") for item in raw_refs]
    normalized_refs = sorted(set(refs))
    if refs != normalized_refs:
        raise ValueError("artifact_refs must be sorted and unique")
    return {
        "predecessor_session_id": _require_string(
            value.get("predecessor_session_id"), "predecessor_session_id"
        ),
        "checkpoint": _require_string(value.get("checkpoint"), "checkpoint"),
        "next_action": _require_string(value.get("next_action"), "next_action"),
        "recovery_transition": transition,
        "artifact_refs": refs,
    }


def _logical_publication_id(
    *,
    task_ref: str,
    task_digest: str,
    entry_ref: str,
    role: str,
    disposition: str,
    reasons: list[str],
    scope: str,
    context: dict[str, Any],
) -> str:
    identity = {
        "schema_version": SCHEMA_VERSION,
        "source_contract_version": SOURCE_CONTRACT_VERSION,
        "task_ref": task_ref,
        "task_body_sha256": task_digest,
        "entry_ref": entry_ref,
        "role": role,
        "disposition": disposition,
        "reason_codes": reasons,
        "scope": scope,
        "context": context,
    }
    encoded = json.dumps(
        identity,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _validate_control_source(
    source: DurableIssueSource,
    document: IssueDocument,
) -> None:
    if source.repository != "kinoko34077/devflow":
        raise ValueError("reconciliation source must be a trusted devflow Repository Control")
    if document.repository != source.repository or document.number != source.issue_number:
        raise ValueError("Control source identity mismatch")
    if document.html_url != (
        f"https://github.com/{source.repository}/issues/{source.issue_number}"
    ):
        raise ValueError("Control source URL identity mismatch")
    if document.state != "open" or document.is_pull_request:
        raise ValueError("Repository Control must be an open Issue")
    if document.author_association not in TRUSTED_AUTHOR_ASSOCIATIONS:
        raise ValueError("Repository Control author is not trusted")


def _projection_payload(body: str) -> dict[str, object] | None:
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
        payload = json.loads(raw, object_pairs_hook=_reject_duplicate_json_keys)
    except json.JSONDecodeError as exc:
        raise ValueError("reconciliation projection JSON is malformed") from exc
    if not isinstance(payload, dict):
        raise ValueError("reconciliation projection must be an object")
    return payload


def _validate_task_document(
    document: IssueDocument,
    *,
    repository: str,
    issue_number: int,
    entry_ref: str,
    expected_digest: str,
) -> None:
    if document.repository != repository or document.number != issue_number:
        raise ValueError("owning task source identity mismatch")
    if document.html_url != entry_ref:
        raise ValueError("owning task entry_ref identity mismatch")
    if document.state != "open" or document.is_pull_request:
        raise ValueError("owning task must be an open Issue")
    if document.author_association not in TRUSTED_AUTHOR_ASSOCIATIONS:
        raise ValueError("owning task author is not trusted")
    if _canonical_digest(document.body) != expected_digest:
        raise ValueError("owning task body digest is stale or mismatched")


def _parse_publication(
    publication: object,
    *,
    repository: str,
    reader: IssueReader,
) -> ClaimCandidate:
    if not isinstance(publication, dict):
        raise ValueError("reconciliation publication must be an object")
    if set(publication) != _REQUIRED_PUBLICATION_FIELDS:
        raise ValueError("reconciliation publication has unknown or missing fields")
    if publication.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("reconciliation publication has unsupported schema_version")

    publication_id = _parse_digest(publication.get("publication_id"), "publication_id")
    task_repository, task_number, task_ref = _parse_task_ref(publication.get("task_ref"))
    if task_repository != repository:
        raise ValueError("publication task_ref repository identity mismatch")
    task_digest = _parse_digest(publication.get("task_body_sha256"), "task_body_sha256")
    entry_repository, entry_number, entry_ref = _parse_entry_ref(publication.get("entry_ref"))
    if entry_repository != task_repository or entry_number != task_number:
        raise ValueError("entry_ref must identify the exact publication task")

    role_raw = publication.get("role")
    disposition = publication.get("disposition")
    if role_raw == "reviewer":
        if disposition != "NEEDS_REVIEWER":
            raise ValueError("reviewer role/disposition mismatch")
        role = Role.REVIEWER
        context = _reviewer_context(publication.get("context"))
    elif role_raw == "recovery":
        if disposition != "NEEDS_RECOVERY":
            raise ValueError("recovery role/disposition mismatch")
        role = Role.RECOVERY
        context = _recovery_context(publication.get("context"))
    else:
        raise ValueError("reconciliation publication role is unsupported")

    if publication.get("requires_user_confirmation") is not False:
        raise ValueError("reconciliation publication cannot bypass user confirmation")
    reasons = _parse_reason_codes(publication.get("reason_codes"))
    scope = _require_string(publication.get("scope"), "scope")
    _parse_timestamp(publication.get("observed_at"))

    freshness = publication.get("freshness")
    if not isinstance(freshness, dict) or set(freshness) != {
        "source_contract_version",
        "task_body_sha256",
    }:
        raise ValueError("publication freshness has unknown or missing fields")
    if freshness.get("source_contract_version") != SOURCE_CONTRACT_VERSION:
        raise ValueError("publication freshness source contract is unsupported")
    if freshness.get("task_body_sha256") != task_digest:
        raise ValueError("publication freshness digest must match task_body_sha256")

    expected_id = _logical_publication_id(
        task_ref=task_ref,
        task_digest=task_digest,
        entry_ref=entry_ref,
        role=str(role_raw),
        disposition=str(disposition),
        reasons=reasons,
        scope=scope,
        context=context,
    )
    if publication_id != expected_id:
        raise ValueError("publication_id does not match canonical logical evidence")

    task_document = reader.read_issue(task_repository, task_number)
    _validate_task_document(
        task_document,
        repository=task_repository,
        issue_number=task_number,
        entry_ref=entry_ref,
        expected_digest=task_digest,
    )

    return ClaimCandidate(
        task=task_ref,
        role=role,
        entry_ref=entry_ref,
        conflict_keys=(),
        scope_ready=True,
        blocked=False,
        requires_user_confirmation=False,
    )


def _candidates_from_control(
    source: DurableIssueSource,
    document: IssueDocument,
    reader: IssueReader,
) -> tuple[ClaimCandidate, ...]:
    _validate_control_source(source, document)
    payload = _projection_payload(document.body)
    if payload is None:
        return ()
    if set(payload) != {"schema_version", "repository", "publications"}:
        raise ValueError("reconciliation projection has unknown or missing fields")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("reconciliation projection has unsupported schema_version")
    repository = payload.get("repository")
    if not isinstance(repository, str) or _REPOSITORY.fullmatch(repository) is None:
        raise ValueError("reconciliation projection repository must be owner/repository")
    if document.title != f"[REPO] {repository}":
        raise ValueError("Repository Control title/repository identity mismatch")
    publications = payload.get("publications")
    if not isinstance(publications, list):
        raise ValueError("reconciliation projection publications must be an array")

    candidates: list[ClaimCandidate] = []
    seen_ids: set[str] = set()
    seen_task_roles: set[tuple[str, Role]] = set()
    for publication in publications:
        if not isinstance(publication, dict):
            raise ValueError("reconciliation publication must be an object")
        publication_id = publication.get("publication_id")
        if not isinstance(publication_id, str):
            raise ValueError("publication_id must be a string")
        if publication_id in seen_ids:
            raise ValueError("reconciliation publication IDs must be unique")
        seen_ids.add(publication_id)

        candidate = _parse_publication(publication, repository=repository, reader=reader)
        boundary = (candidate.task, candidate.role)
        if boundary in seen_task_roles:
            raise ValueError("reconciliation projection contains duplicate task/role demand")
        seen_task_roles.add(boundary)
        candidates.append(candidate)
    return tuple(candidates)


def discover_reconciliation_claim_candidates(
    sources: Iterable[DurableIssueSource],
    reader: IssueReader,
) -> DiscoveryResult:
    """Adopt canonical devflow reconciliation demand as read-only claim candidates.

    This surface is intentionally separate from normal durable candidate
    admission. Publications remain derived demand; only the existing serialized
    runtime claim path can create execution ownership.
    """

    candidates: list[ClaimCandidate] = []
    failures: list[DiscoveryFailure] = []
    seen_sources: set[tuple[str, int]] = set()
    for source in sources:
        source_key = (source.repository, source.issue_number)
        if source_key in seen_sources:
            failures.append(DiscoveryFailure(source, "duplicate Repository Control source"))
            continue
        seen_sources.add(source_key)
        try:
            document = reader.read_issue(source.repository, source.issue_number)
            candidates.extend(_candidates_from_control(source, document, reader))
        except (GitHubApiError, ValueError, KeyError) as exc:
            failures.append(DiscoveryFailure(source, str(exc)))
    return DiscoveryResult(tuple(candidates), tuple(failures))
