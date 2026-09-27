from __future__ import annotations

import unittest

from execution_coordinator.discovery import (
    DurableIssueSource,
    IssueDocument,
    discover_claim_candidates,
)
from execution_coordinator.model import Role


class _Reader:
    def read_issue(self, source: DurableIssueSource) -> IssueDocument:
        return IssueDocument(
            repository=source.repository,
            number=source.issue_number,
            state="open",
            body=(
                "## Type / status\n\n"
                "- Type: `FEATURE`\n"
                "- Work Status: `READY_SOON`\n\n"
                "## Objective\n\nBounded task.\n\n"
                "## Scope\n\nBounded scope.\n\n"
                "## Acceptance criteria\n\n- [ ] Verified.\n\n"
                "## Next action\n\n`[IMPLEMENT] proceed`"
            ),
            html_url=f"https://github.com/{source.repository}/issues/{source.issue_number}",
        )


class DiscoveryStatusVocabularyTests(unittest.TestCase):
    def test_unknown_uppercase_work_status_is_an_explicit_failure(self) -> None:
        source = DurableIssueSource(
            "owner/repo",
            1,
            Role.IMPLEMENTER,
            ("READY_FOR_IMPLEMENTATION",),
        )

        result = discover_claim_candidates((source,), _Reader())

        self.assertEqual(result.candidates, ())
        self.assertEqual(len(result.failures), 1)
        self.assertIn("unsupported Work Status", result.failures[0].reason)

    def test_source_eligible_states_must_use_protocol_work_status_vocabulary(self) -> None:
        with self.assertRaisesRegex(ValueError, "eligible_work_states"):
            DurableIssueSource(
                "owner/repo",
                1,
                Role.IMPLEMENTER,
                ("READY_SOON",),
            )


if __name__ == "__main__":
    unittest.main()
