from __future__ import annotations

import json
import unittest

from execution_coordinator.discovery import (
    DurableIssueSource,
    GitHubIssueReader,
    IssueDocument,
    discover_claim_candidates,
)
from execution_coordinator.model import Role


BODY = """## Type / status

- Type: `FEATURE`
- Work Status: `READY_FOR_IMPLEMENTATION`

## Objective

Implement the bounded change.

## Design / scope

Only the bounded discovery adapter.

## Acceptance criteria

- [ ] The behavior is verified.

## Next action

`[IMPLEMENT] proceed`
"""


class _Reader:
    def __init__(self, document: IssueDocument) -> None:
        self.document = document

    def read_issue(self, source: DurableIssueSource) -> IssueDocument:
        return self.document


class _Response:
    status = 200

    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class DiscoveryTrustTests(unittest.TestCase):
    def source(self) -> DurableIssueSource:
        return DurableIssueSource(
            "owner/repo",
            7,
            Role.IMPLEMENTER,
            ("READY_FOR_IMPLEMENTATION",),
        )

    def document(self, association: str | None) -> IssueDocument:
        return IssueDocument(
            repository="owner/repo",
            number=7,
            state="open",
            body=BODY,
            html_url="https://github.com/owner/repo/issues/7",
            author_association=association,
        )

    def test_outsider_and_missing_author_association_fail_closed(self) -> None:
        for association in ("NONE", "CONTRIBUTOR", None):
            with self.subTest(association=association):
                result = discover_claim_candidates((self.source(),), _Reader(self.document(association)))
                self.assertEqual(result.candidates, ())
                self.assertEqual(len(result.failures), 1)
                self.assertIn("trusted author", result.failures[0].reason)

    def test_owner_member_and_collaborator_are_accepted(self) -> None:
        for association in ("OWNER", "MEMBER", "COLLABORATOR"):
            with self.subTest(association=association):
                result = discover_claim_candidates((self.source(),), _Reader(self.document(association)))
                self.assertEqual(len(result.candidates), 1)
                self.assertEqual(result.failures, ())

    def test_github_issue_reader_captures_author_association(self) -> None:
        def opener(request, timeout):
            return _Response(
                {
                    "number": 7,
                    "state": "open",
                    "body": BODY,
                    "html_url": "https://github.com/owner/repo/issues/7",
                    "author_association": "OWNER",
                }
            )

        document = GitHubIssueReader(token="", opener=opener).read_issue(self.source())
        self.assertEqual(document.author_association, "OWNER")


if __name__ == "__main__":
    unittest.main()
