from __future__ import annotations

import unittest

from execution_coordinator.agent import AgentSession
from execution_coordinator.github_state import GitHubApiError
from execution_coordinator.model import CoordinatorState, MutationResult, Role


class AgentAcknowledgeTransportTests(unittest.TestCase):
    def test_transport_failure_during_acknowledge_fences_session(self) -> None:
        class TransportFailureGateway:
            def mutate(
                self,
                *,
                operation: str,
                payload: dict[str, object],
                idempotency_key: str,
            ) -> MutationResult:
                del payload, idempotency_key
                if operation == "claim":
                    return MutationResult(
                        state=CoordinatorState.empty(),
                        claim_id="clm_transport",
                        generation=1,
                    )
                if operation == "acknowledge":
                    raise GitHubApiError("transport failed")
                raise AssertionError(f"unexpected operation: {operation}")

        session = AgentSession(
            TransportFailureGateway(),
            task="kinoko34077/example#9",
            role=Role.IMPLEMENTER,
            worker_id="worker-a",
        )
        session.claim(idempotency_key="claim-1")

        with self.assertRaisesRegex(GitHubApiError, "transport failed"):
            session.acknowledge(idempotency_key="ack-1")

        self.assertIsNone(session.claim_id)
        with self.assertRaisesRegex(RuntimeError, "fenced"):
            session.release(idempotency_key="release-after-fence")


if __name__ == "__main__":
    unittest.main()
