from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Callable

from .agent import AgentSession, MutationGateway
from .capability import CapabilityMatch, CapabilityMatchResult
from .engine import CoordinationError
from .model import Role
from .ranking import candidate_fingerprint


AUTONOMOUS_CYCLE_SCHEMA_VERSION = "agent-first-autonomous-cycle.v1"


class CycleStatus(StrEnum):
    NO_CANDIDATE = "NO_CANDIDATE"
    CLAIM_REJECTED = "CLAIM_REJECTED"
    COMPLETED = "COMPLETED"


@dataclass(frozen=True, slots=True)
class AutonomousCycleKeys:
    """Caller-owned idempotency keys for one bounded cycle."""

    claim_idempotency_key: str
    acknowledge_idempotency_key: str
    release_idempotency_key: str

    def __post_init__(self) -> None:
        values = (
            self.claim_idempotency_key,
            self.acknowledge_idempotency_key,
            self.release_idempotency_key,
        )
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError("cycle idempotency keys must not be empty")
        if len(set(values)) != len(values):
            raise ValueError("cycle idempotency keys must be distinct")


@dataclass(frozen=True, slots=True)
class AutonomousAttempt:
    """Identity of one execution attempt and what it published (decision 3C).

    ``published`` holds every exact ``(task, role)`` candidate identity that
    this attempt published, added, or relaxed.  Such a candidate may not be
    consumed by the same attempt; a later, separate attempt may claim it.
    """

    attempt_id: str
    published: frozenset[tuple[str, Role]] = frozenset()

    def __post_init__(self) -> None:
        if not isinstance(self.attempt_id, str) or not self.attempt_id.strip():
            raise ValueError("attempt_id must not be empty")
        if not isinstance(self.published, frozenset):
            raise ValueError("published must be a frozenset of (task, role) identities")
        for item in self.published:
            if (
                not isinstance(item, tuple)
                or len(item) != 2
                or not isinstance(item[0], str)
                or not item[0]
                or not isinstance(item[1], Role)
            ):
                raise ValueError("published must contain only (task, Role) identities")


@dataclass(frozen=True, slots=True)
class CycleOmission:
    task: str
    role: Role
    reason: str


@dataclass(frozen=True, slots=True)
class AutonomousCycleResult:
    schema_version: str
    worker_id: str
    status: CycleStatus
    selected: CapabilityMatch | None
    claim_attempts: int
    claim_id: str | None = None
    generation: int | None = None
    work_result: object | None = None
    rejection_reason: str | None = None
    attempt_id: str | None = None
    omissions: tuple[CycleOmission, ...] = ()


def _no_candidate(
    worker_id: str,
    attempt: AutonomousAttempt,
    omissions: tuple[CycleOmission, ...],
) -> AutonomousCycleResult:
    return AutonomousCycleResult(
        schema_version=AUTONOMOUS_CYCLE_SCHEMA_VERSION,
        worker_id=worker_id,
        status=CycleStatus.NO_CANDIDATE,
        selected=None,
        claim_attempts=0,
        attempt_id=attempt.attempt_id,
        omissions=omissions,
    )


def _claim_rejected(
    frontier: CapabilityMatchResult,
    selected: CapabilityMatch,
    error: CoordinationError,
    attempt: AutonomousAttempt,
    omissions: tuple[CycleOmission, ...],
) -> AutonomousCycleResult:
    return AutonomousCycleResult(
        schema_version=AUTONOMOUS_CYCLE_SCHEMA_VERSION,
        worker_id=frontier.worker_id,
        status=CycleStatus.CLAIM_REJECTED,
        selected=selected,
        claim_attempts=1,
        rejection_reason=f"{type(error).__name__}: {error}",
        attempt_id=attempt.attempt_id,
        omissions=omissions,
    )


