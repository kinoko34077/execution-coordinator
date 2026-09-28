from __future__ import annotations

import unittest

from execution_coordinator.discovery import DurableIssueSource
from execution_coordinator.engine import ClaimConflict, claim
from execution_coordinator.model import CoordinatorState, Role
from execution_coordinator.reconciliation import discover_reconciliation_candidates
from tests.test_reconciliation_discovery import (
    NOW,
    _publication,
    _publication_id,
    _reader,
)


CONTROL = DurableIssueSource("kinoko34077/devflow", 107)


class ReconciliationHardeningTests(unittest.TestCase):
    def test_malformed_role_type_fails_closed_as_discovery_failure(self) -> None:
        task_body = "task"
        publication = _publication(task_number=7, task_body=task_body)
        publication["role"] = ["reviewer"]
        reader = _reader([publication], {7: task_body})

        result = discover_reconciliation_candidates((CONTROL,), reader)

        self.assertEqual(result.candidates, ())
        self.assertEqual(len(result.failures), 1)
        self.assertIn("role", result.failures[0].reason.lower())

    def test_reviewer_requires_explicit_different_reviewer_reason(self) -> None:
        task_body = "task"
        publication = _publication(task_number=7, task_body=task_body)
        publication["reason_codes"] = ["REVIEW_REQUIRED"]
        publication["publication_id"] = _publication_id(
            task_ref=publication["task_ref"],
            task_digest=publication["task_body_sha256"],
            entry_ref=publication["entry_ref"],
            role="reviewer",
            disposition="NEEDS_REVIEWER",
            reason_codes=["REVIEW_REQUIRED"],
            scope=publication["scope"],
            context=publication["context"],
        )
        reader = _reader([publication], {7: task_body})

        result = discover_reconciliation_candidates((CONTROL,), reader)

        self.assertEqual(result.candidates, ())
        self.assertEqual(len(result.failures), 1)
        self.assertIn("different-reviewer", result.failures[0].reason.lower())

    def test_recovery_worker_cannot_also_hold_independent_reviewer_for_same_task(self) -> None:
        recovered = claim(
            CoordinatorState.empty(),
            task="owner/repo#7",
            role=Role.RECOVERY,
            worker_id="worker-a",
            conflict_keys=(),
            now=NOW,
            idempotency_key="recovery-claim",
        )

        with self.assertRaises(ClaimConflict):
            claim(
                recovered.state,
                task="owner/repo#7",
                role=Role.REVIEWER,
                worker_id="worker-a",
                conflict_keys=(),
                now=NOW,
                idempotency_key="reviewer-claim",
            )

    def test_recovery_role_respects_existing_conflict_key_authority(self) -> None:
        implemented = claim(
            CoordinatorState.empty(),
            task="owner/repo#8",
            role=Role.IMPLEMENTER,
            worker_id="worker-a",
            conflict_keys=("component:owner/repo:shared",),
            now=NOW,
            idempotency_key="implementer-claim",
        )

        with self.assertRaises(ClaimConflict):
            claim(
                implemented.state,
                task="owner/repo#7",
                role=Role.RECOVERY,
                worker_id="worker-b",
                conflict_keys=("component:owner/repo:shared",),
                now=NOW,
                idempotency_key="recovery-conflict-claim",
            )


if __name__ == "__main__":
    unittest.main()
