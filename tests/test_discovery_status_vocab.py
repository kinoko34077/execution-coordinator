from __future__ import annotations

import unittest

from execution_coordinator.discovery import DurableIssueSource, discover_claim_candidates

from test_discovery_v1_contract import (
    CONTROL,
    _block,
    _control_body,
    _doc,
    _envelope,
    _Reader,
)


class DurableCandidateStatusVocabularyTests(unittest.TestCase):
    def test_projection_status_is_not_derived_from_control_work_status(self) -> None:
        task_body = "task"
        candidate = _envelope(
            task_number=7,
            task_body=task_body,
            status="READY_FOR_IMPLEMENTATION",
        )
        control = _control_body(block=_block(candidates=[candidate]))
        control = control.replace("`AUDITED`", "`NEEDS_REAUDIT`")
        reader = _Reader(
            {
                ("kinoko34077/devflow", 107): _doc(
                    "kinoko34077/devflow",
                    107,
                    control,
                    title="[REPO] repo",
                ),
                ("owner/repo", 7): _doc("owner/repo", 7, task_body),
            }
        )

        result = discover_claim_candidates((DurableIssueSource("kinoko34077/devflow", 107),), reader)

        self.assertEqual(result.failures, ())
        self.assertEqual(len(result.candidates), 1)

    def test_unknown_projection_status_is_an_explicit_failure(self) -> None:
        task_body = "task"
        candidate = _envelope(
            task_number=7,
            task_body=task_body,
            status="UNKNOWN_STATUS",
        )
        reader = _Reader(
            {
                ("kinoko34077/devflow", 107): _doc(
                    "kinoko34077/devflow",
                    107,
                    _control_body(block=_block(candidates=[candidate])),
                    title="[REPO] repo",
                ),
                ("owner/repo", 7): _doc("owner/repo", 7, task_body),
            }
        )

        result = discover_claim_candidates((DurableIssueSource("kinoko34077/devflow", 107),), reader)

        self.assertEqual(result.candidates, ())
        self.assertIn("status", result.failures[0].reason.lower())


if __name__ == "__main__":
    unittest.main()