def run_autonomous_cycle(
    frontier: CapabilityMatchResult,
    gateway: MutationGateway,
    *,
    keys: AutonomousCycleKeys,
    attempt: AutonomousAttempt,
    work: Callable[[AgentSession], object],
    base_sha: str | None = None,
    branch: str | None = None,
) -> AutonomousCycleResult:
    """Run one worker-scoped, at-most-one-claim autonomous cycle.

    The caller is responsible for refreshing discovery, runtime state and
    capability evidence before invoking this bounded execution boundary.  This
    function selects only the first already-eligible match that this attempt
    did not itself publish (decision 3C), submits one claim,
    and never falls through to another candidate after a rejection.  Existing
    ``AgentSession`` fencing and lifecycle semantics remain the authority.
    """

    if not isinstance(frontier, CapabilityMatchResult):
        raise TypeError("frontier must be a CapabilityMatchResult")
    if not isinstance(keys, AutonomousCycleKeys):
        raise TypeError("keys must be an AutonomousCycleKeys")
    if not isinstance(attempt, AutonomousAttempt):
        raise TypeError("attempt must be an AutonomousAttempt")
    if not callable(work):
        raise TypeError("work must be callable")

    # Decision 3C: a candidate this attempt published or relaxed is never
    # consumed by the same attempt.  Excluding it is a selection filter, not a
    # claim attempt, so the one-claim-per-cycle bound is unaffected.
    omissions: list[CycleOmission] = []
    selected: CapabilityMatch | None = None
    for match in frontier.matches:
        identity = (match.candidate.task, match.candidate.role)
        if identity in attempt.published:
            omissions.append(
                CycleOmission(
                    task=match.candidate.task,
                    role=match.candidate.role,
                    reason="candidate was published by this attempt (3C)",
                )
            )
            continue
        selected = match
        break
    cycle_omissions = tuple(omissions)
    if selected is None:
        return _no_candidate(frontier.worker_id, attempt, cycle_omissions)

    if selected.worker_id != frontier.worker_id:
        raise ValueError("selected capability match belongs to another worker")
    if (
        selected.requirements.task != selected.candidate.task
        or selected.requirements.role is not selected.candidate.role
        or selected.requirements.candidate_fingerprint
        != candidate_fingerprint(selected.candidate)
    ):
        raise ValueError("selected capability match has inconsistent requirements")

    session = AgentSession(
        gateway,
        task=selected.candidate.task,
        role=selected.candidate.role,
        worker_id=frontier.worker_id,
        conflict_keys=selected.candidate.conflict_keys,
        base_sha=base_sha,
        branch=branch,
    )
    try:
        session.claim(idempotency_key=keys.claim_idempotency_key)
    except CoordinationError as exc:
        # One serialized rejection ends this discovery cycle.  In particular,
        # do not attempt the next ranked candidate with the same stale read.
        return _claim_rejected(frontier, selected, exc, attempt, cycle_omissions)

    claim_id = session.claim_id
    generation = session.generation
    if claim_id is None or generation is None:
        raise RuntimeError("claimed session did not retain authority")

    # Use the existing AgentSession lifecycle methods directly so the claim
    # rejection can end this cycle without conflating it with an acknowledge
    # or work failure.  AgentSession fences ambiguous continuation responses
    # and validates the terminal release response.
    session.acknowledge(idempotency_key=keys.acknowledge_idempotency_key)
    try:
        work_result = work(session)
    except BaseException as work_error:
        if session.claim_id is not None:
            try:
                session.release(idempotency_key=keys.release_idempotency_key)
            except BaseException as release_error:
                work_error.add_note(f"release after work failure failed: {release_error}")
        raise

    session.release(idempotency_key=keys.release_idempotency_key)
    return AutonomousCycleResult(
        schema_version=AUTONOMOUS_CYCLE_SCHEMA_VERSION,
        worker_id=frontier.worker_id,
        status=CycleStatus.COMPLETED,
        selected=selected,
        claim_attempts=1,
        claim_id=claim_id,
        generation=generation,
        work_result=work_result,
        attempt_id=attempt.attempt_id,
        omissions=cycle_omissions,
    )
