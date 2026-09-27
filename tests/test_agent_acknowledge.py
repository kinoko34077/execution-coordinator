from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from execution_coordinator.agent import AdapterProtocolError, AgentSession
from execution_coordinator.engine import CoordinationError
from execution_coordinator.model import CoordinatorState, MutationResult, Role
from execution_coordinator.mutate import apply_mutation
from execution_coordinator.snapshot import render_issue_body


UTC = timezone.utc
T0 = datetime(2026, 9, 27, 2, 30, tzinfo=UTC)


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
        self.now = T0
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


class AgentAcknowledgeTests(unittest.TestCase):
    def _session(self, gateway: _Gateway) -> AgentSession:
        return AgentSession(
            gateway,
            task="kinoko34077/example#9",
            role=Role.IMPLEMENTER,
            worker_id="worker-a",
            conflict_keys=("component:example:parser",),
        )

    def test_acknowledge_forwards_current_authority_and_idempotency_key(self) -> None:
        gateway = _Gateway()
        session = self._session(gateway)
        session.claim(idempotency_key="claim-1")

        session.acknowledge(idempotency_key="ack-1")

        self.assertEqual(["claim", "acknowledge"], [call[0] for call in gateway.calls])
        operation, payload, key = gateway.calls[-1]
        self.assertEqual("acknowledge", operation)
        self.assertEqual("ack-1", key)
        self.assertEqual(session.claim_id, payload["claim_id"])
        self.assertEqual(session.generation, payload["generation"])

    def test_run_orders_claim_acknowledge_work_release(self) -> None:
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

    def test_acknowledge_failure_never_enters_work_callback(self) -> None:
        class RejectAcknowledgeGateway(_Gateway):
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

        gateway = RejectAcknowledgeGateway()
        session = self._session(gateway)
        entered: list[bool] = []

        with self.assertRaisesRegex(CoordinationError, "acknowledge rejected"):
            session.run(
                lambda _active: entered.append(True),
                claim_idempotency_key="claim-1",
                acknowledge_idempotency_key="ack-1",
                release_idempotency_key="release-1",
            )

        self.assertEqual([], entered)
        self.assertIsNone(session.claim_id)

    def test_stale_generation_acknowledge_fences_session(self) -> None:
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

        with self.assertRaises(CoordinationError):
            session.acknowledge(idempotency_key="ack-stale")

        self.assertIsNone(session.claim_id)
        with self.assertRaisesRegex(RuntimeError, "fenced"):
            session.release(idempotency_key="release-after-fence")

    def test_malformed_acknowledge_response_fences_session(self) -> None:
        class MalformedAcknowledgeGateway(_Gateway):
            def mutate(
                self,
                *,
                operation: str,
                payload: dict[str, object],
                idempotency_key: str,
            ) -> MutationResult:
                if operation == "acknowledge":
                    self.calls.append((operation, dict(payload), idempotency_key))
                    return MutationResult(state=CoordinatorState.empty())
                return super().mutate(
                    operation=operation,
                    payload=payload,
                    idempotency_key=idempotency_key,
                )

        gateway = MalformedAcknowledgeGateway()
        session = self._session(gateway)
        session.claim(idempotency_key="claim-1")

        with self.assertRaises(AdapterProtocolError):
            session.acknowledge(idempotency_key="ack-malformed")

        self.assertIsNone(session.claim_id)
        with self.assertRaisesRegex(RuntimeError, "fenced"):
            session.release(idempotency_key="release-after-fence")

    def test_changed_authority_acknowledge_response_fences_session(self) -> None:
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
                if operation == "acknowledge":
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
            session.acknowledge(idempotency_key="ack-changed")

        self.assertIsNone(session.claim_id)
        with self.assertRaisesRegex(RuntimeError, "fenced"):
            session.release(idempotency_key="release-after-fence")


if __name__ == "__main__":
    unittest.main()
