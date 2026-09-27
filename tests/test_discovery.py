from __future__ import annotations

import importlib
import io
import json
import unittest
from urllib.error import HTTPError

from execution_coordinator.github_state import GitHubApiError
from execution_coordinator.model import Role


class _FakeReader:
    def __init__(self, documents: dict[tuple[str, int], object]) -> None:
        self.documents = documents
        self.calls: list[tuple[str, int]] = []

    def read_issue(self, source):
        key = (source.repository, source.issue_number)
        self.calls.append(key)
        return self.documents[key]


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


def _body(
    *,
    status: str = "READY_FOR_IMPLEMENTATION",
    objective: str | None = "Implement the bounded change.",
    scope: str | None = "Only the bounded discovery adapter.",
    acceptance: str | None = "- [ ] The behavior is verified.",
    next_action: str | None = "`[IMPLEMENT] proceed`",
    extra: str = "",
) -> str:
    sections = [
        "## Type / status\n\n- Type: `FEATURE`\n- Work Status: `" + status + "`",
    ]
    if objective is not None:
        sections.append("## Objective\n\n" + objective)
    if scope is not None:
        sections.append("## Design / scope\n\n" + scope)
    if acceptance is not None:
        sections.append("## Acceptance criteria\n\n" + acceptance)
    if next_action is not None:
        sections.append("## Next action\n\n" + next_action)
    if extra:
        sections.append(extra)
    return "\n\n".join(sections)


