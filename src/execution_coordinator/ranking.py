from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Iterable

from .discovery import DiscoveryFailure
from .managed_frontier import FrontierSourceFailure, ManagedFrontierResult
from .model import Role
from .query import ClaimabilityReason, ClaimCandidate


RANKING_SCHEMA_VERSION = "managed-frontier-ranking.v1"

_CONTROL_REF = re.compile(r"^kinoko34077/devflow#[1-9][0-9]*$")
_TASK_REF = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*$")
_FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{64}$")


class ControlPriority(StrEnum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class ReadinessClass(StrEnum):
    SPECIFY = "SPECIFY"
    IMPLEMENT = "IMPLEMENT"
    VERIFY = "VERIFY"
    REVIEW = "REVIEW"
    MERGE = "MERGE"


_PRIORITY_ORDER = {
    ControlPriority.P0: 0,
    ControlPriority.P1: 1,
    ControlPriority.P2: 2,
    ControlPriority.P3: 3,
}
_READINESS_ORDER = {
    ReadinessClass.SPECIFY: 0,
    ReadinessClass.IMPLEMENT: 1,
    ReadinessClass.VERIFY: 2,
    ReadinessClass.REVIEW: 3,
    ReadinessClass.MERGE: 4,
}


def _require_utc(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must use UTC timezone")


def candidate_fingerprint(candidate: ClaimCandidate) -> str:
    """Return a stable fingerprint for one validated frontier candidate."""

    payload = {
        "task": candidate.task,
        "role": candidate.role.value,
        "entry_ref": candidate.entry_ref,
        "conflict_keys": list(candidate.conflict_keys),
        "scope_ready": candidate.scope_ready,
        "blocked": candidate.blocked,
        "requires_user_confirmation": candidate.requires_user_confirmation,
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class RankingMetadata:
    """Explicit, provenance-bound metadata required by the Phase 2 policy."""

    schema_version: str
    source_ref: str
    task: str
    role: Role
    candidate_fingerprint: str
    control_priority: ControlPriority
    controller_urgency: int | None
    dependency_ready: bool
    dependency_order: int | None
    readiness_class: ReadinessClass
    ready_at: datetime | None
    observed_at: datetime
    fresh_until: datetime

    def __post_init__(self) -> None:
        if self.schema_version != RANKING_SCHEMA_VERSION:
            raise ValueError("ranking metadata schema_version is unsupported")
        if not isinstance(self.source_ref, str) or _CONTROL_REF.fullmatch(self.source_ref) is None:
            raise ValueError("ranking metadata source_ref must be an exact devflow Control")
        if not isinstance(self.task, str) or _TASK_REF.fullmatch(self.task) is None:
            raise ValueError("ranking metadata task must be an exact task reference")
        if not isinstance(self.role, Role):
            raise ValueError("ranking metadata role is unsupported")
        if not isinstance(self.candidate_fingerprint, str) or _FINGERPRINT.fullmatch(
            self.candidate_fingerprint
        ) is None:
            raise ValueError("ranking metadata candidate_fingerprint is malformed")
        if not isinstance(self.control_priority, ControlPriority):
            raise ValueError("ranking metadata control_priority is unsupported")
        if self.controller_urgency is not None and (
            type(self.controller_urgency) is not int
            or not 0 <= self.controller_urgency <= 100
        ):
            raise ValueError("ranking metadata controller_urgency must be 0..100 or null")
        if type(self.dependency_ready) is not bool:
            raise ValueError("ranking metadata dependency_ready must be boolean")
        if self.dependency_ready:
            if (
                type(self.dependency_order) is not int
                or self.dependency_order < 0
            ):
                raise ValueError(
                    "ranking metadata dependency_order is required when dependency_ready is true"
                )
        elif self.dependency_order is not None:
            raise ValueError(
                "ranking metadata dependency_order must be null when dependency_ready is false"
            )
        if not isinstance(self.readiness_class, ReadinessClass):
            raise ValueError("ranking metadata readiness_class is unsupported")
        _require_utc(self.observed_at, "ranking metadata observed_at")
        _require_utc(self.fresh_until, "ranking metadata fresh_until")
        if self.fresh_until <= self.observed_at:
            raise ValueError("ranking metadata fresh_until must be after observed_at")
        if self.ready_at is not None:
            _require_utc(self.ready_at, "ranking metadata ready_at")


@dataclass(frozen=True, slots=True)
class RankingOmission:
    task: str | None
    role: Role | None
    reason: str
    source_ref: str | None = None


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    candidate: ClaimCandidate
    metadata: RankingMetadata
    rank_key: tuple[object, ...]


@dataclass(frozen=True, slots=True)
class RankedFrontierResult:
    ranked: tuple[RankedCandidate, ...]
    recovery_candidates: tuple[ClaimCandidate, ...]
    omissions: tuple[RankingOmission, ...]
    source_failures: tuple[FrontierSourceFailure, ...]
    discovery_failures: tuple[DiscoveryFailure, ...]


def _candidate_key(candidate: ClaimCandidate) -> tuple[str, Role]:
    return candidate.task, candidate.role


def _rank_key(metadata: RankingMetadata) -> tuple[object, ...]:
    urgency = (
        (0, -metadata.controller_urgency)
        if metadata.controller_urgency is not None
        else (1, 0)
    )
    ready_at = (
        (0, metadata.ready_at)
        if metadata.ready_at is not None
        else (1, datetime.max.replace(tzinfo=timezone.utc))
    )
    assert metadata.dependency_order is not None
    return (
        _PRIORITY_ORDER[metadata.control_priority],
        urgency,
        metadata.dependency_order,
        _READINESS_ORDER[metadata.readiness_class],
        ready_at,
        metadata.task,
        metadata.role.value,
    )


def _omission(
    candidate: ClaimCandidate | None,
    reason: str,
    *,
    source_ref: str | None = None,
) -> RankingOmission:
    return RankingOmission(
        task=candidate.task if candidate is not None else None,
        role=candidate.role if candidate is not None else None,
        reason=reason,
        source_ref=source_ref,
    )


def rank_managed_frontier(
    frontier: ManagedFrontierResult,
    metadata: Iterable[RankingMetadata],
    *,
    now: datetime,
) -> RankedFrontierResult:
    """Apply explicit Phase 2 hard filters, then deterministic ranking.

    This function is a pure projection. It never changes the frontier,
    runtime state, or any external Issue. Recovery candidates are returned as
    a separate unranked track.
    """

    _require_utc(now, "now")
    source_refs = {
        f"{source.repository}#{source.issue_number}" for source in frontier.sources
    }
    metadata_by_key: dict[tuple[str, Role], RankingMetadata] = {}
    duplicate_keys: set[tuple[str, Role]] = set()
    omissions: list[RankingOmission] = []
    for item in metadata:
        if not isinstance(item, RankingMetadata):
            omissions.append(_omission(None, "ranking metadata entry has an unsupported type"))
            continue
        key = (item.task, item.role)
        if key in metadata_by_key:
            duplicate_keys.add(key)
            continue
        metadata_by_key[key] = item

    candidate_keys = {
        _candidate_key(candidate)
        for candidate in frontier.fresh_candidates
    }
    for key, item in metadata_by_key.items():
        if key not in candidate_keys:
            omissions.append(
                _omission(
                    None,
                    "ranking metadata references an unknown fresh candidate",
                    source_ref=item.source_ref,
                )
            )

    claimability = {
        _candidate_key(projection.candidate): projection.reason
        for projection in frontier.claimability
    }
    ranked: list[RankedCandidate] = []
    seen_candidates: set[tuple[str, Role]] = set()
    for candidate in frontier.fresh_candidates:
        key = _candidate_key(candidate)
        if key in seen_candidates:
            omissions.append(_omission(candidate, "duplicate fresh candidate identity"))
            continue
        seen_candidates.add(key)

        reason = claimability.get(key)
        if reason is None:
            omissions.append(
                _omission(candidate, "candidate is not durably claimability-eligible")
            )
            continue
        if reason is not ClaimabilityReason.CLAIMABLE:
            omissions.append(
                _omission(candidate, f"runtime claimability is {reason.value}")
            )
            continue
        if key in duplicate_keys:
            omissions.append(_omission(candidate, "duplicate ranking metadata"))
            continue
        item = metadata_by_key.get(key)
        if item is None:
            omissions.append(_omission(candidate, "ranking metadata is missing"))
            continue
        if item.source_ref not in source_refs:
            omissions.append(
                _omission(
                    candidate,
                    "ranking metadata source_ref is not an enumerated Control source",
                    source_ref=item.source_ref,
                )
            )
            continue
        if item.task != candidate.task or item.role is not candidate.role:
            omissions.append(_omission(candidate, "ranking metadata task/role mismatch"))
            continue
        if item.candidate_fingerprint != candidate_fingerprint(candidate):
            omissions.append(_omission(candidate, "ranking metadata fingerprint mismatch"))
            continue
        if now < item.observed_at:
            omissions.append(_omission(candidate, "ranking metadata observation is from the future"))
            continue
        if now > item.fresh_until:
            omissions.append(_omission(candidate, "ranking metadata is stale"))
            continue
        if not item.dependency_ready:
            omissions.append(_omission(candidate, "dependency frontier is not ready"))
            continue

        rank_key = _rank_key(item)
        ranked.append(RankedCandidate(candidate=candidate, metadata=item, rank_key=rank_key))

    ranked.sort(key=lambda item: item.rank_key)
    omissions.sort(
        key=lambda item: (
            item.task or "",
            item.role.value if item.role is not None else "",
            item.reason,
            item.source_ref or "",
        )
    )
    discovery_failures = (
        frontier.read.discovery.failures if frontier.read is not None else ()
    )
    return RankedFrontierResult(
        ranked=tuple(ranked),
        recovery_candidates=frontier.recovery_candidates,
        omissions=tuple(omissions),
        source_failures=frontier.source_failures,
        discovery_failures=discovery_failures,
    )

