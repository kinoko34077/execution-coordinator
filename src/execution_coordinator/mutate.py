from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import Protocol

from .engine import acknowledge, claim, expire, fail, progress, release, renew, takeover, wait
from .github_state import GitHubApiError, GitHubStateStore
from .model import MutationResult, Role, WaitReason
from .snapshot import parse_issue_body, render_issue_body


class StateStore(Protocol):
    def load_body(self) -> str: ...
    def save_body(self, body: str) -> None: ...
    def add_comment(self, body: str) -> None: ...


LIFECYCLE_EVENT_KINDS = {"CLAIMED", "RELEASED", "FAILED", "EXPIRED"}


def _required(payload: dict[str, object], key: str) -> object:
    if key not in payload:
        raise ValueError(f"missing payload field: {key}")
    return payload[key]


def _event_comment(result: MutationResult) -> str:
    event = result.events[0]
    lines = [
        "### Execution Coordination Event",
        f"- Kind: `{event.kind}`",
        f"- Claim: `{event.claim_id or 'n/a'}`",
        f"- Task: `{event.task or 'n/a'}`",
        f"- Role: `{event.role.value if event.role else 'n/a'}`",
        f"- Generation: `{event.generation if event.generation is not None else 'n/a'}`",
        f"- At: `{event.at.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')}`",
    ]
    if event.reason:
        lines.append(f"- Reason: `{event.reason}`")
    return "\n".join(lines)


def _apply_to_state(
    state,
    *,
    operation: str,
    payload: dict[str, object],
    idempotency_key: str,
    now: datetime,
) -> MutationResult:
    common_claim = {
        "task": str(_required(payload, "task")),
        "role": Role(str(_required(payload, "role"))),
        "worker_id": str(_required(payload, "worker_id")),
        "conflict_keys": tuple(str(item) for item in payload.get("conflict_keys", [])),
        "now": now,
        "idempotency_key": idempotency_key,
        "base_sha": str(payload["base_sha"]) if payload.get("base_sha") is not None else None,
        "branch": str(payload["branch"]) if payload.get("branch") is not None else None,
    }
    if operation == "claim":
        return claim(state, **common_claim)
    if operation == "takeover":
        return takeover(state, **common_claim)
    if operation == "expire":
        return expire(state, now=now, idempotency_key=idempotency_key)

    claim_id = str(_required(payload, "claim_id"))
    generation = int(_required(payload, "generation"))
    common_current = {
        "claim_id": claim_id,
        "generation": generation,
        "now": now,
        "idempotency_key": idempotency_key,
    }
    if operation == "acknowledge":
        return acknowledge(state, **common_current)
    if operation == "renew":
        return renew(state, **common_current)
    if operation == "progress":
        return progress(state, **common_current)
    if operation == "wait":
        return wait(
            state,
            **common_current,
            reason=WaitReason(str(_required(payload, "reason"))),
            evidence_ref=str(_required(payload, "evidence_ref")),
        )
    if operation == "release":
        return release(state, **common_current)
    if operation == "fail":
        return fail(state, **common_current, reason=str(_required(payload, "reason")))
    raise ValueError(f"unsupported operation: {operation}")


def apply_mutation(
    store: StateStore,
    *,
    operation: str,
    payload: dict[str, object],
    idempotency_key: str,
    now: datetime,
) -> MutationResult:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if not idempotency_key.strip():
        raise ValueError("idempotency_key must not be empty")

    existing_body = store.load_body()
    state = parse_issue_body(existing_body)
    result = _apply_to_state(
        state,
        operation=operation,
        payload=payload,
        idempotency_key=idempotency_key,
        now=now.astimezone(timezone.utc),
    )
    next_body = render_issue_body(existing_body, result.state)

    # The body update is the authority commit point. If it fails, no success
    # event may be emitted and callers must treat the mutation as unsuccessful.
    store.save_body(next_body)

    # Event comments are durable audit evidence, not authority. They are best
    # effort after the authoritative snapshot has committed; an idempotent retry
    # must not duplicate them.
    if not result.replayed:
        for event in result.events:
            if event.kind not in LIFECYCLE_EVENT_KINDS:
                continue
            try:
                store.add_comment(_event_comment(
                    MutationResult(
                        state=result.state,
                        claim_id=result.claim_id,
                        generation=result.generation,
                        events=(event,),
                    )
                ))
            except GitHubApiError as exc:
                print(f"warning: lifecycle comment failed: {exc}", file=sys.stderr)
    return result


def _build_store_from_env() -> GitHubStateStore:
    token = os.environ.get("GITHUB_TOKEN", "")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    issue_raw = os.environ.get("STATE_ISSUE_NUMBER", "")
    if not token:
        raise ValueError("GITHUB_TOKEN is required")
    if not repository:
        raise ValueError("GITHUB_REPOSITORY is required")
    try:
        issue_number = int(issue_raw)
    except ValueError as exc:
        raise ValueError("STATE_ISSUE_NUMBER must be an integer") from exc
    return GitHubStateStore(token=token, repository=repository, issue_number=issue_number)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply one serialized execution-coordinator mutation")
    parser.add_argument("--operation", required=True)
    parser.add_argument("--payload-json", required=True)
    parser.add_argument("--idempotency-key", required=True)
    args = parser.parse_args(argv)
    try:
        payload = json.loads(args.payload_json)
        if not isinstance(payload, dict):
            raise ValueError("payload JSON must be an object")
        result = apply_mutation(
            _build_store_from_env(),
            operation=args.operation,
            payload=payload,
            idempotency_key=args.idempotency_key,
            now=datetime.now(timezone.utc),
        )
    except (ValueError, GitHubApiError, RuntimeError) as exc:
        print(f"mutation failed: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "claim_id": result.claim_id,
                "generation": result.generation,
                "replayed": result.replayed,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
