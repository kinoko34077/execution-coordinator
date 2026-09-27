from __future__ import annotations

import unittest
from datetime import datetime, timezone

from execution_coordinator.agent import AgentSession
from execution_coordinator.github_state import GitHubApiError
from execution_coordinator.model import CoordinatorState, MutationResult, Role, WaitReason
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


class _Gateway:
    def __init__(self) -> None:
        self.store = _Store()
        self.calls: list[tuple[str, dict[str, object], str]] = []

    def mutate(
        self,
        *,
        operation: str,
        payload: dict[str, object],
        idempotency_key: str,
    ) -> MutationResult:
        self.calls.append((operation, dict(payload), idempotency_key))
        return apply_mutation(
            self.store,
            operation=operation,
            payload=payload,
            idempotency_key=idempotency_key,
            now=T0,
        )


def _running_session(gateway: _Gateway) -> AgentSession:
    session = AgentSession(
        gateway,
        task="kinoko34077/execution-coordinator#18",
        role=Role.IMPLEMENTER,
        worker_id="worker-wait-resume-transport",
        conflict_keys=("component:execution-coordinator:agent-adapter",),
    )
    session.claim(idempotency_key="claim-1")
    session.acknowledge(idempotency_key="ack-1")
    return session


class AgentWaitResumeTransportTests(unittest.TestCase):
    def test_wait_transport_failure_fences_local_authority(self) -> None:
        class WaitTransportFailureGateway(_Gateway):
            def mutate(self, *, operation, payload, idempotency_key):
                if operation == "wait":
                    self.calls.append((operation, dict(payload), idempotency_key))
                    raise GitHubApiError("wait transport failed")
                return super().mutate(
                    operation=operation,
                    payload=payload,
                    idempotency_key=idempotency_key,
                )

        gateway = WaitTransportFailureGateway()
        session = _running_session(gateway)

        with self.assertRaisesRegex(GitHubApiError, "wait transport failed"):
            session.wait(
                reason=WaitReason.CI,
                evidence_ref="run:transport-wait",
                idempotency_key="wait-transport",
            )

        self.assertIsNone(session.claim_id)
        self.assertIsNone(session.generation)
        self.assertEqual(
            ["claim", "acknowledge", "wait"],
            [operation for operation, _payload, _key in gateway.calls],
        )
        with self.assertRaisesRegex(RuntimeError, "fenced"):
            session.renew(idempotency_key="renew-after-wait-fence")

    def test_resume_transport_failure_fences_local_authority(self) -> None:
        class ResumeTransportFailureGateway(_Gateway):
            def mutate(self, *, operation, payload, idempotency_key):
                if operation == "resume":
                    self.calls.append((operation, dict(payload), idempotency_key))
                    raise GitHubApiError("resume transport failed")
                return super().mutate(
                    operation=operation,
                    payload=payload,
                    idempotency_key=idempotency_key,
                )

        gateway = ResumeTransportFailureGateway()
        session = _running_session(gateway)
        session.wait(
            reason=WaitReason.EXTERNAL,
            evidence_ref="external:transport-resume",
            idempotency_key="wait-1",
        )

        with self.assertRaisesRegex(GitHubApiError, "resume transport failed"):
            session.resume(idempotency_key="resume-transport")

        self.assertIsNone(session.claim_id)
        self.assertIsNone(session.generation)
        self.assertEqual(
            ["claim", "acknowledge", "wait", "resume"],
            [operation for operation, _payload, _key in gateway.calls],
        )
        with self.assertRaisesRegex(RuntimeError, "fenced"):
            session.release(idempotency_key="release-after-resume-fence")


if __name__ == "__main__":
    unittest.main()
