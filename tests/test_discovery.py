from __future__ import annotations

import io
import json
import unittest
from urllib.error import HTTPError

from execution_coordinator.discovery import DurableIssueSource, GitHubIssueReader
from execution_coordinator.github_state import GitHubApiError


class _Response:
    def __init__(self, payload: dict[str, object], status: int = 200) -> None:
        self._raw = json.dumps(payload).encode("utf-8")
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return self._raw


class DiscoveryTransportTests(unittest.TestCase):
    def test_durable_source_contains_only_exact_owning_issue_identity(self) -> None:
        source = DurableIssueSource(repository="owner/repo", issue_number=7)

        self.assertEqual(source.repository, "owner/repo")
        self.assertEqual(source.issue_number, 7)
        self.assertFalse(hasattr(source, "role"))
        self.assertFalse(hasattr(source, "eligible_work_states"))
        self.assertFalse(hasattr(source, "conflict_keys"))

    def test_durable_source_rejects_invalid_repository_or_issue_number(self) -> None:
        with self.assertRaisesRegex(ValueError, "owner/name"):
            DurableIssueSource(repository="not-a-repository", issue_number=1)
        with self.assertRaisesRegex(ValueError, "positive"):
            DurableIssueSource(repository="owner/repo", issue_number=0)

    def test_github_issue_reader_is_get_only_and_detects_pull_requests(self) -> None:
        seen: list[object] = []

        def opener(request, timeout):
            seen.append((request, timeout))
            return _Response(
                {
                    "number": 7,
                    "state": "open",
                    "title": "Candidate PR",
                    "body": "PR body",
                    "html_url": "https://github.com/owner/repo/pull/7",
                    "pull_request": {
                        "url": "https://api.github.com/repos/owner/repo/pulls/7"
                    },
                }
            )

        reader = GitHubIssueReader(
            token="secret-token",
            api_base_url="https://api.github.test",
            opener=opener,
        )

        document = reader.read_issue("owner/repo", 7)

        request, timeout = seen[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(
            request.full_url, "https://api.github.test/repos/owner/repo/issues/7"
        )
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-token")
        self.assertEqual(timeout, 20.0)
        self.assertTrue(document.is_pull_request)
        self.assertEqual(document.repository, "owner/repo")
        self.assertEqual(document.number, 7)
        self.assertEqual(document.title, "Candidate PR")

    def test_github_issue_reader_omits_auth_when_token_empty(self) -> None:
        seen = []

        def opener(request, timeout):
            seen.append(request)
            return _Response(
                {
                    "number": 1,
                    "state": "open",
                    "title": "Task",
                    "body": "body",
                    "html_url": "https://github.com/owner/repo/issues/1",
                }
            )

        reader = GitHubIssueReader(token="", opener=opener)
        reader.read_issue("owner/repo", 1)

        self.assertIsNone(seen[0].get_header("Authorization"))

    def _reader_returning(self, payload: dict[str, object]) -> GitHubIssueReader:
        return GitHubIssueReader(
            token="",
            api_base_url="https://api.github.test",
            opener=lambda request, timeout: _Response(payload),
        )

    def test_github_issue_reader_rejects_response_from_another_repository(self) -> None:
        # e.g. a transferred Issue whose redirect lands in a different repository
        reader = self._reader_returning(
            {
                "number": 7,
                "state": "open",
                "body": "",
                "html_url": "https://github.com/other/repo/issues/7",
            }
        )

        with self.assertRaisesRegex(GitHubApiError, "repository identity"):
            reader.read_issue("owner/repo", 7)

    def test_github_issue_reader_rejects_response_with_another_number(self) -> None:
        reader = self._reader_returning(
            {
                "number": 8,
                "state": "open",
                "body": "",
                "html_url": "https://github.com/owner/repo/issues/8",
            }
        )

        with self.assertRaisesRegex(GitHubApiError, "issue number"):
            reader.read_issue("owner/repo", 7)

    def test_github_issue_reader_rejects_non_canonical_html_url(self) -> None:
        for html_url in (
            "https://example.com/owner/repo/issues/7",
            "https://github.com/owner/repo/issues/7#x",
            "https://github.com/owner/repo/issues/8",
            "https://github.com/Owner/repo/issues/7",
        ):
            with self.subTest(html_url=html_url):
                reader = self._reader_returning(
                    {"number": 7, "state": "open", "body": "", "html_url": html_url}
                )
                with self.assertRaises(GitHubApiError):
                    reader.read_issue("owner/repo", 7)

    def test_github_issue_reader_rejects_mismatched_repository_url(self) -> None:
        reader = self._reader_returning(
            {
                "number": 7,
                "state": "open",
                "body": "",
                "html_url": "https://github.com/owner/repo/issues/7",
                "repository_url": "https://api.github.test/repos/other/repo",
            }
        )

        with self.assertRaisesRegex(GitHubApiError, "repository identity"):
            reader.read_issue("owner/repo", 7)

    def test_github_issue_reader_accepts_matching_repository_url(self) -> None:
        reader = self._reader_returning(
            {
                "number": 7,
                "state": "open",
                "body": "",
                "html_url": "https://github.com/owner/repo/issues/7",
                "repository_url": "https://api.github.test/repos/owner/repo",
            }
        )

        document = reader.read_issue("owner/repo", 7)

        self.assertEqual(document.repository, "owner/repo")
        self.assertEqual(document.html_url, "https://github.com/owner/repo/issues/7")

    def test_github_issue_reader_propagates_sanitized_non_2xx_failure(self) -> None:
        def opener(request, timeout):
            raise HTTPError(
                request.full_url,
                403,
                "Forbidden",
                {},
                io.BytesIO(b"secret-token"),
            )

        reader = GitHubIssueReader(token="secret-token", opener=opener)

        with self.assertRaises(GitHubApiError) as ctx:
            reader.read_issue("owner/repo", 1)

        self.assertIn("HTTP 403", str(ctx.exception))
        self.assertNotIn("secret-token", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
