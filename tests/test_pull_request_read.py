from __future__ import annotations

import json
import unittest

from execution_coordinator.github_state import GitHubApiError
from execution_coordinator.pull_request_read import GitHubPullRequestReader


class _Response:
    def __init__(self, payload, status=200):
        self._raw = json.dumps(payload).encode("utf-8")
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self._raw


def _payload(*, number=12, state="open", head="a" * 40, repository="owner/repo"):
    return {
        "number": number,
        "state": state,
        "html_url": f"https://github.com/{repository}/pull/{number}",
        "head": {"sha": head},
        "base": {"repo": {"full_name": repository}},
    }


class PullRequestReadTests(unittest.TestCase):
    def test_reader_is_get_only_and_returns_exact_snapshot(self):
        seen = []

        def opener(request, timeout):
            seen.append((request, timeout))
            return _Response(_payload())

        reader = GitHubPullRequestReader(
            token="secret-token",
            api_base_url="https://api.github.test",
            opener=opener,
        )
        snapshot = reader.read_pull_request("owner/repo", 12)

        request, timeout = seen[0]
        self.assertEqual("GET", request.get_method())
        self.assertEqual(
            "https://api.github.test/repos/owner/repo/pulls/12",
            request.full_url,
        )
        self.assertEqual("Bearer secret-token", request.get_header("Authorization"))
        self.assertEqual(20.0, timeout)
        self.assertEqual("owner/repo", snapshot.repository)
        self.assertEqual(12, snapshot.number)
        self.assertEqual("open", snapshot.state)
        self.assertEqual("a" * 40, snapshot.head_sha)

    def test_reader_rejects_wrong_base_repository_or_html_identity(self):
        wrong_base = _payload()
        wrong_base["base"] = {"repo": {"full_name": "other/repo"}}
        reader = GitHubPullRequestReader(
            token="",
            opener=lambda request, timeout: _Response(wrong_base),
        )
        with self.assertRaisesRegex(GitHubApiError, "base repository"):
            reader.read_pull_request("owner/repo", 12)

        wrong_url = _payload()
        wrong_url["html_url"] = "https://github.com/other/repo/pull/12"
        reader = GitHubPullRequestReader(
            token="",
            opener=lambda request, timeout: _Response(wrong_url),
        )
        with self.assertRaisesRegex(GitHubApiError, "identity"):
            reader.read_pull_request("owner/repo", 12)

    def test_reader_rejects_invalid_head_sha(self):
        reader = GitHubPullRequestReader(
            token="",
            opener=lambda request, timeout: _Response(_payload(head="abc")),
        )
        with self.assertRaisesRegex(GitHubApiError, "head SHA"):
            reader.read_pull_request("owner/repo", 12)


if __name__ == "__main__":
    unittest.main()
