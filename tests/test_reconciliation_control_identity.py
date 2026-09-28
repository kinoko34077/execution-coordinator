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


class ReconciliationControlIdentityTests(unittest.TestCase):
    def test_canonical_repository_control_title_is_accepted(self) -> None:
        control = replace(
            _control_document([_publication("reviewer")]),
            title="[REPO] repo",
        )
        reader = _Reader(control, _task_document())

        result = discover_reconciliation_claim_candidates((CONTROL_SOURCE,), reader)

        self.assertEqual(result.failures, ())
        self.assertEqual(len(result.candidates), 1)


if __name__ == "__main__":
    unittest.main()
