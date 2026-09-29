from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .capability import CapabilityMatch, WorkerProfile, match_ranked_frontier
from .model import Role
from .portfolio_runtime import PortfolioRuntimeRead
from .query import ClaimabilityReason
from .ranking import ControlPriority, candidate_fingerprint, portable_rank_class_key

CONTROLLER_OFFER_SCHEMA_VERSION = "controller-offer.v1"


class OfferResponseCode(StrEnum):
    ACCEPTED = "ACCEPTED"
    DECLINED_CAPABILITY = "DECLINED_CAPABILITY"
    DECLINED_CONFLICT = "DECLINED_CONFLICT"
    DEFERRED_BUSY = "DEFERRED_BUSY"
    BLOCKED_DEPENDENCY = "BLOCKED_DEPENDENCY"
    REQUIRES_USER_AUTHORITY = "REQUIRES_USER_AUTHORITY"


@dataclass(frozen=True, slots=True)
class ControllerOffer:
    schema_version: str
    task: str
    role: Role
    source_ref: str
    candidate_fingerprint: str
    control_priority: ControlPriority
    controller_urgency: int | None
    rank_key: tuple[object, ...]
    required_capabilities: frozenset[str]
    required_environment: frozenset[str]
    conflict_keys: tuple[str, ...]
    risk: str | None = None
    authority_requirements: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != CONTROLLER_OFFER_SCHEMA_VERSION:
            raise ValueError("controller offer schema_version is unsupported")
        if not self.task.strip() or not self.source_ref.strip():
            raise ValueError("controller offer task/source_ref must not be empty")
        if not self.candidate_fingerprint.startswith("sha256:"):
            raise ValueError("controller offer candidate_fingerprint is malformed")
        object.__setattr__(self, "role", Role(self.role))
        object.__setattr__(self, "control_priority", ControlPriority(self.control_priority))
        object.__setattr__(self, "required_capabilities", frozenset(self.required_capabilities))
        object.__setattr__(self, "required_environment", frozenset(self.required_environment))
        object.__setattr__(self, "conflict_keys", tuple(self.conflict_keys))
        object.__setattr__(self, "authority_requirements", tuple(self.authority_requirements))


def select_controller_offer(
    read: PortfolioRuntimeRead,
    *,
    supported_roles: frozenset[Role] = frozenset({Role.IMPLEMENTER}),
) -> ControllerOffer | None:
    if not isinstance(read, PortfolioRuntimeRead):
        raise TypeError("read must be a PortfolioRuntimeRead")
    roles = frozenset(Role(role) for role in supported_roles)
    if not read.complete or read.ranked is None:
        return None
    requirements = {(item.task, item.role): item for item in read.requirements}
    for ranked in read.ranked.ranked:
        candidate = ranked.candidate
        if candidate.role not in roles:
            continue
        requirement = requirements.get((candidate.task, candidate.role))
        if requirement is None:
            continue
        return ControllerOffer(
            schema_version=CONTROLLER_OFFER_SCHEMA_VERSION,
            task=candidate.task,
            role=candidate.role,
            source_ref=requirement.source_ref,
            candidate_fingerprint=requirement.candidate_fingerprint,
            control_priority=ranked.metadata.control_priority,
            controller_urgency=ranked.metadata.controller_urgency,
            rank_key=portable_rank_class_key(ranked.metadata),
            required_capabilities=requirement.required_capabilities,
            required_environment=requirement.required_environment,
            conflict_keys=candidate.conflict_keys,
            authority_requirements=(),
        )
    return None


def _current_candidate(read: PortfolioRuntimeRead, offer: ControllerOffer):
    matches = [
        candidate
        for candidate in read.frontier.candidates
        if candidate.task == offer.task and candidate.role is offer.role
    ]
    return matches[0] if len(matches) == 1 else None


def evaluate_controller_offer(
    offer: ControllerOffer,
    worker_profile: WorkerProfile,
    *,
    current_read: PortfolioRuntimeRead,
    provider_ready: bool,
    human_gate: bool,
) -> tuple[OfferResponseCode, CapabilityMatch | None]:
    if not isinstance(offer, ControllerOffer):
        raise TypeError("offer must be a ControllerOffer")
    if not isinstance(worker_profile, WorkerProfile):
        raise TypeError("worker_profile must be a WorkerProfile")
    if not isinstance(current_read, PortfolioRuntimeRead):
        raise TypeError("current_read must be a PortfolioRuntimeRead")
    if human_gate or not provider_ready:
        return OfferResponseCode.REQUIRES_USER_AUTHORITY, None

    current = _current_candidate(current_read, offer)
    if current is None or candidate_fingerprint(current) != offer.candidate_fingerprint:
        return OfferResponseCode.DEFERRED_BUSY, None

    claimability = {
        (item.candidate.task, item.candidate.role): item.reason
        for item in current_read.frontier.claimability
    }
    reason = claimability.get((offer.task, offer.role))
    if reason is not ClaimabilityReason.CLAIMABLE:
        return OfferResponseCode.DECLINED_CONFLICT, None

    if not current_read.complete or current_read.ranked is None:
        return OfferResponseCode.DEFERRED_BUSY, None

    ranked_keys = {
        (item.candidate.task, item.candidate.role) for item in current_read.ranked.ranked
    }
    key = (offer.task, offer.role)
    if key not in ranked_keys:
        for omission in current_read.ranked.omissions:
            if omission.task == offer.task and omission.role is offer.role:
                if "dependency" in omission.reason or "ready_at" in omission.reason:
                    return OfferResponseCode.BLOCKED_DEPENDENCY, None
        return OfferResponseCode.DEFERRED_BUSY, None

    requirement = next(
        (
            item
            for item in current_read.requirements
            if item.task == offer.task and item.role is offer.role
        ),
        None,
    )
    if (
        requirement is None
        or requirement.source_ref != offer.source_ref
        or requirement.candidate_fingerprint != offer.candidate_fingerprint
        or requirement.required_capabilities != offer.required_capabilities
        or requirement.required_environment != offer.required_environment
    ):
        return OfferResponseCode.DEFERRED_BUSY, None

    matched = match_ranked_frontier(
        current_read.ranked,
        worker_profile,
        current_read.requirements,
        now=current_read.observed_at,
    )
    match = next(
        (
            item
            for item in matched.matches
            if item.candidate.task == offer.task and item.candidate.role is offer.role
        ),
        None,
    )
    if match is None:
        return OfferResponseCode.DECLINED_CAPABILITY, None
    return OfferResponseCode.ACCEPTED, match
