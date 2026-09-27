from __future__ import annotations

import json
import unittest

from execution_coordinator.discovery import (
    DurableIssueSource,
    IssueDocument,
    discover_claim_candidates,
)


BEGIN = "<!-- DEVFLOW_EXECUTION_CANDIDATE_V1_BEGIN -->"
END = "<!-- DEVFLOW_EXECUTION_CANDIDATE_V1_END -->"


def _marker(role: str, task_number: int = 1) -> str:
    payload = {
        "schema_version": 1,
        "task_ref": f"owner/repo#{task_number}",
        "entry_ref": f"https://github.com/owner/repo/issues/{task_number}",
        "role": role,
        "scope_ready": True,
        "blocked": False,
        "requires_user_confirmation": False,
        "provenance": {"control_ref": "kinoko34077/devflow#17"},
    }
    return f"{BEGIN}\n{json.dumps(payload)}\n{END}"


def _control(status: str) -> str:
    return (
        "## Repository\n\n`owner/repo`\n\n"
        f"## Work Status\n\n`{status}`\n\n"
        "## Next Action\n\n`[VERIFY] bounded stage`"
    )


class _Reader:
    def __init__(self, status: str, role: str) -> None:
        self.status = status
        self.role = role

    def read_issue(self, repository: str, issue_number: int) -> IssueDocument:
        if repository == "owner/repo":
            return IssueDocument(
                repository=repository,
                number=issue_number,
                state="open",
                title="Task",
                body=_marker(self.role, issue_number),
                html_url=f"https://github.com/{repository}/issues/{issue_number}",
            )
        return IssueDocument(
            repository="kinoko34077/devflow",
            number=17,
            state="open",
            title="[REPO] repo",
            body=_control(self.status),
            html_url="https://github.com/kinoko34077/devflow/issues/17",
        )


class DiscoveryStatusVocabularyTests(unittest.TestCase):
    def test_unknown_uppercase_control_work_status_is_an_explicit_failure(self) -> None:
        source = DurableIssueSource("owner/repo", 1)

        result = discover_claim_candidates((source,), _Reader("READY_SOON", "implementer"))

        self.assertEqual(result.candidates, ())
        self.assertEqual(len(result.failures), 1)
        self.assertIn("unsupported Control Work Status", result.failures[0].reason)

    def test_ordinary_role_status_matrix_is_fixed_by_devflow_contract(self) -> None:
        cases = (
            ("implementer", "READY_FOR_IMPLEMENTATION", True),
            ("implementer", "AWAITING_REVIEW", False),
            ("reviewer", "AWAITING_REVIEW", True),
            ("verifier", "AWAITING_REVIEW", True),
            ("integrator", "AWAITING_REVIEW", True),
            ("reviewer", "READY_FOR_IMPLEMENTATION", False),
        )

        for role, status, expected in cases:
            with self.subTest(role=role, status=status):
                result = discover_claim_candidates(
                    (DurableIssueSource("owner/repo", 1),),
                    _Reader(status, role),
                )
                self.assertEqual(bool(result.candidates), expected)
                self.assertEqual(bool(result.failures), not expected)


if __name__ == "__main__":
    unittest.main()
