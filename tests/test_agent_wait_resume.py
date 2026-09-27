from __future__ import annotations

import unittest
from datetime import datetime, timezone

from execution_coordinator.agent import AdapterProtocolError, AgentSession
from execution_coordinator.engine import CoordinationError
from execution_coordinator.github_state import GitHubApiError
from execution_coordinator.model import (
    CoordinatorState,
    ExecutionState,
    MutationResult,
    Role,
    WaitReason,
)
from execution_coordinator.mutate import apply_mutation
from execution_coordinator.snapshot import render_issue_body


UTC = timezone.utc
T0 = datetime(2026, 9, 27, 3, 10, tzinfo=UTC)


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


def _only_claim(result: MutationResult):
    return next(iter(result.state.claims.values()))


class AgentWaitResumeTests(unittest.TestCase):
    def _session(self, gateway: _Gateway) -> AgentSession:
        return AgentSession(
            gateway,
            task="kinoko34077/execution-coordinator#18",
            role=Role.IMPLEMENTER,
            worker_id="worker-wait-resume",
            conflict_keys=("component:execution-coordinator:agent-adapter",),
        )

    def _running_session(self, gateway: _Gateway) -> AgentSession:
        session = self._session(gateway)
        session.claim(idempotency_key="claim-1")
        session.acknowledge(idempotency_key="ack-1")
        return session

    def test_wait_and_resume_preserve_authority_and_runtime_state(self) -> None:
        gateway = _Gateway()
        session = self._running_session(gateway)
        authority = (session.claim_id, session.generation)

        waiting = session.wait(
            reason=WaitReason.CI,
            evidence_ref="run:36290000000",
            idempotency_key="wait-1",
        )
        waiting_claim = _only_claim(waiting)
        self.assertEqual(authority, (waiting.claim_id, waiting.generation))
        self.assertEqual(ExecutionState.WAITING, waiting_claim.state)
        self.assertEqual(WaitReason.CI, waiting_claim.wait_reason)
        self.assertEqual("run:36290000000", waiting_claim.evidence_ref)
        self.assertEqual(
            {
                "claim_id": authority[0],
                "generation": authority[1],
                "reason": "CI",
                "evidence_ref": "run:36290000000",
            },
            gateway.calls[-1][1],
        )
        self.assertEqual("wait-1", gateway.calls[-1][2])

        resumed = session.resume(idempotency_key="resume-1")
        resumed_claim = _only_claim(resumed)
        self.assertEqual(authority, (resumed.claim_id, resumed.generation))
        self.assertEqual(ExecutionState.RUNNING, resumed_claim.state)
        self.assertIsNone(resumed_claim.wait_reason)
        self.assertIsNone(resumed_claim.evidence_ref)
        self.assertEqual("resume-1", gateway.calls[-1][2])

    def test_wait_without_active_claim_does_not_reach_gateway(self) -> None:
        gateway = _Gateway()
        session = self._session(gateway)

        with self.assertRaisesRegex(RuntimeError, "no active claim"):
            session.wait(
                reason=WaitReason.CI,
                evidence_ref="run:1",
                idempotency_key="wait-no-claim",
            )

        self.assertEqual([], gateway.calls)

    def test_empty_wait_evidence_is_rejected_before_gateway_mutation(self) -> None:
        gateway = _Gateway()
        session = self._running_session(gateway)
        before = len(gateway.calls)

        with self.assertRaisesRegex(ValueError, "evidence_ref"):
            session.wait(
                reason=WaitReason.REVIEW,
                evidence_ref="   ",
                idempotency_key="wait-empty",
            )

        self.assertEqual(before, len(gateway.calls))

    def test_wait_transport_failure_fences_local_authority(self) -> None:
        class TransportFailGateway(_Gateway):
            def mutate(self, *, operation, payload, idempotency_key):
                if operation == "wait":
                    self.calls.append((operation, dict(payload), idempotency_key))
                    raise GitHubApiError("wait transport failed")
                return super().mutate(
                    operation=operation,
                    payload=payload,
                    idempotency_key=idempotency_key,
                )

        gateway = TransportFailGateway()
        session = self._running_session(gateway)

        with self.assertRaisesRegex(GitHubApiError, "wait transport failed"):
            session.wait(
                reason=WaitReason.CI,
                evidence_ref="run:2",
                idempotency_key="wait-transport",
            )

        self.assertIsNone(session.claim_id)
        self.assertIsNone(session.generation)
        with self.assertRaisesRegex(RuntimeError, "fenced"):
            session.resume(idempotency_key="resume-after-fence")

    def test_malformed_wait_response_fences_local_authority(self) -> None:
        class MalformedWaitGateway(_Gateway):
            def mutate(self, *, operation, payload, idempotency_key):
                if operation == "wait":
                    self.calls.append((operation, dict(payload), idempotency_key))
                    return MutationResult(state=CoordinatorState.empty())
                return super().mutate(
                    operation=operation,
                    payload=payload,
                    idempotency_key=idempotency_key,
                )

        gateway = MalformedWaitGateway()
        session = self._running_session(gateway)

        with self.assertRaises(AdapterProtocolError):
            session.wait(
                reason=WaitReason.DEPENDENCY,
                evidence_ref="issue:42",
                idempotency_key="wait-malformed",
            )

        self.assertIsNone(session.claim_id)
        self.assertIsNone(session.generation)

    def test_changed_authority_resume_fences_local_session(self) -> None:
        class ChangedResumeGateway(_Gateway):
            def mutate(self, *, operation, payload, idempotency_key):
                result = super().mutate(
                    operation=operation,
                    payload=payload,
                    idempotency_key=idempotency_key,
                )
                if operation == "resume":
                    return MutationResult(
                        state=result.state,
                        claim_id="clm_other",
                        generation=99,
                    )
                return result

        gateway = ChangedResumeGateway()
        session = self._running_session(gateway)
        session.wait(
            reason=WaitReason.EXTERNAL,
            evidence_ref="external:1",
            idempotency_key="wait-1",
        )

        with self.assertRaises(AdapterProtocolError):
            session.resume(idempotency_key="resume-changed")

        self.assertIsNone(session.claim_id)
        self.assertIsNone(session.generation)

    def test_stale_resume_failure_fences_local_session(self) -> None:
        class StaleResumeGateway(_Gateway):
            def mutate(self, *, operation, payload, idempotency_key):
                if operation == "resume":
                    self.calls.append((operation, dict(payload), idempotency_key))
                    raise CoordinationError("stale generation")
                return super().mutate(
                    operation=operation,
                    payload=payload,
                    idempotency_key=idempotency_key,
                )

        gateway = StaleResumeGateway()
        session = self._running_session(gateway)
        session.wait(
            reason=WaitReason.PROVIDER,
            evidence_ref="provider:waiting",
            idempotency_key="wait-1",
        )

        with self.assertRaisesRegex(CoordinationError, "stale generation"):
            session.resume(idempotency_key="resume-stale")

        self.assertIsNone(session.claim_id)
        self.assertIsNone(session.generation)


if __name__ == "__main__":
    unittest.main()
