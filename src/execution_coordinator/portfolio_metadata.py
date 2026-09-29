from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from .capability import CAPABILITY_SCHEMA_VERSION, CandidateRequirements
from .discovery import IssueDocument, MARKER_BEGIN, MARKER_END, _sections, _strip_code_value
from .model import Role
from .query import ClaimCandidate
from .ranking import (
    RANKING_SCHEMA_VERSION,
    ControlPriority,
    RankingMetadata,
    ReadinessClass,
    candidate_fingerprint,
)

PORTFOLIO_MARKER_BEGIN = "<!-- DEVFLOW_EXECUTION_PORTFOLIO_METADATA_V1_BEGIN -->"
PORTFOLIO_MARKER_END = "<!-- DEVFLOW_EXECUTION_PORTFOLIO_METADATA_V1_END -->"
PORTFOLIO_SCHEMA_VERSION = "execution-portfolio-metadata.v1"

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_TASK = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*$")
_CONTROL = re.compile(r"^kinoko34077/devflow#[1-9][0-9]*$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_TAG = re.compile(r"^[a-z0-9][a-z0-9_.:/-]{0,63}$")

WORK_CLASSES = frozenset(
    {"audit", "triage", "sync-check", "quickfix", "implementation", "formal-review"}
)
_OUTER_FIELDS = frozenset({"schema_version", "source_ref", "repository", "entries"})
_ENTRY_REQUIRED_FIELDS = frozenset(
    {
        "task",
        "role",
        "task_body_sha256",
        "candidate_fingerprint",
        "controller_urgency",
        "dependency_ready",
        "dependency_order",
        "readiness_class",
        "ready_at",
        "required_capabilities",
        "required_environment",
        "observed_at",
        "fresh_until",
    }
)
_ENTRY_FIELDS = _ENTRY_REQUIRED_FIELDS | {"work_class"}


class PortfolioMetadataError(ValueError):
    """Portfolio companion evidence is absent, malformed, stale, or unbound."""


@dataclass(frozen=True, slots=True)
class PortfolioCandidateMetadata:
    ranking: RankingMetadata
    requirements: CandidateRequirements
    task_body_sha256: str
    work_class: str | None = None


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PortfolioMetadataError(f"portfolio metadata contains duplicate key: {key}")
        result[key] = value
    return result


def _json_block(body: str, begin_marker: str, end_marker: str, label: str) -> object | None:
    begins = body.count(begin_marker)
    ends = body.count(end_marker)
    if begins == 0 and ends == 0:
        return None
    if begins != 1 or ends != 1:
        raise PortfolioMetadataError(f"{label} must contain exactly one begin/end pair")
    begin = body.find(begin_marker)
    end = body.find(end_marker)
    if end < begin + len(begin_marker):
        raise PortfolioMetadataError(f"{label} framing is reversed or malformed")
    raw = body[begin + len(begin_marker) : end].strip()
    if not raw:
        raise PortfolioMetadataError(f"{label} JSON is empty")
    try:
        return json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise PortfolioMetadataError(f"{label} JSON is malformed") from exc


def _single_section(document: IssueDocument, name: str) -> str:
    values = _sections(document.body).get(name.casefold(), [])
    if len(values) != 1 or not values[0].strip():
        raise PortfolioMetadataError(f"Control {name} section must appear exactly once")
    return _strip_code_value(values[0]).strip()


def _utc_timestamp(value: object, field: str, *, nullable: bool = False) -> datetime | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value.endswith("Z"):
        raise PortfolioMetadataError(f"{field} must be a UTC RFC3339 timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise PortfolioMetadataError(f"{field} is malformed") from exc
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise PortfolioMetadataError(f"{field} must use UTC")
    return parsed


def _tag_set(value: object, field: str) -> frozenset[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise PortfolioMetadataError(f"{field} must be an array of exact string tags")
    if len(value) != len(set(value)):
        raise PortfolioMetadataError(f"{field} tags must be unique")
    if any(_TAG.fullmatch(item) is None for item in value):
        raise PortfolioMetadataError(f"{field} contains a malformed tag")
    return frozenset(value)


def _v1_bindings(control: IssueDocument) -> dict[tuple[str, Role], str]:
    raw = _json_block(control.body, MARKER_BEGIN, MARKER_END, "candidate projection")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise PortfolioMetadataError("candidate projection must be a JSON object")
    candidates = raw.get("candidates")
    if not isinstance(candidates, list):
        raise PortfolioMetadataError("candidate projection candidates must be an array")
    bindings: dict[tuple[str, Role], str] = {}
    for envelope in candidates:
        if not isinstance(envelope, dict):
            raise PortfolioMetadataError("candidate task envelope must be an object")
        task = envelope.get("task")
        digest = envelope.get("task_body_sha256")
        roles = envelope.get("roles")
        if not isinstance(task, str) or _TASK.fullmatch(task) is None:
            raise PortfolioMetadataError("candidate task is malformed")
        if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
            raise PortfolioMetadataError("candidate task_body_sha256 is malformed")
        if not isinstance(roles, list):
            raise PortfolioMetadataError("candidate roles must be an array")
        for role_entry in roles:
            if not isinstance(role_entry, dict):
                raise PortfolioMetadataError("candidate role entry must be an object")
            try:
                role = Role(role_entry.get("role"))
            except (TypeError, ValueError) as exc:
                raise PortfolioMetadataError("candidate role is unsupported") from exc
            key = (task, role)
            if key in bindings:
                raise PortfolioMetadataError("candidate projection contains duplicate task/role binding")
            bindings[key] = digest
    return bindings


def parse_portfolio_metadata(
    control: IssueDocument,
    *,
    candidates: Iterable[ClaimCandidate],
    now: datetime,
) -> tuple[PortfolioCandidateMetadata, ...]:
    """Validate one Control companion block against the exact v1 candidates.

    The candidate source remains the accepted v1 projection.  This function
    only supplies the explicit scheduling/worker-match evidence required for
    portfolio scope and fails closed rather than inferring missing values.
    """

    if now.tzinfo is None or now.utcoffset() != timezone.utc.utcoffset(now):
        raise PortfolioMetadataError("now must use UTC")
    source_ref = f"{control.repository}#{control.number}"
    if control.repository != "kinoko34077/devflow" or _CONTROL.fullmatch(source_ref) is None:
        raise PortfolioMetadataError("portfolio metadata source must be an exact devflow Control")

    candidate_items = tuple(candidates)
    candidate_by_key: dict[tuple[str, Role], ClaimCandidate] = {}
    for candidate in candidate_items:
        key = (candidate.task, candidate.role)
        if key in candidate_by_key:
            raise PortfolioMetadataError("candidate set contains duplicate task/role identity")
        candidate_by_key[key] = candidate

    bindings = _v1_bindings(control)
    relevant = {key: digest for key, digest in bindings.items() if key in candidate_by_key and key[1] is not Role.RECOVERY}
    if not relevant:
        return ()

    raw = _json_block(
        control.body,
        PORTFOLIO_MARKER_BEGIN,
        PORTFOLIO_MARKER_END,
        "portfolio metadata block",
    )
    if raw is None:
        raise PortfolioMetadataError("portfolio metadata block is missing for an ordinary candidate")
    if not isinstance(raw, dict) or set(raw) != _OUTER_FIELDS:
        raise PortfolioMetadataError("portfolio metadata outer object has unknown or missing fields")
    if raw["schema_version"] != PORTFOLIO_SCHEMA_VERSION:
        raise PortfolioMetadataError("portfolio metadata schema_version is unsupported")
    if raw["source_ref"] != source_ref:
        raise PortfolioMetadataError("portfolio metadata source_ref does not identify this Control")
    repository = _single_section(control, "repository")
    if not _REPOSITORY.fullmatch(repository) or raw["repository"] != repository:
        raise PortfolioMetadataError("portfolio metadata repository does not match this Control")
    try:
        priority = ControlPriority(_single_section(control, "priority"))
    except ValueError as exc:
        raise PortfolioMetadataError("Control Priority is unsupported") from exc

    entries = raw["entries"]
    if not isinstance(entries, list):
        raise PortfolioMetadataError("portfolio metadata entries must be an array")
    by_key: dict[tuple[str, Role], PortfolioCandidateMetadata] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise PortfolioMetadataError("portfolio metadata entry has unknown or missing fields")
        entry_fields = set(entry)
        if not _ENTRY_REQUIRED_FIELDS.issubset(entry_fields) or not entry_fields.issubset(_ENTRY_FIELDS):
            raise PortfolioMetadataError("portfolio metadata entry has unknown or missing fields")
        task = entry["task"]
        if not isinstance(task, str) or _TASK.fullmatch(task) is None:
            raise PortfolioMetadataError("portfolio metadata task is malformed")
        try:
            role = Role(entry["role"])
        except (TypeError, ValueError) as exc:
            raise PortfolioMetadataError("portfolio metadata role is unsupported") from exc
        if role is Role.RECOVERY:
            raise PortfolioMetadataError("recovery demand does not use ordinary portfolio metadata")
        key = (task, role)
        if key in by_key:
            raise PortfolioMetadataError("portfolio metadata contains duplicate task/role entry")
        candidate = candidate_by_key.get(key)
        if candidate is None or key not in relevant:
            raise PortfolioMetadataError("portfolio metadata references an unknown ordinary v1 candidate")
        digest = entry["task_body_sha256"]
        if digest != relevant[key]:
            raise PortfolioMetadataError("portfolio metadata task_body_sha256 does not match the v1 candidate")
        fingerprint = entry["candidate_fingerprint"]
        if fingerprint != candidate_fingerprint(candidate):
            raise PortfolioMetadataError("portfolio metadata candidate fingerprint does not match current candidate")

        work_class = entry.get("work_class")
        if work_class is not None and (
            not isinstance(work_class, str) or work_class not in WORK_CLASSES
        ):
            raise PortfolioMetadataError("work_class is unsupported")

        urgency = entry["controller_urgency"]
        if urgency is not None and (type(urgency) is not int or not 0 <= urgency <= 100):
            raise PortfolioMetadataError("controller_urgency must be 0..100 or null")
        dependency_ready = entry["dependency_ready"]
        dependency_order = entry["dependency_order"]
        if type(dependency_ready) is not bool:
            raise PortfolioMetadataError("dependency_ready must be boolean")
        if dependency_ready:
            if type(dependency_order) is not int or dependency_order < 0:
                raise PortfolioMetadataError("dependency_order is required when dependency_ready is true")
        elif dependency_order is not None:
            raise PortfolioMetadataError("dependency_order must be null when dependency_ready is false")
        try:
            readiness = ReadinessClass(entry["readiness_class"])
        except (TypeError, ValueError) as exc:
            raise PortfolioMetadataError("readiness_class is unsupported") from exc
        ready_at = _utc_timestamp(entry["ready_at"], "ready_at", nullable=True)
        observed_at = _utc_timestamp(entry["observed_at"], "observed_at")
        fresh_until = _utc_timestamp(entry["fresh_until"], "fresh_until")
        assert observed_at is not None and fresh_until is not None
        if fresh_until <= observed_at:
            raise PortfolioMetadataError("portfolio metadata fresh_until must be after observed_at")
        if now < observed_at:
            raise PortfolioMetadataError("portfolio metadata observation is from the future")
        if now > fresh_until:
            raise PortfolioMetadataError("portfolio metadata is stale")
        capabilities = _tag_set(entry["required_capabilities"], "required_capabilities")
        environment = _tag_set(entry["required_environment"], "required_environment")

        try:
            ranking = RankingMetadata(
                schema_version=RANKING_SCHEMA_VERSION,
                source_ref=source_ref,
                task=task,
                role=role,
                candidate_fingerprint=fingerprint,
                control_priority=priority,
                controller_urgency=urgency,
                dependency_ready=dependency_ready,
                dependency_order=dependency_order,
                readiness_class=readiness,
                ready_at=ready_at,
                observed_at=observed_at,
                fresh_until=fresh_until,
            )
            requirements = CandidateRequirements(
                schema_version=CAPABILITY_SCHEMA_VERSION,
                source_ref=source_ref,
                task=task,
                role=role,
                candidate_fingerprint=fingerprint,
                required_capabilities=capabilities,
                required_environment=environment,
                observed_at=observed_at,
                fresh_until=fresh_until,
            )
        except ValueError as exc:
            raise PortfolioMetadataError(str(exc)) from exc
        by_key[key] = PortfolioCandidateMetadata(
            ranking=ranking,
            requirements=requirements,
            task_body_sha256=digest,
            work_class=work_class,
        )

    missing = set(relevant) - set(by_key)
    if missing:
        raise PortfolioMetadataError("portfolio metadata is missing an ordinary candidate task/role entry")
    return tuple(by_key[key] for key in sorted(by_key, key=lambda item: (item[0], item[1].value)))
