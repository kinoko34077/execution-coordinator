from __future__ import annotations

import unittest
from datetime import datetime, timezone

from execution_coordinator.agent import AgentSession
from execution_coordinator.github_state import GitHubApiError
from execution_coordinator.model import CoordinatorState, MutationResult, Role
from execution_coordinator.mutate import apply_mutation
from execution_coordinator.snapshot import render_issue_body


UTC = timezone.utc
T0 = datetime(2026, 9, 27, 3, 20, tzinfo=UTC)


class _Store:
    def __init__(self) -> None:
        self.body = render_issue_body("# state\n", CoordinatorState.empty())
        self.comments: list[str] = []

    def load_body(self) -> str:
        return self.body

    def save_body(self, body: str) -> None:
        self.body = body

    def add_comment(self, body: str) -> None:
        self.comments.append(body)


class _TransportFailureGateway:
    def __init__(self, failing_operation: str) -> None:
        self.store = _Store()
        self.failing_operation = failing_operation
        self.calls: list[tuple[str, dict[str, object], str]] = []

    def mutate(
        self,
        *,
        operation: str,
        payload: dict[str, object],
        idempotency_key: str,
    ) -> MutationResult:
        self.calls.append((operation, dict(payload), idempotency_key))
        if operation == self.failing_operation:
            raise GitHubApiError(f"{operation} transport failed")
        return apply_mutation(
            self.store,
            operation=operation,
            payload=payload,
            idempotency_key=idempotency_key,
            now=T0,
        )


def _running_session(gateway: _TransportFailureGateway) -> AgentSession:
    session = AgentSession(
        gateway,
        task="kinoko34077/execution-coordinator#31",
        role=Role.IMPLEMENTER,
        worker_id="worker-transport-fence",
        conflict_keys=("component:execution-coordinator:agent-adapter",),
    )
    session.claim(idempotency_key="claim-1")
    session.acknowledge(idempotency_key="ack-1")
    return session


class AgentTransportFencingTests(unittest.TestCase):
    def _assert_transport_failure_fences(self, operation: str, invoke) -> None:
        gateway = _TransportFailureGateway(operation)
        session = _running_session(gateway)

        with self.assertRaisesRegex(GitHubApiError, f"{operation} transport failed"):
            invoke(session)

        self.assertIsNone(session.claim_id)
        self.assertIsNone(session.generation)
        with self.assertRaisesRegex(RuntimeError, "fenced"):
            session.renew(idempotency_key="renew-after-fence")

    def test_renew_transport_failure_fences_local_authority(self) -> None:
        self._assert_transport_failure_fences(
            "renew", lambda session: session.renew(idempotency_key="renew-transport")
        )

    def test_progress_transport_failure_fences_local_authority(self) -> None:
        self._assert_transport_failure_fences(
            "progress", lambda session: session.progress(idempotency_key="progress-transport")
        )

    def test_fail_transport_failure_fences_local_authority(self) -> None:
        self._assert_transport_failure_fences(
            "fail",
            lambda session: session.fail(
                reason="test failure", idempotency_key="fail-transport"
            ),
        )

    def test_release_transport_failure_fences_local_authority(self) -> None:
        self._assert_transport_failure_fences(
            "release", lambda session: session.release(idempotency_key="release-transport")
        )


if __name__ == "__main__":
    unittest.main()
