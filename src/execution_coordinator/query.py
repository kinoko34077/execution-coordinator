from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Iterable, Protocol

from .github_state import GitHubApiError, GitHubStateStore, IssueBodyRead
from .model import (
    CoordinatorState,
    Role,
    roles_can_share_conflict_key,
    same_worker_role_conflict,
)
from .snapshot import parse_issue_body, state_to_data


class StateReader(Protocol):
    def load_body(self) -> str: ...


@dataclass(frozen=True, slots=True)
class StateReadResult:
    """Validated runtime state plus optional transport-source freshness metadata."""

    state: CoordinatorState
    source_updated_at: str | None


@dataclass(frozen=True, slots=True)
class ClaimCandidate:
    """Normalized durable-work candidate for read-only eligibility projection.

    The caller remains responsible for deriving these fields from canonical
    devflow / owning-repository state. This object is not a durable task store.
    """

    task: str
    role: Role
    entry_ref: str | None
    conflict_keys: tuple[str, ...] = ()
    scope_ready: bool = True
    blocked: bool = False
    requires_user_confirmation: bool = False


def _candidate_has_runtime_conflict(
    candidate: ClaimCandidate,
    state: CoordinatorState,
    *,
    worker_id: str | None,
) -> bool:
    candidate_keys = set(candidate.conflict_keys)
    for active in state.claims.values():
        if active.task == candidate.task and active.role == candidate.role:
            return True
        if worker_id is not None and same_worker_role_conflict(
            active,
            task=candidate.task,
            role=candidate.role,
            worker_id=worker_id,
        ):
            return True
        if (
            candidate_keys.intersection(active.conflict_keys)
            and not roles_can_share_conflict_key(active.role, candidate.role)
        ):
            return True
    return False


def list_claimable(
    candidates: Iterable[ClaimCandidate],
    state: CoordinatorState,
    *,
    worker_id: str | None = None,
) -> tuple[ClaimCandidate, ...]:
    """Return candidates that remain eligible against current runtime state.

    This is a deterministic, read-only projection. Durable discovery/parsing,
    ranking, capability matching, scheduling, and claim mutation stay outside
    this bounded surface.
    """

    claimable: list[ClaimCandidate] = []
    seen_task_roles: set[tuple[str, Role]] = set()
    for candidate in candidates:
        if not candidate.scope_ready:
            continue
        if candidate.blocked or candidate.requires_user_confirmation:
            continue
        if not candidate.entry_ref or not candidate.entry_ref.strip():
            continue
        if _candidate_has_runtime_conflict(candidate, state, worker_id=worker_id):
            continue
        task_role = (candidate.task, candidate.role)
        if task_role in seen_task_roles:
            continue
        seen_task_roles.add(task_role)
        claimable.append(candidate)
    return tuple(claimable)


def get_state(store: StateReader) -> CoordinatorState:
    """Load and validate the current execution-coordination snapshot.

    This compatibility path remains intentionally read-only and returns only
    runtime authority state, without transport-source metadata.
    """

    return parse_issue_body(store.load_body())


def get_state_result(store: StateReader) -> StateReadResult:
    """Load validated state plus freshness metadata when the reader exposes it."""

    load_with_metadata = getattr(store, "load_body_with_metadata", None)
    if callable(load_with_metadata):
        source = load_with_metadata()
        if not isinstance(source, IssueBodyRead):
            raise RuntimeError("metadata-bearing state read returned an invalid result")
        return StateReadResult(
            state=parse_issue_body(source.body),
            source_updated_at=source.updated_at,
        )
    return StateReadResult(
        state=parse_issue_body(store.load_body()),
        source_updated_at=None,
    )


def _build_store_from_env() -> GitHubStateStore:
    token = os.environ.get("GITHUB_TOKEN", "")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    issue_raw = os.environ.get("STATE_ISSUE_NUMBER", "")
    if not repository:
        raise ValueError("GITHUB_REPOSITORY is required")
    try:
        issue_number = int(issue_raw)
    except ValueError as exc:
        raise ValueError("STATE_ISSUE_NUMBER must be an integer") from exc
    return GitHubStateStore(
        token=token,
        repository=repository,
        issue_number=issue_number,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read execution-coordinator state without mutating authority"
    )
    parser.add_argument("operation", choices=("get_state",))
    args = parser.parse_args(argv)

    try:
        if args.operation != "get_state":
            raise ValueError(f"unsupported query operation: {args.operation}")
        result = get_state_result(_build_store_from_env())
    except (ValueError, GitHubApiError, RuntimeError) as exc:
        print(f"query failed: {exc}", file=sys.stderr)
        return 2

    payload = state_to_data(result.state)
    payload["source_updated_at"] = result.source_updated_at
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
