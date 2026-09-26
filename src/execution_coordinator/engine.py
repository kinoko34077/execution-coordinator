from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, uuid5

from .model import (
    Claim,
    CoordinatorState,
    Event,
    ExecutionState,
    IdempotencyRecord,
    MutationResult,
    Role,
    WaitReason,
)


DEFAULT_LEASE_MINUTES = 15


class CoordinationError(RuntimeError):
    pass


class ClaimConflict(CoordinationError):
    pass


class StaleGeneration(CoordinationError):
    pass


class LeaseExpired(CoordinationError):
    pass


class InvalidTransition(CoordinationError):
    pass


class IdempotencyConflict(CoordinationError):
    pass


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _boundary(task: str, role: Role) -> str:
    return f"{task}|{role.value}"


def _fingerprint(operation: str, payload: dict[str, object]) -> str:
    normalized = json.dumps(
        {"operation": operation, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()


def _replay_or_none(
    state: CoordinatorState,
    *,
    idempotency_key: str,
    fingerprint: str,
) -> MutationResult | None:
    existing = state.idempotency.get(idempotency_key)
    if existing is None:
        return None
    if existing.fingerprint != fingerprint:
        raise IdempotencyConflict(f"idempotency key reused with different payload: {idempotency_key}")
    return MutationResult(
        state=state,
        claim_id=existing.claim_id,
        generation=existing.generation,
        replayed=True,
        events=existing.events,
    )


def _record(
    state: CoordinatorState,
    *,
    idempotency_key: str,
    fingerprint: str,
    claim_id: str | None,
    generation: int | None,
    events: tuple[Event, ...] = (),
) -> CoordinatorState:
    records = dict(state.idempotency)
    records[idempotency_key] = IdempotencyRecord(
        fingerprint=fingerprint,
        claim_id=claim_id,
        generation=generation,
        events=events,
    )
    return state.with_maps(idempotency=records)


def _roles_compatible(left: Role, right: Role) -> bool:
    return Role.REVIEWER in (left, right)


def _same_worker_self_review(active: Claim, *, task: str, role: Role, worker_id: str) -> bool:
    return (
        active.task == task
        and active.worker_id == worker_id
        and {active.role, role} == {Role.IMPLEMENTER, Role.REVIEWER}
    )


def _require_current(
    state: CoordinatorState,
    claim_id: str,
    generation: int,
    *,
    now: datetime,
) -> Claim:
    current = state.claims.get(claim_id)
    if current is None or current.generation != generation:
        raise StaleGeneration(f"claim generation is not current: {claim_id}@{generation}")

    protected_wait = current.state is ExecutionState.WAITING and bool(current.evidence_ref)
    if not protected_wait and current.lease_until <= _utc(now):
        raise LeaseExpired(f"claim lease has expired: {claim_id}@{generation}")
    return current


def claim(
    state: CoordinatorState,
    *,
    task: str,
    role: Role,
    worker_id: str,
    conflict_keys: tuple[str, ...],
    now: datetime,
    idempotency_key: str,
    base_sha: str | None = None,
    branch: str | None = None,
    lease_minutes: int = DEFAULT_LEASE_MINUTES,
) -> MutationResult:
    now = _utc(now)
    keys = tuple(sorted(set(conflict_keys)))
    # Runtime wall-clock time is intentionally excluded from the request
    # fingerprint. An Actions/transport retry with the same logical request and
    # idempotency key must replay the original result even when retried later.
    payload = {
        "task": task,
        "role": role.value,
        "worker_id": worker_id,
        "conflict_keys": list(keys),
        "base_sha": base_sha,
        "branch": branch,
        "lease_minutes": lease_minutes,
    }
    fingerprint = _fingerprint("claim", payload)
    replay = _replay_or_none(state, idempotency_key=idempotency_key, fingerprint=fingerprint)
    if replay is not None:
        return replay

    for active in state.claims.values():
        if active.task == task and active.role == role:
            raise ClaimConflict(f"task/role already claimed: {task} {role.value}")
        if _same_worker_self_review(active, task=task, role=role, worker_id=worker_id):
            raise ClaimConflict("same worker cannot hold implementer and independent reviewer roles for one task")
        if set(active.conflict_keys).intersection(keys) and not _roles_compatible(active.role, role):
            raise ClaimConflict(f"conflict key already owned by {active.claim_id}")

    boundary = _boundary(task, role)
    generation = state.generations.get(boundary, 0) + 1
    claim_id = "clm_" + uuid5(
        NAMESPACE_URL,
        f"{task}|{role.value}|{worker_id}|{idempotency_key}|{generation}",
    ).hex
    current = Claim(
        claim_id=claim_id,
        generation=generation,
        task=task,
        role=role,
        worker_id=worker_id,
        conflict_keys=keys,
        claimed_at=now,
        heartbeat_at=now,
        last_progress_at=now,
        lease_until=now + timedelta(minutes=lease_minutes),
        base_sha=base_sha,
        branch=branch,
    )
    claims = dict(state.claims)
    claims[claim_id] = current
    generations = dict(state.generations)
    generations[boundary] = generation
    event = Event("CLAIMED", claim_id, task, role, generation, now)
    next_state = state.with_maps(claims=claims, generations=generations)
    next_state = _record(
        next_state,
        idempotency_key=idempotency_key,
        fingerprint=fingerprint,
        claim_id=claim_id,
        generation=generation,
        events=(event,),
    )
    return MutationResult(next_state, claim_id, generation, False, (event,))


def _update_claim(
    state: CoordinatorState,
    *,
    claim_id: str,
    generation: int,
    now: datetime,
    idempotency_key: str,
    operation: str,
    payload: dict[str, object],
    updater,
) -> MutationResult:
    now = _utc(now)
    # `now` is execution metadata, not logical request identity.
    full_payload = {**payload, "claim_id": claim_id, "generation": generation}
    fingerprint = _fingerprint(operation, full_payload)
    replay = _replay_or_none(state, idempotency_key=idempotency_key, fingerprint=fingerprint)
    if replay is not None:
        return replay
    current = _require_current(state, claim_id, generation, now=now)
    updated = updater(current, now)
    claims = dict(state.claims)
    claims[claim_id] = updated
    next_state = state.with_maps(claims=claims)
    next_state = _record(
        next_state,
        idempotency_key=idempotency_key,
        fingerprint=fingerprint,
        claim_id=claim_id,
        generation=generation,
    )
    return MutationResult(next_state, claim_id, generation)


def acknowledge(
    state: CoordinatorState,
    *,
    claim_id: str,
    generation: int,
    now: datetime,
    idempotency_key: str,
) -> MutationResult:
    def updater(current: Claim, at: datetime) -> Claim:
        if current.state is not ExecutionState.CLAIMED:
            raise InvalidTransition("acknowledge requires CLAIMED state")
        return replace(current, state=ExecutionState.RUNNING, heartbeat_at=at)

    return _update_claim(
        state,
        claim_id=claim_id,
        generation=generation,
        now=now,
        idempotency_key=idempotency_key,
        operation="acknowledge",
        payload={},
        updater=updater,
    )


def renew(
    state: CoordinatorState,
    *,
    claim_id: str,
    generation: int,
    now: datetime,
    idempotency_key: str,
    lease_minutes: int = DEFAULT_LEASE_MINUTES,
) -> MutationResult:
    return _update_claim(
        state,
        claim_id=claim_id,
        generation=generation,
        now=now,
        idempotency_key=idempotency_key,
        operation="renew",
        payload={"lease_minutes": lease_minutes},
        updater=lambda current, at: replace(
            current,
            heartbeat_at=at,
            lease_until=at + timedelta(minutes=lease_minutes),
        ),
    )


def progress(
    state: CoordinatorState,
    *,
    claim_id: str,
    generation: int,
    now: datetime,
    idempotency_key: str,
) -> MutationResult:
    return _update_claim(
        state,
        claim_id=claim_id,
        generation=generation,
        now=now,
        idempotency_key=idempotency_key,
        operation="progress",
        payload={},
        updater=lambda current, at: replace(current, last_progress_at=at),
    )


def wait(
    state: CoordinatorState,
    *,
    claim_id: str,
    generation: int,
    reason: WaitReason,
    evidence_ref: str,
    now: datetime,
    idempotency_key: str,
) -> MutationResult:
    if not evidence_ref.strip():
        raise ValueError("wait requires evidence_ref")
    return _update_claim(
        state,
        claim_id=claim_id,
        generation=generation,
        now=now,
        idempotency_key=idempotency_key,
        operation="wait",
        payload={"reason": reason.value, "evidence_ref": evidence_ref},
        updater=lambda current, at: replace(
            current,
            state=ExecutionState.WAITING,
            wait_reason=reason,
            evidence_ref=evidence_ref,
            heartbeat_at=at,
        ),
    )


def _terminal(
    state: CoordinatorState,
    *,
    claim_id: str,
    generation: int,
    now: datetime,
    idempotency_key: str,
    kind: str,
    reason: str | None = None,
) -> MutationResult:
    now = _utc(now)
    payload = {
        "claim_id": claim_id,
        "generation": generation,
        "reason": reason,
    }
    fingerprint = _fingerprint(kind.lower(), payload)
    replay = _replay_or_none(state, idempotency_key=idempotency_key, fingerprint=fingerprint)
    if replay is not None:
        return replay
    current = _require_current(state, claim_id, generation, now=now)
    claims = dict(state.claims)
    del claims[claim_id]
    event = Event(kind, claim_id, current.task, current.role, generation, now, reason)
    next_state = state.with_maps(claims=claims)
    next_state = _record(
        next_state,
        idempotency_key=idempotency_key,
        fingerprint=fingerprint,
        claim_id=claim_id,
        generation=generation,
        events=(event,),
    )
    return MutationResult(next_state, claim_id, generation, False, (event,))


def release(
    state: CoordinatorState,
    *,
    claim_id: str,
    generation: int,
    now: datetime,
    idempotency_key: str,
) -> MutationResult:
    return _terminal(
        state,
        claim_id=claim_id,
        generation=generation,
        now=now,
        idempotency_key=idempotency_key,
        kind="RELEASED",
    )


def fail(
    state: CoordinatorState,
    *,
    claim_id: str,
    generation: int,
    now: datetime,
    idempotency_key: str,
    reason: str,
) -> MutationResult:
    return _terminal(
        state,
        claim_id=claim_id,
        generation=generation,
        now=now,
        idempotency_key=idempotency_key,
        kind="FAILED",
        reason=reason,
    )


def expire(
    state: CoordinatorState,
    *,
    now: datetime,
    idempotency_key: str,
) -> MutationResult:
    now = _utc(now)
    fingerprint = _fingerprint("expire", {})
    replay = _replay_or_none(state, idempotency_key=idempotency_key, fingerprint=fingerprint)
    if replay is not None:
        return replay
    claims = dict(state.claims)
    events: list[Event] = []
    for claim_id, current in list(claims.items()):
        protected_wait = current.state is ExecutionState.WAITING and bool(current.evidence_ref)
        if current.lease_until <= now and not protected_wait:
            del claims[claim_id]
            events.append(
                Event("EXPIRED", claim_id, current.task, current.role, current.generation, now)
            )
    next_state = state.with_maps(claims=claims)
    next_state = _record(
        next_state,
        idempotency_key=idempotency_key,
        fingerprint=fingerprint,
        claim_id=None,
        generation=None,
        events=tuple(events),
    )
    return MutationResult(next_state, events=tuple(events))


def takeover(state: CoordinatorState, **kwargs) -> MutationResult:
    return claim(state, **kwargs)
