from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .model import (
    MAX_IDEMPOTENCY_RECORDS,
    Claim,
    CoordinatorState,
    Event,
    ExecutionState,
    IdempotencyRecord,
    Role,
    WaitReason,
)


BEGIN_MARKER = "<!-- EXECUTION_COORDINATOR_STATE_V1_BEGIN -->"
END_MARKER = "<!-- EXECUTION_COORDINATOR_STATE_V1_END -->"


class SnapshotError(ValueError):
    pass


def _format_datetime(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SnapshotError("snapshot timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise SnapshotError("snapshot timestamp must use UTC Z form")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise SnapshotError(f"invalid snapshot timestamp: {value}") from exc
    return parsed.astimezone(timezone.utc)


def _event_to_data(event: Event) -> dict[str, Any]:
    return {
        "kind": event.kind,
        "claim_id": event.claim_id,
        "task": event.task,
        "role": event.role.value if event.role is not None else None,
        "generation": event.generation,
        "at": _format_datetime(event.at),
        "reason": event.reason,
    }


def _event_from_data(data: object) -> Event:
    if not isinstance(data, dict):
        raise SnapshotError("event must be an object")
    try:
        role_raw = data["role"]
        return Event(
            kind=str(data["kind"]),
            claim_id=data["claim_id"],
            task=data["task"],
            role=Role(role_raw) if role_raw is not None else None,
            generation=data["generation"],
            at=_parse_datetime(data["at"]),
            reason=data["reason"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SnapshotError("invalid event record") from exc


def _claim_to_data(claim: Claim) -> dict[str, Any]:
    return {
        "claim_id": claim.claim_id,
        "generation": claim.generation,
        "task": claim.task,
        "role": claim.role.value,
        "worker_id": claim.worker_id,
        "conflict_keys": list(claim.conflict_keys),
        "claimed_at": _format_datetime(claim.claimed_at),
        "heartbeat_at": _format_datetime(claim.heartbeat_at),
        "last_progress_at": _format_datetime(claim.last_progress_at),
        "lease_until": _format_datetime(claim.lease_until),
        "state": claim.state.value,
        "wait_reason": claim.wait_reason.value if claim.wait_reason is not None else None,
        "evidence_ref": claim.evidence_ref,
        "base_sha": claim.base_sha,
        "branch": claim.branch,
    }


def _claim_from_data(data: object) -> Claim:
    if not isinstance(data, dict):
        raise SnapshotError("claim must be an object")
    try:
        claim = Claim(
            claim_id=str(data["claim_id"]),
            generation=int(data["generation"]),
            task=str(data["task"]),
            role=Role(data["role"]),
            worker_id=str(data["worker_id"]),
            conflict_keys=tuple(str(item) for item in data["conflict_keys"]),
            claimed_at=_parse_datetime(data["claimed_at"]),
            heartbeat_at=_parse_datetime(data["heartbeat_at"]),
            last_progress_at=_parse_datetime(data["last_progress_at"]),
            lease_until=_parse_datetime(data["lease_until"]),
            state=ExecutionState(data["state"]),
            wait_reason=WaitReason(data["wait_reason"]) if data["wait_reason"] is not None else None,
            evidence_ref=data["evidence_ref"],
            base_sha=data["base_sha"],
            branch=data["branch"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SnapshotError("invalid claim record") from exc
    if claim.generation < 1:
        raise SnapshotError("claim generation must be positive")
    return claim


def _validate_authority_invariants(
    claims: dict[str, Claim],
    generations: dict[str, int],
) -> None:
    seen_boundaries: set[str] = set()
    for claim in claims.values():
        boundary = f"{claim.task}|{claim.role.value}"
        if boundary in seen_boundaries:
            raise SnapshotError("multiple active claims share one task/role boundary")
        seen_boundaries.add(boundary)

        if generations.get(boundary) != claim.generation:
            raise SnapshotError("active claim generation does not match generation table")

        if claim.state is ExecutionState.WAITING:
            if claim.wait_reason is None:
                raise SnapshotError("waiting claim requires wait_reason")
            if not isinstance(claim.evidence_ref, str) or not claim.evidence_ref.strip():
                raise SnapshotError("waiting claim requires non-empty evidence_ref")
        elif claim.wait_reason is not None or claim.evidence_ref is not None:
            raise SnapshotError("non-waiting claim must not carry wait metadata")


def state_to_data(state: CoordinatorState) -> dict[str, Any]:
    if state.schema_version != 1:
        raise SnapshotError(f"unsupported schema version: {state.schema_version}")
    if len(state.idempotency) > MAX_IDEMPOTENCY_RECORDS:
        raise SnapshotError("idempotency retention exceeds configured maximum")
    _validate_authority_invariants(state.claims, state.generations)
    return {
        "schema_version": 1,
        "claims": {
            claim_id: _claim_to_data(claim)
            for claim_id, claim in sorted(state.claims.items())
        },
        "generations": dict(sorted(state.generations.items())),
        # Retention order is insertion order. Do not sort this mapping: each
        # Actions mutation reloads the Issue snapshot before applying the next
        # mutation, so preserving order is required for deterministic eviction.
        "idempotency": {
            key: {
                "fingerprint": record.fingerprint,
                "claim_id": record.claim_id,
                "generation": record.generation,
                "events": [_event_to_data(event) for event in record.events],
            }
            for key, record in state.idempotency.items()
        },
    }


def state_from_data(data: object) -> CoordinatorState:
    if not isinstance(data, dict):
        raise SnapshotError("snapshot root must be an object")
    if data.get("schema_version") != 1:
        raise SnapshotError(f"unsupported schema version: {data.get('schema_version')}")
    try:
        claims_raw = data["claims"]
        generations_raw = data["generations"]
        idempotency_raw = data["idempotency"]
    except KeyError as exc:
        raise SnapshotError("snapshot is missing required fields") from exc
    if not isinstance(claims_raw, dict) or not isinstance(generations_raw, dict) or not isinstance(idempotency_raw, dict):
        raise SnapshotError("snapshot maps must be objects")
    if len(idempotency_raw) > MAX_IDEMPOTENCY_RECORDS:
        raise SnapshotError("idempotency retention exceeds configured maximum")

    claims: dict[str, Claim] = {}
    for claim_id, raw in claims_raw.items():
        claim = _claim_from_data(raw)
        if claim.claim_id != claim_id:
            raise SnapshotError("claim map key does not match claim_id")
        claims[str(claim_id)] = claim

    generations: dict[str, int] = {}
    for key, value in generations_raw.items():
        try:
            generation = int(value)
        except (TypeError, ValueError) as exc:
            raise SnapshotError("generation must be an integer") from exc
        if generation < 0:
            raise SnapshotError("generation must not be negative")
        generations[str(key)] = generation

    _validate_authority_invariants(claims, generations)

    idempotency: dict[str, IdempotencyRecord] = {}
    for key, raw in idempotency_raw.items():
        if not isinstance(raw, dict):
            raise SnapshotError("idempotency record must be an object")
        try:
            idempotency[str(key)] = IdempotencyRecord(
                fingerprint=str(raw["fingerprint"]),
                claim_id=raw["claim_id"],
                generation=raw["generation"],
                events=tuple(_event_from_data(item) for item in raw["events"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SnapshotError("invalid idempotency record") from exc

    return CoordinatorState(
        schema_version=1,
        claims=claims,
        generations=generations,
        idempotency=idempotency,
    )


def _marker_bounds(body: str) -> tuple[int, int] | None:
    begin_count = body.count(BEGIN_MARKER)
    end_count = body.count(END_MARKER)
    if begin_count == 0 and end_count == 0:
        return None
    if begin_count != 1 or end_count != 1:
        raise SnapshotError("snapshot must contain exactly one marker pair")
    begin = body.index(BEGIN_MARKER)
    end = body.index(END_MARKER)
    if end < begin:
        raise SnapshotError("snapshot markers are out of order")
    return begin, end


def parse_issue_body(body: str) -> CoordinatorState:
    bounds = _marker_bounds(body)
    if bounds is None:
        return CoordinatorState.empty()
    begin, end = bounds
    payload = body[begin + len(BEGIN_MARKER):end].strip()
    if not payload:
        raise SnapshotError("snapshot payload is empty")
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise SnapshotError("snapshot payload is not valid JSON") from exc
    return state_from_data(data)


def render_issue_body(existing_body: str, state: CoordinatorState) -> str:
    payload = json.dumps(
        state_to_data(state),
        ensure_ascii=False,
        sort_keys=False,
        indent=2,
    )
    block = f"{BEGIN_MARKER}\n{payload}\n{END_MARKER}"
    bounds = _marker_bounds(existing_body)
    if bounds is None:
        separator = "" if not existing_body or existing_body.endswith("\n") else "\n"
        return f"{existing_body}{separator}{block}\n"
    begin, end = bounds
    suffix_start = end + len(END_MARKER)
    return f"{existing_body[:begin]}{block}{existing_body[suffix_start:]}"
