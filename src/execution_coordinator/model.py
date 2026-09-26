from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class Role(StrEnum):
    IMPLEMENTER = "implementer"
    REVIEWER = "reviewer"
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


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    fingerprint: str
    claim_id: str | None
    generation: int | None
    events: tuple[Event, ...] = ()


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
        return CoordinatorState(
            schema_version=self.schema_version,
            claims=dict(self.claims if claims is None else claims),
            generations=dict(self.generations if generations is None else generations),
            idempotency=dict(self.idempotency if idempotency is None else idempotency),
        )


@dataclass(frozen=True, slots=True)
class MutationResult:
    state: CoordinatorState
    claim_id: str | None = None
    generation: int | None = None
    replayed: bool = False
    events: tuple[Event, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
