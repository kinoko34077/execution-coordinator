from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

from .discovery import DiscoveryFailure
from .managed_frontier import FrontierSourceFailure
from .model import Role
from .query import ClaimCandidate
from .ranking import RankedFrontierResult, RankingOmission, candidate_fingerprint


CAPABILITY_SCHEMA_VERSION = "execution-capability-matching.v1"

_CONTROL_REF = re.compile(r"^kinoko34077/devflow#[1-9][0-9]*$")
_TASK_REF = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*$")
_FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{64}$")
_WORKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$")


def _require_utc(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must use UTC timezone")


def _normalize_tags(value: object, field: str) -> frozenset[str]:
    if not isinstance(value, (frozenset, set, tuple, list)):
        raise ValueError(f"{field} must be a finite collection of tags")
    try:
        tags = frozenset(value)
    except TypeError as exc:
        raise ValueError(f"{field} must contain hashable string tags") from exc
    if any(not isinstance(tag, str) for tag in tags):
        raise ValueError(f"{field} must contain only string tags")
    if any(_TAG.fullmatch(tag) is None for tag in tags):
        raise ValueError(f"{field} contains a malformed tag")
    return tags


def _freshness_reason(
    observed_at: datetime,
    fresh_until: datetime,
    now: datetime,
    *,
    label: str,
) -> str | None:
    if now < observed_at:
        return f"{label} evidence is from the future"
    if now > fresh_until:
        return f"{label} evidence is stale"
    return None


@dataclass(frozen=True, slots=True)
class WorkerProfile:
    """Versioned, provenance-bound worker capability evidence.

    Capability and environment values are opaque exact tags.  The matcher
    never normalizes, aliases, or infers them; a required tag is satisfied
    only when the identical tag is present in this profile.
    """

    schema_version: str
    worker_id: str
    source_ref: str
    capabilities: frozenset[str]
    environment: frozenset[str]
    observed_at: datetime
    fresh_until: datetime

    def __post_init__(self) -> None:
        if self.schema_version != CAPABILITY_SCHEMA_VERSION:
            raise ValueError("worker profile schema_version is unsupported")
        if not isinstance(self.worker_id, str) or _WORKER_ID.fullmatch(self.worker_id) is None:
            raise ValueError("worker profile worker_id is malformed")
        if self.source_ref != f"worker:{self.worker_id}":
            raise ValueError("worker profile source_ref must match worker_id")
        object.__setattr__(
            self,
            "capabilities",
            _normalize_tags(self.capabilities, "worker profile capabilities"),
        )
        object.__setattr__(
            self,
            "environment",
            _normalize_tags(self.environment, "worker profile environment"),
        )
        _require_utc(self.observed_at, "worker profile observed_at")
        _require_utc(self.fresh_until, "worker profile fresh_until")
        if self.fresh_until <= self.observed_at:
            raise ValueError("worker profile fresh_until must be after observed_at")


@dataclass(frozen=True, slots=True)
class CandidateRequirements:
    """Versioned requirements bound to one ranked candidate and Control."""

    schema_version: str
    source_ref: str
    task: str
    role: Role
    candidate_fingerprint: str
    required_capabilities: frozenset[str]
    required_environment: frozenset[str]
    observed_at: datetime
    fresh_until: datetime

    def __post_init__(self) -> None:
        if self.schema_version != CAPABILITY_SCHEMA_VERSION:
            raise ValueError("candidate requirements schema_version is unsupported")
        if not isinstance(self.source_ref, str) or _CONTROL_REF.fullmatch(self.source_ref) is None:
            raise ValueError("candidate requirements source_ref must be an exact devflow Control")
        if not isinstance(self.task, str) or _TASK_REF.fullmatch(self.task) is None:
            raise ValueError("candidate requirements task must be an exact task reference")
        if not isinstance(self.role, Role):
            raise ValueError("candidate requirements role is unsupported")
        if not isinstance(self.candidate_fingerprint, str) or _FINGERPRINT.fullmatch(
            self.candidate_fingerprint
        ) is None:
            raise ValueError("candidate requirements candidate_fingerprint is malformed")
        object.__setattr__(
            self,
            "required_capabilities",
            _normalize_tags(
                self.required_capabilities,
                "candidate requirements required_capabilities",
            ),
        )
        object.__setattr__(
            self,
            "required_environment",
            _normalize_tags(
                self.required_environment,
                "candidate requirements required_environment",
            ),
        )
        _require_utc(self.observed_at, "candidate requirements observed_at")
        _require_utc(self.fresh_until, "candidate requirements fresh_until")
        if self.fresh_until <= self.observed_at:
            raise ValueError(
                "candidate requirements fresh_until must be after observed_at"
            )


@dataclass(frozen=True, slots=True)
class CapabilityOmission:
    task: str | None
    role: Role | None
    reason: str
    source_ref: str | None = None


@dataclass(frozen=True, slots=True)
class CapabilityMatch:
    candidate: ClaimCandidate
    requirements: CandidateRequirements
    worker_id: str


@dataclass(frozen=True, slots=True)
class CapabilityMatchResult:
    worker_id: str
    matches: tuple[CapabilityMatch, ...]
    recovery_candidates: tuple[ClaimCandidate, ...]
    omissions: tuple[CapabilityOmission, ...]
    ranking_omissions: tuple[RankingOmission, ...]
    source_failures: tuple[FrontierSourceFailure, ...]
    discovery_failures: tuple[DiscoveryFailure, ...]


def _omission(
    candidate: ClaimCandidate | None,
    reason: str,
    *,
    source_ref: str | None = None,
) -> CapabilityOmission:
    return CapabilityOmission(
        task=candidate.task if candidate is not None else None,
        role=candidate.role if candidate is not None else None,
        reason=reason,
        source_ref=source_ref,
    )


def _candidate_key(candidate: ClaimCandidate) -> tuple[str, Role]:
    return candidate.task, candidate.role


def _result_failures(frontier: RankedFrontierResult) -> tuple[
    tuple[FrontierSourceFailure, ...], tuple[DiscoveryFailure, ...]
]:
    return frontier.source_failures, frontier.discovery_failures


def match_ranked_frontier(
    frontier: RankedFrontierResult,
    worker: WorkerProfile,
    requirements: Iterable[CandidateRequirements],
    *,
    now: datetime,
) -> CapabilityMatchResult:
    """Match one worker against the already-ranked fresh frontier.

    This is a pure, worker-local projection.  It does not select, claim,
    schedule, publish, or mutate runtime state.  Recovery candidates and all
    Phase 2 omission/failure evidence remain separate from fresh matches.
    """

    if not isinstance(frontier, RankedFrontierResult):
        raise TypeError("frontier must be a RankedFrontierResult")
    if not isinstance(worker, WorkerProfile):
        raise TypeError("worker must be a WorkerProfile")
    _require_utc(now, "now")

    requirement_items = tuple(requirements)
    by_key: dict[tuple[str, Role], CandidateRequirements] = {}
    duplicate_keys: set[tuple[str, Role]] = set()
    omissions: list[CapabilityOmission] = []
    for requirement in requirement_items:
        if not isinstance(requirement, CandidateRequirements):
            omissions.append(
                _omission(None, "candidate requirements entry has an unsupported type")
            )
            continue
        key = (requirement.task, requirement.role)
        if key in by_key:
            duplicate_keys.add(key)
            continue
        by_key[key] = requirement

    ranked_keys = {_candidate_key(item.candidate) for item in frontier.ranked}
    for requirement in by_key.values():
        key = (requirement.task, requirement.role)
        if key not in ranked_keys:
            omissions.append(
                _omission(
                    None,
                    "candidate requirements reference an unknown ranked candidate",
                    source_ref=requirement.source_ref,
                )
            )

    worker_freshness = _freshness_reason(
        worker.observed_at,
        worker.fresh_until,
        now,
        label="worker profile",
    )
    matches: list[CapabilityMatch] = []
    seen_keys: set[tuple[str, Role]] = set()
    for item in frontier.ranked:
        candidate = item.candidate
        key = _candidate_key(candidate)
        if key in seen_keys:
            omissions.append(_omission(candidate, "duplicate ranked candidate identity"))
            continue
        seen_keys.add(key)

        if worker_freshness is not None:
            omissions.append(_omission(candidate, worker_freshness))
            continue
        if candidate.role is Role.RECOVERY:
            omissions.append(
                _omission(candidate, "recovery candidate cannot enter fresh matching")
            )
            continue

        ranking_freshness = _freshness_reason(
            item.metadata.observed_at,
            item.metadata.fresh_until,
            now,
            label="ranking metadata",
        )
        if ranking_freshness is not None:
            omissions.append(_omission(candidate, ranking_freshness))
            continue
        if item.metadata.ready_at is not None and item.metadata.ready_at > now:
            omissions.append(_omission(candidate, "candidate ready_at is in the future"))
            continue
        if item.metadata.task != candidate.task or item.metadata.role is not candidate.role:
            omissions.append(_omission(candidate, "ranking metadata task/role mismatch"))
            continue
        if item.metadata.candidate_fingerprint != candidate_fingerprint(candidate):
            omissions.append(_omission(candidate, "ranking fingerprint mismatch"))
            continue

        if key in duplicate_keys:
            omissions.append(_omission(candidate, "duplicate candidate requirements"))
            continue
        requirement = by_key.get(key)
        if requirement is None:
            omissions.append(_omission(candidate, "candidate requirements are missing"))
            continue
        if requirement.source_ref != item.metadata.source_ref:
            omissions.append(
                _omission(
                    candidate,
                    "candidate requirements source_ref does not match ranking provenance",
                    source_ref=requirement.source_ref,
                )
            )
            continue
        if requirement.candidate_fingerprint != candidate_fingerprint(candidate):
            omissions.append(_omission(candidate, "candidate requirements fingerprint mismatch"))
            continue

        requirement_freshness = _freshness_reason(
            requirement.observed_at,
            requirement.fresh_until,
            now,
            label="candidate requirements",
        )
        if requirement_freshness is not None:
            omissions.append(_omission(candidate, requirement_freshness))
            continue

        missing_capabilities = sorted(
            requirement.required_capabilities - worker.capabilities
        )
        missing_environment = sorted(
            requirement.required_environment - worker.environment
        )
        if missing_capabilities or missing_environment:
            details: list[str] = []
            if missing_capabilities:
                details.append(
                    "missing capabilities=" + ",".join(missing_capabilities)
                )
            if missing_environment:
                details.append("missing environment=" + ",".join(missing_environment))
            omissions.append(
                _omission(
                    candidate,
                    "exact worker subset mismatch (" + "; ".join(details) + ")",
                )
            )
            continue

        matches.append(
            CapabilityMatch(
                candidate=candidate,
                requirements=requirement,
                worker_id=worker.worker_id,
            )
        )

    omissions.sort(
        key=lambda item: (
            item.task or "",
            item.role.value if item.role is not None else "",
            item.reason,
            item.source_ref or "",
        )
    )
    source_failures, discovery_failures = _result_failures(frontier)
    return CapabilityMatchResult(
        worker_id=worker.worker_id,
        matches=tuple(matches),
        recovery_candidates=frontier.recovery_candidates,
        omissions=tuple(omissions),
        ranking_omissions=frontier.omissions,
        source_failures=source_failures,
        discovery_failures=discovery_failures,
    )


def match_ranked_frontier_for_workers(
    frontier: RankedFrontierResult,
    workers: Iterable[WorkerProfile],
    requirements: Iterable[CandidateRequirements],
    *,
    now: datetime,
) -> tuple[CapabilityMatchResult, ...]:
    """Return deterministic independent projections for multiple workers."""

    worker_items = tuple(workers)
    if any(not isinstance(worker, WorkerProfile) for worker in worker_items):
        raise TypeError("workers must contain only WorkerProfile values")
    worker_ids = [worker.worker_id for worker in worker_items]
    if len(worker_ids) != len(set(worker_ids)):
        raise ValueError("workers must have unique worker_id values")
    requirement_items = tuple(requirements)
    return tuple(
        match_ranked_frontier(
            frontier,
            worker,
            requirement_items,
            now=now,
        )
        for worker in sorted(worker_items, key=lambda item: item.worker_id)
    )
