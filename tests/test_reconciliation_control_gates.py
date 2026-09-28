from __future__ import annotations

from dataclasses import replace
import unittest

from execution_coordinator.reconciliation import discover_reconciliation_claim_candidates
from tests.test_reconciliation_adoption import (
    CONTROL_SOURCE,
    _Reader,
    _control_document,
    _publication,
    _task_document,
)


def _canonical_control():
    return _control_document([_publication("reviewer")])


class ReconciliationControlGateTests(unittest.TestCase):
    def test_full_control_repository_identity_must_match_projection(self) -> None:
        control = _canonical_control()
        control = replace(
            control,
            body=control.body.replace("`owner/repo`", "`other/repo`", 1),
        )
        reader = _Reader(control, _task_document())

        result = discover_reconciliation_claim_candidates((CONTROL_SOURCE,), reader)

        self.assertEqual(result.candidates, ())
        self.assertEqual(len(result.failures), 1)
        self.assertIn("Repository", result.failures[0].reason)

    def test_inactive_control_repository_state_vetoes_publication(self) -> None:
        control = _canonical_control()
        control = replace(control, body=control.body.replace("`ACTIVE`", "`PARKED`", 1))
        reader = _Reader(control, _task_document())

        result = discover_reconciliation_claim_candidates((CONTROL_SOURCE,), reader)

        self.assertEqual(result.candidates, ())
        self.assertEqual(len(result.failures), 1)
        self.assertIn("State", result.failures[0].reason)

    def test_current_control_human_gate_vetoes_publication(self) -> None:
        control = _canonical_control()
        control = replace(
            control,
            body=control.body.replace("[IMPLEMENT]", "[USER_DECISION]", 1),
        )
        reader = _Reader(control, _task_document())

        result = discover_reconciliation_claim_candidates((CONTROL_SOURCE,), reader)

        self.assertEqual(result.candidates, ())
        self.assertEqual(len(result.failures), 1)
        self.assertIn("Gate", result.failures[0].reason)


if __name__ == "__main__":
    unittest.main()
