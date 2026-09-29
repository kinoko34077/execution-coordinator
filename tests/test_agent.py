from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

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
from execution_coordinator.snapshot import parse_issue_body, render_issue_body
from execution_coordinator.agent import AdapterProtocolError, AgentSession


UTC = timezone.utc
T0 = datetime(2026, 9, 27, 2, 0, tzinfo=UTC)


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
    def __init__(self, *, now: datetime = T0) -> None:
        self.store = _Store()
        self.now = now
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
            now=self.now,
        )


class AgentSessionTests(unittest.TestCase):
    def _session(self, gateway: _Gateway, *, worker_id: str = "worker-a") -> AgentSession:
        return AgentSession(
            gateway,
            task="kinoko34077/example#1",
            role=Role.IMPLEMENTER,
            worker_id=worker_id,
            conflict_keys=("component:example:parser",),
        )

    def test_claim_rejection_never_enters_work_callback(self) -> None:
        gateway = _Gateway()
        owner = self._session(gateway)
        owner.claim(idempotency_key="claim-owner")
        contender = self._session(gateway, worker_id="worker-b")
        entered: list[bool] = []

        with self.assertRaises(CoordinationError):
            contender.run(
                lambda _session: entered.append(True),
                claim_idempotency_key="claim-contender",
                acknowledge_idempotency_key="ack-contender",
                release_idempotency_key="release-contender",
            )

        self.assertEqual([], entered)
        self.assertIsNone(contender.claim_id)

    def test_run_claims_before_callback_and_releases_after_callback(self) -> None:
        gateway = _Gateway()
        session = self._session(gateway)
        observed: list[tuple[str | None, int | None]] = []

        result = session.run(
            lambda active: observed.append((active.claim_id, active.generation)) or "done",
            claim_idempotency_key="claim-1",
            acknowledge_idempotency_key="ack-1",
            release_idempotency_key="release-1",
        )

        self.assertEqual("done", result)
        self.assertEqual(1, len(observed))
        self.assertIsNotNone(observed[0][0])
        self.assertEqual(1, observed[0][1])
        self.assertIsNone(session.claim_id)
        self.assertEqual(
            ["claim", "acknowledge", "release"],
            [call[0] for call in gateway.calls],
        )
        self.assertEqual(
            {
                "task": "kinoko34077/example#1",
                "role": "implementer",
                "worker_id": "worker-a",
                "conflict_keys": ["component:example:parser"],
                "base_sha": None,
                "branch": None,
            },
            gateway.calls[0][1],
        )

    def test_renew_and_release_forward_current_generation_and_keys(self) -> None:
        gateway = _Gateway()
        session = self._session(gateway)
        session.claim(idempotency_key="claim-1")

        session.renew(idempotency_key="renew-1")
        session.release(idempotency_key="release-1")

        self.assertEqual(
            [
                ("claim", "claim-1"),
                ("renew", "renew-1"),
                ("release", "release-1"),
            ],
            [(operation, key) for operation, _payload, key in gateway.calls],
        )
        for operation, payload, _key in gateway.calls[1:]:
            self.assertEqual("clm_", payload["claim_id"][:4])
            self.assertEqual(1, payload["generation"])
        self.assertIsNone(session.claim_id)

    def test_release_is_idempotent_after_success(self) -> None:
        gateway = _Gateway()
        session = self._session(gateway)
        session.claim(idempotency_key="claim-1")

        first = session.release(idempotency_key="release-1")
        second = session.release(idempotency_key="release-1")

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(["claim", "release"], [call[0] for call in gateway.calls])

    def test_work_exception_still_releases_claim(self) -> None:
        gateway = _Gateway()
        session = self._session(gateway)

        def fail(_session: AgentSession) -> None:
            raise ValueError("work failed")

        with self.assertRaisesRegex(ValueError, "work failed"):
            session.run(
                fail,
                claim_idempotency_key="claim-1",
                acknowledge_idempotency_key="ack-1",
                release_idempotency_key="release-1",
            )

        self.assertIsNone(session.claim_id)
        self.assertEqual(
            ["claim", "acknowledge", "release"],
            [call[0] for call in gateway.calls],
        )

    def test_malformed_claim_response_fences_session(self) -> None:
        class MalformedGateway:
            def mutate(self, **_kwargs: object) -> MutationResult:
                return MutationResult(state=CoordinatorState.empty())

        session = AgentSession(
            MalformedGateway(),
            task="kinoko34077/example#1",
            role=Role.IMPLEMENTER,
            worker_id="worker-a",
        )

        with self.assertRaises(AdapterProtocolError):
            session.claim(idempotency_key="claim-malformed")
        with self.assertRaisesRegex(RuntimeError, "fenced"):
            session.claim(idempotency_key="claim-retry")

    def test_stale_generation_fences_session_for_future_mutations(self) -> None:
        gateway = _Gateway()
        session = self._session(gateway)
        session.claim(idempotency_key="claim-1")
        gateway.now = T0 + timedelta(minutes=16)
        apply_mutation(
            gateway.store,
            operation="expire",
            payload={},
            idempotency_key="expire-1",
            now=gateway.now,
        )
        replacement = self._session(gateway, worker_id="worker-b")
        replacement.claim(idempotency_key="claim-replacement")

        with self.assertRaises(CoordinationError):
            session.renew(idempotency_key="renew-stale")
        self.assertIsNone(session.claim_id)
        with self.assertRaises(RuntimeError):
            session.release(idempotency_key="release-after-fence")

    def test_changed_authority_response_fences_session(self) -> None:
        class ChangedAuthorityGateway(_Gateway):
            def mutate(
                self,
                *,
                operation: str,
                payload: dict[str, object],
                idempotency_key: str,
            ) -> MutationResult:
                result = super().mutate(
                    operation=operation,
                    payload=payload,
                    idempotency_key=idempotency_key,
                )
                if operation == "renew":
                    return MutationResult(
                        claim_id="clm_other",
                        generation=99,
                        state=result.state,
                    )
                return result

        gateway = ChangedAuthorityGateway()
        session = self._session(gateway)
        session.claim(idempotency_key="claim-1")

        with self.assertRaises(AdapterProtocolError):
            session.renew(idempotency_key="renew-changed-authority")
        self.assertIsNone(session.claim_id)
        with self.assertRaisesRegex(RuntimeError, "fenced"):
            session.release(idempotency_key="release-after-fence")


    def test_acknowledge_forwards_current_authority_and_idempotency_key(self) -> None:
        gateway = _Gateway()
        session = self._session(gateway)
        claim_result = session.claim(idempotency_key="claim-1")

        result = session.acknowledge(idempotency_key="ack-1")

        self.assertEqual(session.claim_id, result.claim_id)
        self.assertEqual(session.generation, result.generation)
        self.assertEqual(
            ("acknowledge", "ack-1"),
            (gateway.calls[1][0], gateway.calls[1][2]),
        )
        self.assertEqual(
            {
                "claim_id": claim_result.claim_id,
                "generation": claim_result.generation,
            },
            gateway.calls[1][1],
        )

    def test_run_acknowledges_before_callback_and_releases_after_callback(self) -> None:
        gateway = _Gateway()
        session = self._session(gateway)
        observed: list[tuple[str | None, int | None]] = []

        result = session.run(
            lambda active: observed.append((active.claim_id, active.generation)) or "done",
            claim_idempotency_key="claim-1",
            acknowledge_idempotency_key="ack-1",
            release_idempotency_key="release-1",
        )

        self.assertEqual("done", result)
        self.assertEqual(1, len(observed))
        self.assertEqual(
            ["claim", "acknowledge", "release"],
            [call[0] for call in gateway.calls],
        )
        self.assertEqual("ack-1", gateway.calls[1][2])

    def test_acknowledge_failure_never_enters_callback_and_fences_session(self) -> None:
        class FailingAcknowledgeGateway(_Gateway):
            def mutate(
                self,
                *,
                operation: str,
                payload: dict[str, object],
                idempotency_key: str,
            ) -> MutationResult:
                if operation == "acknowledge":
                    self.calls.append((operation, dict(payload), idempotency_key))
                    raise CoordinationError("acknowledge rejected")
                return super().mutate(
                    operation=operation,
                    payload=payload,
                    idempotency_key=idempotency_key,
                )

        gateway = FailingAcknowledgeGateway()
        session = self._session(gateway)
        entered: list[bool] = []

        with self.assertRaises(CoordinationError):
            session.run(
                lambda _session: entered.append(True),
                claim_idempotency_key="claim-1",
                acknowledge_idempotency_key="ack-1",
                release_idempotency_key="release-1",
            )

        self.assertEqual([], entered)
        self.assertIsNone(session.claim_id)
        self.assertEqual(["claim", "acknowledge"], [call[0] for call in gateway.calls])
        with self.assertRaisesRegex(RuntimeError, "fenced"):
            session.claim(idempotency_key="claim-retry")

    def test_lifecycle_methods_forward_authority_and_transition_waiting(self) -> None:
        gateway = _Gateway()
        session = self._session(gateway)
        session.claim(idempotency_key="claim-1")

        session.acknowledge(idempotency_key="ack-1")
        session.progress(idempotency_key="progress-1")
        session.wait(
            reason=WaitReason.CI,
            evidence_ref="run:123",
            idempotency_key="wait-1",
        )
        waiting_state = parse_issue_body(gateway.store.body)
        claim = next(iter(waiting_state.claims.values()))
        self.assertEqual(ExecutionState.WAITING, claim.state)
        session.resume(idempotency_key="resume-1")
        session.fail(reason="verification failed", idempotency_key="fail-1")

        self.assertEqual(
            ["claim", "acknowledge", "progress", "wait", "resume", "fail"],
            [call[0] for call in gateway.calls],
        )
        for _operation, payload, _key in gateway.calls[1:]:
            self.assertEqual("clm_", payload["claim_id"][:4])
            self.assertEqual(1, payload["generation"])
        self.assertEqual("CI", gateway.calls[3][1]["reason"])
        self.assertEqual("run:123", gateway.calls[3][1]["evidence_ref"])
        self.assertEqual("verification failed", gateway.calls[5][1]["reason"])
        self.assertIsNone(session.claim_id)

    def test_resume_requires_waiting_without_gateway_call(self) -> None:
        gateway = _Gateway()
        session = self._session(gateway)
        session.claim(idempotency_key="claim-1")

        with self.assertRaisesRegex(RuntimeError, "WAITING"):
            session.resume(idempotency_key="resume-invalid")

        self.assertEqual(["claim"], [call[0] for call in gateway.calls])

    def test_work_exception_preserves_original_when_release_fails(self) -> None:
        class ReleaseFailureGateway(_Gateway):
            def mutate(
                self,
                *,
                operation: str,
                payload: dict[str, object],
                idempotency_key: str,
            ) -> MutationResult:
                if operation == "release":
                    raise CoordinationError("release unavailable")
                return super().mutate(
                    operation=operation,
                    payload=payload,
                    idempotency_key=idempotency_key,
                )

        gateway = ReleaseFailureGateway()
        session = self._session(gateway)

        def fail(_session: AgentSession) -> None:
            raise ValueError("work failed")

        with self.assertRaisesRegex(ValueError, "work failed") as raised:
            session.run(
                fail,
                claim_idempotency_key="claim-1",
                acknowledge_idempotency_key="ack-1",
                release_idempotency_key="release-1",
            )

        self.assertTrue(any("release" in note for note in raised.exception.__notes__))

    def test_terminal_response_retaining_claim_fences_session(self) -> None:
        class RetainedClaimGateway(_Gateway):
            def mutate(
                self,
                *,
                operation: str,
                payload: dict[str, object],
                idempotency_key: str,
            ) -> MutationResult:
                before = parse_issue_body(self.store.body)
                result = super().mutate(
                    operation=operation,
                    payload=payload,
                    idempotency_key=idempotency_key,
                )
                if operation == "release":
                    return MutationResult(
                        state=before,
                        claim_id=result.claim_id,
                        generation=result.generation,
                    )
                return result

        gateway = RetainedClaimGateway()
        session = self._session(gateway)
        session.claim(idempotency_key="claim-1")

        with self.assertRaises(AdapterProtocolError):
            session.release(idempotency_key="release-1")

        self.assertIsNone(session.claim_id)
        with self.assertRaisesRegex(RuntimeError, "fenced"):
            session.progress(idempotency_key="progress-after-fence")

    def test_from_current_claim_reattaches_exact_authority(self) -> None:
        gateway = _Gateway()
        original = self._session(gateway)
        original.claim(idempotency_key="claim-reattach")
        state = parse_issue_body(gateway.store.body)
        claim = next(iter(state.claims.values()))

        attached = AgentSession.from_current_claim(
            gateway,
            claim,
            expected_task="kinoko34077/example#1",
            expected_role=Role.IMPLEMENTER,
            expected_worker_id="worker-a",
        )
        attached.acknowledge(idempotency_key="ack-reattach")
        self.assertEqual(claim.claim_id, attached.claim_id)
        self.assertEqual(claim.generation, attached.generation)

        with self.assertRaisesRegex(ValueError, "worker"):
            AgentSession.from_current_claim(
                gateway,
                claim,
                expected_task="kinoko34077/example#1",
                expected_role=Role.IMPLEMENTER,
                expected_worker_id="worker-b",
            )


if __name__ == "__main__":
    unittest.main()