class DiscoveryContractTests(unittest.TestCase):
    def _api(self):
        try:
            module = importlib.import_module("execution_coordinator.discovery")
        except ModuleNotFoundError as exc:
            self.fail(f"discovery module must exist: {exc}")
        required = (
            "DurableIssueSource",
            "IssueDocument",
            "DiscoveryFailure",
            "DiscoveryResult",
            "GitHubIssueReader",
            "discover_claim_candidates",
        )
        for name in required:
            self.assertTrue(hasattr(module, name), f"{name} must exist")
        return module

    def test_strict_normalization_preserves_order_role_policy_and_conflict_keys(self) -> None:
        api = self._api()
        Source = api.DurableIssueSource
        Document = api.IssueDocument
        first = Source(
            repository="owner/repo",
            issue_number=7,
            role=Role.IMPLEMENTER,
            eligible_work_states=("READY_FOR_IMPLEMENTATION",),
            conflict_keys=("component:owner/repo:core",),
        )
        second = Source(
            repository="owner/repo",
            issue_number=8,
            role=Role.REVIEWER,
            eligible_work_states=("AWAITING_REVIEW",),
            conflict_keys=("component:owner/repo:docs",),
        )
        reader = _FakeReader(
            {
                ("owner/repo", 7): Document(
                    repository="owner/repo",
                    number=7,
                    state="open",
                    body=_body(),
                    html_url="https://github.com/owner/repo/issues/7",
                ),
                ("owner/repo", 8): Document(
                    repository="owner/repo",
                    number=8,
                    state="open",
                    body=_body(status="READY_FOR_IMPLEMENTATION"),
                    html_url="https://github.com/owner/repo/issues/8",
                ),
            }
        )

        result = api.discover_claim_candidates((first, second), reader)

        self.assertEqual(tuple(c.task for c in result.candidates), ("owner/repo#7", "owner/repo#8"))
        self.assertEqual(result.candidates[0].role, Role.IMPLEMENTER)
        self.assertEqual(result.candidates[0].conflict_keys, ("component:owner/repo:core",))
        self.assertTrue(result.candidates[0].scope_ready)
        self.assertEqual(result.candidates[1].role, Role.REVIEWER)
        self.assertEqual(result.candidates[1].conflict_keys, ("component:owner/repo:docs",))
        self.assertFalse(result.candidates[1].scope_ready)
        self.assertEqual(result.failures, ())
        self.assertEqual(reader.calls, [("owner/repo", 7), ("owner/repo", 8)])

    def test_closed_issues_and_pull_request_objects_are_explicit_failures(self) -> None:
        api = self._api()
        Source = api.DurableIssueSource
        Document = api.IssueDocument
        closed = Source("owner/repo", 1, Role.IMPLEMENTER, ("READY_FOR_IMPLEMENTATION",))
        pull_request = Source("owner/repo", 2, Role.IMPLEMENTER, ("READY_FOR_IMPLEMENTATION",))
        reader = _FakeReader(
            {
                ("owner/repo", 1): Document("owner/repo", 1, "closed", _body(), "https://github.com/owner/repo/issues/1"),
                ("owner/repo", 2): Document("owner/repo", 2, "open", _body(), "https://github.com/owner/repo/pull/2", True),
            }
        )

        result = api.discover_claim_candidates((closed, pull_request), reader)

        self.assertEqual(result.candidates, ())
        self.assertEqual(len(result.failures), 2)
        self.assertIn("open Issue", result.failures[0].reason)
        self.assertIn("pull request", result.failures[1].reason.lower())

    def test_missing_duplicate_and_malformed_work_status_fail_closed(self) -> None:
        api = self._api()
        Source = api.DurableIssueSource
        Document = api.IssueDocument
        sources = tuple(
            Source("owner/repo", n, Role.IMPLEMENTER, ("READY_FOR_IMPLEMENTATION",))
            for n in (1, 2, 3)
        )
        missing = _body().replace("- Work Status: `READY_FOR_IMPLEMENTATION`\n", "")
        duplicate = _body(extra="## Work Status\n\n`READY_FOR_IMPLEMENTATION`")
        malformed = _body(status="ready soon")
        reader = _FakeReader(
            {
                ("owner/repo", 1): Document("owner/repo", 1, "open", missing, "https://github.com/owner/repo/issues/1"),
                ("owner/repo", 2): Document("owner/repo", 2, "open", duplicate, "https://github.com/owner/repo/issues/2"),
                ("owner/repo", 3): Document("owner/repo", 3, "open", malformed, "https://github.com/owner/repo/issues/3"),
            }
        )

        result = api.discover_claim_candidates(sources, reader)

        self.assertEqual(result.candidates, ())
        self.assertEqual(len(result.failures), 3)
        self.assertTrue(all("Work Status" in failure.reason for failure in result.failures))

    def test_missing_required_objective_scope_or_acceptance_fails_closed(self) -> None:
        api = self._api()
        Source = api.DurableIssueSource
        Document = api.IssueDocument
        sources = tuple(Source("owner/repo", n, Role.IMPLEMENTER, ("READY_FOR_IMPLEMENTATION",)) for n in (1, 2, 3))
        reader = _FakeReader(
            {
                ("owner/repo", 1): Document("owner/repo", 1, "open", _body(objective=None), "https://github.com/owner/repo/issues/1"),
                ("owner/repo", 2): Document("owner/repo", 2, "open", _body(scope=None), "https://github.com/owner/repo/issues/2"),
                ("owner/repo", 3): Document("owner/repo", 3, "open", _body(acceptance=None), "https://github.com/owner/repo/issues/3"),
            }
        )

        result = api.discover_claim_candidates(sources, reader)

        self.assertEqual(result.candidates, ())
        self.assertEqual(len(result.failures), 3)
        joined = " ".join(f.reason for f in result.failures)
        self.assertIn("Objective", joined)
        self.assertIn("Scope", joined)
        self.assertIn("Acceptance", joined)

    def test_blocked_and_explicit_user_decision_are_normalized_without_broad_text_inference(self) -> None:
        api = self._api()
        Source = api.DurableIssueSource
        Document = api.IssueDocument
        blocked = Source("owner/repo", 1, Role.IMPLEMENTER, ("BLOCKED",))
        user = Source("owner/repo", 2, Role.IMPLEMENTER, ("READY_FOR_IMPLEMENTATION",))
        incidental = Source("owner/repo", 3, Role.IMPLEMENTER, ("READY_FOR_IMPLEMENTATION",))
        reader = _FakeReader(
            {
                ("owner/repo", 1): Document("owner/repo", 1, "open", _body(status="BLOCKED"), "https://github.com/owner/repo/issues/1"),
                ("owner/repo", 2): Document("owner/repo", 2, "open", _body(next_action="`[USER_DECISION] choose A or B`"), "https://github.com/owner/repo/issues/2"),
                ("owner/repo", 3): Document("owner/repo", 3, "open", _body(objective="Discuss literal [USER_DECISION] documentation.", next_action="`[IMPLEMENT] proceed`"), "https://github.com/owner/repo/issues/3"),
            }
        )

        result = api.discover_claim_candidates((blocked, user, incidental), reader)

        self.assertEqual(len(result.candidates), 3)
        self.assertTrue(result.candidates[0].blocked)
        self.assertFalse(result.candidates[0].requires_user_confirmation)
        self.assertTrue(result.candidates[1].requires_user_confirmation)
        self.assertFalse(result.candidates[2].requires_user_confirmation)

    def test_one_malformed_source_does_not_hide_valid_sibling_candidates(self) -> None:
        api = self._api()
        Source = api.DurableIssueSource
        Document = api.IssueDocument
        good1 = Source("owner/repo", 1, Role.IMPLEMENTER, ("READY_FOR_IMPLEMENTATION",))
        bad = Source("owner/repo", 2, Role.IMPLEMENTER, ("READY_FOR_IMPLEMENTATION",))
        good3 = Source("owner/repo", 3, Role.IMPLEMENTER, ("READY_FOR_IMPLEMENTATION",))
        reader = _FakeReader(
            {
                ("owner/repo", 1): Document("owner/repo", 1, "open", _body(), "https://github.com/owner/repo/issues/1"),
                ("owner/repo", 2): Document("owner/repo", 2, "open", _body(scope=None), "https://github.com/owner/repo/issues/2"),
                ("owner/repo", 3): Document("owner/repo", 3, "open", _body(), "https://github.com/owner/repo/issues/3"),
            }
        )

        result = api.discover_claim_candidates((good1, bad, good3), reader)

        self.assertEqual(tuple(c.task for c in result.candidates), ("owner/repo#1", "owner/repo#3"))
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.failures[0].source, bad)

    def test_github_issue_reader_is_get_only_and_detects_pull_requests(self) -> None:
        api = self._api()
        Source = api.DurableIssueSource
        seen: list[object] = []

        def opener(request, timeout):
            seen.append((request, timeout))
            return _Response(
                {
                    "number": 7,
                    "state": "open",
                    "body": _body(),
                    "html_url": "https://github.com/owner/repo/pull/7",
                    "pull_request": {"url": "https://api.github.com/repos/owner/repo/pulls/7"},
                }
            )

        reader = api.GitHubIssueReader(
            token="secret-token",
            api_base_url="https://api.github.test",
            opener=opener,
        )
        source = Source("owner/repo", 7, Role.REVIEWER, ("AWAITING_REVIEW",))

        document = reader.read_issue(source)

        request, timeout = seen[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.full_url, "https://api.github.test/repos/owner/repo/issues/7")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-token")
        self.assertEqual(timeout, 20.0)
        self.assertTrue(document.is_pull_request)
        self.assertEqual(document.repository, "owner/repo")
        self.assertEqual(document.number, 7)

    def test_github_issue_reader_omits_auth_when_token_empty(self) -> None:
        api = self._api()
        Source = api.DurableIssueSource
        seen = []

        def opener(request, timeout):
            seen.append(request)
            return _Response(
                {
                    "number": 1,
                    "state": "open",
                    "body": _body(),
                    "html_url": "https://github.com/owner/repo/issues/1",
                }
            )

        reader = api.GitHubIssueReader(token="", opener=opener)
        reader.read_issue(Source("owner/repo", 1, Role.IMPLEMENTER, ("READY_FOR_IMPLEMENTATION",)))

        self.assertIsNone(seen[0].get_header("Authorization"))

    def test_github_issue_reader_propagates_sanitized_non_2xx_failure(self) -> None:
        api = self._api()
        Source = api.DurableIssueSource

        def opener(request, timeout):
            raise HTTPError(request.full_url, 403, "Forbidden", {}, io.BytesIO(b"secret-token"))

        reader = api.GitHubIssueReader(token="secret-token", opener=opener)
        source = Source("owner/repo", 1, Role.IMPLEMENTER, ("READY_FOR_IMPLEMENTATION",))

        with self.assertRaises(GitHubApiError) as ctx:
            reader.read_issue(source)

        self.assertIn("HTTP 403", str(ctx.exception))
        self.assertNotIn("secret-token", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
