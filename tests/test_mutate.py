from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from execution_coordinator.github_state import GitHubApiError
from execution_coordinator.model import CoordinatorState, ExecutionState
from execution_coordinator.mutate import apply_mutation
from execution_coordinator.snapshot import parse_issue_body, render_issue_body


UTC = timezone.utc
T0 = datetime(2026, 9, 27, 2, 0, tzinfo=UTC)


class _FakeStore:
    def __init__(self, *, fail_save: bool = False) -> None:
        self.body = render_issue_body(
            "# Execution Coordination State\n\nRuntime snapshot.\n",
            CoordinatorState.empty(),
        )
        self.fail_save = fail_save
        self.comments: list[str] = []
        self.save_count = 0

    def load_body(self) -> str:
        return self.body

    def save_body(self, body: str) -> None:
        if self.fail_save:
            raise GitHubApiError("synthetic save failure")
        self.body = body
        self.save_count += 1

    def add_comment(self, body: str) -> None:
        self.comments.append(body)


class MutationTests(unittest.TestCase):
    def test_claim_saves_snapshot_and_emits_one_lifecycle_comment(self) -> None:
        store = _FakeStore()
        result = apply_mutation(
            store,
            operation="claim",
            payload={
                "task": "kinoko34077/example#1",
                "role": "implementer",
                "worker_id": "worker-a",
                "conflict_keys": ["component:example:parser"],
            },
            idempotency_key="claim-1",
            now=T0,
        )
        state = parse_issue_body(store.body)
        self.assertIn(result.claim_id, state.claims)
        self.assertEqual(1, store.save_count)
        self.assertEqual(1, len(store.comments))
        self.assertIn("CLAIMED", store.comments[0])
        self.assertNotIn("worker-a", store.comments[0])

    def test_renew_updates_snapshot_without_lifecycle_comment(self) -> None:
        store = _FakeStore()
        claimed = apply_mutation(
            store,
            operation="claim",
            payload={
                "task": "kinoko34077/example#1",
                "role": "implementer",
                "worker_id": "worker-a",
                "conflict_keys": [],
            },
            idempotency_key="claim-1",
            now=T0,
        )
        store.comments.clear()
        apply_mutation(
            store,
            operation="renew",
            payload={"claim_id": claimed.claim_id, "generation": claimed.generation},
            idempotency_key="renew-1",
            now=T0 + timedelta(minutes=5),
        )
        self.assertEqual([], store.comments)
        current = parse_issue_body(store.body).claims[claimed.claim_id]
        self.assertEqual(T0 + timedelta(minutes=20), current.lease_until)

    def test_wait_updates_snapshot_without_lifecycle_comment(self) -> None:
        store = _FakeStore()
        claimed = apply_mutation(
            store,
            operation="claim",
            payload={
                "task": "kinoko34077/example#1",
                "role": "implementer",
                "worker_id": "worker-a",
                "conflict_keys": [],
            },
            idempotency_key="claim-1",
            now=T0,
        )
        store.comments.clear()
        apply_mutation(
            store,
            operation="wait",
            payload={
                "claim_id": claimed.claim_id,
                "generation": claimed.generation,
                "reason": "CI",
                "evidence_ref": "example#2/checks",
            },
            idempotency_key="wait-1",
            now=T0 + timedelta(minutes=1),
        )
        self.assertEqual([], store.comments)

    def test_resume_updates_waiting_claim_without_lifecycle_comment(self) -> None:
        store = _FakeStore()
        claimed = apply_mutation(
            store,
            operation="claim",
            payload={
                "task": "kinoko34077/example#1",
                "role": "implementer",
                "worker_id": "worker-a",
                "conflict_keys": [],
            },
            idempotency_key="claim-1",
            now=T0,
        )
        apply_mutation(
            store,
            operation="wait",
            payload={
                "claim_id": claimed.claim_id,
                "generation": claimed.generation,
                "reason": "CI",
                "evidence_ref": "example#2/checks",
            },
            idempotency_key="wait-1",
            now=T0 + timedelta(minutes=1),
        )
        store.comments.clear()
        apply_mutation(
            store,
            operation="resume",
            payload={"claim_id": claimed.claim_id, "generation": claimed.generation},
            idempotency_key="resume-1",
            now=T0 + timedelta(minutes=2),
        )
        self.assertEqual([], store.comments)
        current = parse_issue_body(store.body).claims[claimed.claim_id]
        self.assertEqual(ExecutionState.RUNNING, current.state)
        self.assertIsNone(current.wait_reason)
        self.assertIsNone(current.evidence_ref)

    def test_release_emits_durable_comment(self) -> None:
        store = _FakeStore()
        claimed = apply_mutation(
            store,
            operation="claim",
            payload={
                "task": "kinoko34077/example#1",
                "role": "implementer",
                "worker_id": "worker-a",
                "conflict_keys": [],
            },
            idempotency_key="claim-1",
            now=T0,
        )
        store.comments.clear()
        apply_mutation(
            store,
            operation="release",
            payload={"claim_id": claimed.claim_id, "generation": claimed.generation},
            idempotency_key="release-1",
            now=T0 + timedelta(minutes=1),
        )
        self.assertEqual(1, len(store.comments))
        self.assertIn("RELEASED", store.comments[0])

    def test_failed_snapshot_save_does_not_emit_success_comment(self) -> None:
        store = _FakeStore(fail_save=True)
        with self.assertRaises(GitHubApiError):
            apply_mutation(
                store,
                operation="claim",
                payload={
                    "task": "kinoko34077/example#1",
                    "role": "implementer",
                    "worker_id": "worker-a",
                    "conflict_keys": [],
                },
                idempotency_key="claim-1",
                now=T0,
            )
        self.assertEqual([], store.comments)

    def test_unknown_operation_fails_without_writing(self) -> None:
        store = _FakeStore()
        with self.assertRaises(ValueError):
            apply_mutation(
                store,
                operation="erase-everything",
                payload={},
                idempotency_key="bad-op",
                now=T0,
            )
        self.assertEqual(0, store.save_count)
        self.assertEqual([], store.comments)


if __name__ == "__main__":
    unittest.main()
