from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


MAX_IDEMPOTENCY_RECORDS = 128


class Role(StrEnum):
    IMPLEMENTER = "implementer"
    REVIEWER = "reviewer"
    RECOVERY = "recovery"
    VERIFIER = "verifier"
    INTEGRATOR = "integrator"


class ExecutionState(StrEnum):
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    WAITING = "WAITING"


class WaitReason(StrEnum):
    CI = "CI"
    REVIEW = "REVIEW"
    USER_DECISION = "USER_DECISION"
    DEPENDENCY = "DEPENDENCY"
    PROVIDER = "PROVIDER"
    RATE_LIMIT = "RATE_LIMIT"
    EXTERNAL = "EXTERNAL"
    SCHEDULE = "SCHEDULE"


@dataclass(frozen=True, slots=True)
class Event:
    kind: str
    claim_id: str | None
    task: str | None
    role: Role | None
    generation: int | None
    at: datetime
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class Claim:
    claim_id: str
    generation: int
    task: str
    role: Role
    worker_id: str
    conflict_keys: tuple[str, ...]
    claimed_at: datetime
    heartbeat_at: datetime
    last_progress_at: datetime
    lease_until: datetime
    state: ExecutionState = ExecutionState.CLAIMED
    wait_reason: WaitReason | None = None
    evidence_ref: str | None = None
    base_sha: str | None = None
    branch: str | None = None


def roles_can_share_conflict_key(left: Role, right: Role) -> bool:
    """Return whether these role claims may overlap one logical conflict key."""

    return Role.REVIEWER in (left, right)


def roles_can_share_task(left: Role, right: Role) -> bool:
    """Return whether distinct role claims may coexist on one task.

    Existing v1 role pairs remain compatible. Recovery is a successor-work
    authority and therefore cannot race another non-reviewer role on the same
    task. Review remains observationally compatible with recovery.
    """

    if left == right:
        return False
    if Role.REVIEWER in (left, right):
        return True
    return Role.RECOVERY not in (left, right)


def same_worker_role_conflict(
    active: Claim,
    *,
    task: str,
    role: Role,
    worker_id: str,
) -> bool:
    """Reject one logical worker acting as implementer and reviewer on a task."""

    return (
        active.task == task
        and active.worker_id == worker_id
        and {active.role, role} == {Role.IMPLEMENTER, Role.REVIEWER}
    )


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    fingerprint: str
    claim_id: str | None
    generation: int | None
    events: tuple[Event, ...] = ()


def _bounded_idempotency(
    records: dict[str, IdempotencyRecord],
) -> dict[str, IdempotencyRecord]:
    """Bound retry memory while preferring durable authority-boundary records.

    Dict insertion order is the retention order. The newest mutation key is
    never evicted by the same write. Non-event records (renew/progress/wait)
    are discarded before significant authority-boundary records whenever
    possible. If the map is entirely authority-boundary records, the oldest
    record is evicted so the state remains strictly bounded.
    """

    bounded = dict(records)
    while len(bounded) > MAX_IDEMPOTENCY_RECORDS:
        keys = list(bounded)
        newest_key = keys[-1]
        eviction_key = next(
            (
                key
                for key in keys[:-1]
                if not bounded[key].events
            ),
            None,
        )
        if eviction_key is None:
            eviction_key = keys[0]
            if eviction_key == newest_key:
                break
        del bounded[eviction_key]
    return bounded


@dataclass(frozen=True, slots=True)
class CoordinatorState:
    schema_version: int = 1
    claims: dict[str, Claim] = field(default_factory=dict)
    generations: dict[str, int] = field(default_factory=dict)
    idempotency: dict[str, IdempotencyRecord] = field(default_factory=dict)

    @classmethod
    def empty(cls) -> "CoordinatorState":
        return cls()

    def with_maps(
        self,
        *,
        claims: dict[str, Claim] | None = None,
        generations: dict[str, int] | None = None,
        idempotency: dict[str, IdempotencyRecord] | None = None,
    ) -> "CoordinatorState":
        next_idempotency = (
            dict(self.idempotency)
            if idempotency is None
            else _bounded_idempotency(idempotency)
        )
        return CoordinatorState(
            schema_version=self.schema_version,
            claims=dict(self.claims if claims is None else claims),
            generations=dict(self.generations if generations is None else generations),
            idempotency=next_idempotency,
        )


@dataclass(frozen=True, slots=True)
class MutationResult:
    state: CoordinatorState
    claim_id: str | None = None
    generation: int | None = None
    replayed: bool = False
    events: tuple[Event, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
