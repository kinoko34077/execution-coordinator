from __future__ import annotations

import argparse
import json
import os

from .agent import MutationGateway


def expire_stale_claims(
    gateway: MutationGateway,
    *,
    attempt_id: str,
) -> dict[str, object]:
    """Sweep lease-expired runtime claims through the serialized authority lane.

    Discovery deliberately treats expired-but-unswept claims as blockers.  A
    controller cycle therefore performs this bounded authority mutation before
    its read-only portfolio discovery so an ambiguous/dead prior worker cannot
    block the portfolio forever after its lease expires.
    """

    if not isinstance(attempt_id, str) or not attempt_id.strip():
        raise ValueError("attempt_id must not be empty")
    result = gateway.mutate(
        operation="expire",
        payload={},
        idempotency_key=f"{attempt_id}:expire",
    )
    expired = tuple(event for event in result.events if event.kind == "EXPIRED")
    return {
        "state": "EXPIRED" if expired else "NOOP",
        "expired_count": len(expired),
    }


def _emit_outputs(values: dict[str, object]) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if not output:
        return
    with open(output, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - workflow integration
    from .actions_gateway import ActionsMutationGateway
    from .github_state import GitHubStateStore

    parser = argparse.ArgumentParser(
        prog="python -m execution_coordinator.runtime_maintenance"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    expire = sub.add_parser("expire-stale")
    expire.add_argument("--attempt-id", required=True)
    args = parser.parse_args(argv)

    token = os.environ["GITHUB_TOKEN"]
    repository = os.environ["GITHUB_REPOSITORY"]
    state = GitHubStateStore(token=token, repository=repository, issue_number=3)
    gateway = ActionsMutationGateway(
        token=token,
        repository=repository,
        state_reader=state.load_body,
    )
    result = expire_stale_claims(gateway, attempt_id=args.attempt_id)
    _emit_outputs(result)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
