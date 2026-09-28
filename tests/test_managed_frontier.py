from __future__ import annotations

import hashlib
import json
import unittest
from datetime import datetime, timezone

from execution_coordinator.discovery import DurableIssueSource, IssueDocument
from execution_coordinator.engine import claim
from execution_coordinator.managed_frontier import enumerate_managed_frontier
from execution_coordinator.model import CoordinatorState, Role
from execution_coordinator.query import ClaimabilityReason
from execution_coordinator.reconciliation import MARKER_BEGIN, MARKER_END
from execution_coordinator.snapshot import render_issue_body


NOW = datetime(2026, 9, 28, 5, 0, tzinfo=timezone.utc)
CONTROL = DurableIssueSource("kinoko34077/devflow", 107)

NORMAL_BEGIN = "<!-- DEVFLOW_EXECUTION_CANDIDATES_V1_BEGIN -->"
NORMAL_END = "<!-- DEVFLOW_EXECUTION_CANDIDATES_V1_END -->"


def _digest(body: str) -> str:
    normalized = body.replace("\r\n", "\n").replace("\r", "\n")
    return "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _document(repository: str, number: int, body: str, *, title: str = "Task") -> IssueDocument:
    return IssueDocument(
        repository=repository,
        number=number,
        state="open",
        body=body,
        html_url=f"https://github.com/{repository}/issues/{number}",
        title=title,
        author_association="MEMBER",
    )


def _normal_projection(task_body: str, *, task_number: int = 8) -> str:
    envelope = {
        "task": f"owner/repo#{task_number}",
        "task_body_sha256": _digest(task_body),
        "task_work_status": "READY_FOR_IMPLEMENTATION",
        "entry_ref": f"https://github.com/owner/repo/issues/{task_number}",
        "scope_ready": True,
        "blocked": False,
        "requires_user_confirmation": False,
        "roles": [{"role": "implementer", "next_action_tag": "IMPLEMENT"}],
    }
    payload = {
        "schema_version": 1,
        "source_ref": "kinoko34077/devflow#107",
        "repository": "owner/repo",
        "candidates": [envelope],
    }
    return f"{NORMAL_BEGIN}\n{json.dumps(payload)}\n{NORMAL_END}"


def _publication(task_body: str, *, role: str = "recovery") -> dict[str, object]:
    task_ref = "owner/repo#7"
    entry_ref = "https://github.com/owner/repo/issues/7"
    digest = _digest(task_body)
    disposition = "NEEDS_RECOVERY"
    reasons = ["STALE_SESSION_RECOVERY_READY"]
    context: dict[str, object] = {
        "predecessor_session_id": "session-7",
        "checkpoint": "implementation committed",
        "next_action": "run the repository regression suite",
        "recovery_transition": "RECOVERY_ASSESSMENT",
        "artifact_refs": ["refs/heads/work/7"],
    }
    identity = {
        "schema_version": "development-reconciliation-work.v1",
        "source_contract_version": "development-reconciliation.v1",
        "task_ref": task_ref,
        "task_body_sha256": digest,
        "entry_ref": entry_ref,
        "role": role,
        "disposition": disposition,
        "reason_codes": reasons,
        "scope": "bounded role acceptance scope",
        "context": context,
    }
    encoded = json.dumps(
        identity,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return {
        "schema_version": "development-reconciliation-work.v1",
        "publication_id": "sha256:" + hashlib.sha256(encoded).hexdigest(),
        "task_ref": task_ref,
        "task_body_sha256": digest,
        "entry_ref": entry_ref,
        "role": role,
        "disposition": disposition,
        "reason_codes": reasons,
        "scope": "bounded role acceptance scope",
        "requires_user_confirmation": False,
        "observed_at": "2026-09-28T05:00:00Z",
        "freshness": {
            "source_contract_version": "development-reconciliation.v1",
            "task_body_sha256": digest,
        },
        "context": context,
    }


def _control_body(*, normal: str | None = None, publications: list[dict[str, object]] | None = None) -> str:
    body = (
        "## Repository\n\n`owner/repo`\n\n"
        "## Repository State\n\n`ACTIVE`\n\n"
        "## Work Status\n\n`AUDITED`\n\n"
        "## Next Action\n\n`[IMPLEMENT] bounded task`\n"
    )
    if normal is not None:
        body += f"\n{normal}\n"
    if publications is not None:
        payload = {
            "schema_version": "development-reconciliation-work.v1",
            "repository": "owner/repo",
            "publications": publications,
        }
        body += f"\n{MARKER_BEGIN}\n{json.dumps(payload, indent=2, sort_keys=True)}\n{MARKER_END}\n"
    return body


class _IssueReader:
    def __init__(self, documents: dict[tuple[str, int], IssueDocument]) -> None:
        self.documents = documents
        self.calls: list[tuple[str, int]] = []

    def read_issue(self, repository: str, issue_number: int) -> IssueDocument:
        key = (repository, issue_number)
        self.calls.append(key)
        return self.documents[key]


class _StateReader:
    def __init__(self, state: CoordinatorState | None = None) -> None:
        self.body = render_issue_body("", state or CoordinatorState.empty())
        self.calls = 0
        self.write_calls = 0

    def load_body(self) -> str:
        self.calls += 1
        return self.body

    def save_body(self, body: str) -> None:
        self.write_calls += 1
        raise AssertionError("managed frontier enumeration must not write runtime state")


class ManagedFrontierTests(unittest.TestCase):
    def test_combines_normal_and_recovery_demand_with_cached_issue_reads(self) -> None:
        normal_body = "## Scope\n\nnormal task"
        recovery_body = "## Scope\n\nrecovery task"
        reader = _IssueReader(
            {
                ("kinoko34077/devflow", 107): _document(
                    "kinoko34077/devflow",
                    107,
                    _control_body(
                        normal=_normal_projection(normal_body),
                        publications=[_publication(recovery_body)],
                    ),
                    title="[REPO] repo",
                ),
                ("owner/repo", 8): _document("owner/repo", 8, normal_body),
                ("owner/repo", 7): _document("owner/repo", 7, recovery_body),
            }
        )
        state = _StateReader()

        result = enumerate_managed_frontier(
            [CONTROL], issue_reader=reader, state_reader=state, now=NOW
        )

        self.assertIsNotNone(result.read)
        self.assertEqual(result.source_failures, ())
        self.assertEqual(
            [(candidate.task, candidate.role) for candidate in result.candidates],
            [("owner/repo#8", Role.IMPLEMENTER), ("owner/repo#7", Role.RECOVERY)],
        )
        self.assertEqual([candidate.task for candidate in result.fresh_candidates], ["owner/repo#8"])
        self.assertEqual([candidate.task for candidate in result.recovery_candidates], ["owner/repo#7"])
        self.assertEqual(
            reader.calls,
            [
                ("kinoko34077/devflow", 107),
                ("owner/repo", 8),
                ("owner/repo", 7),
            ],
        )
        self.assertEqual(state.calls, 1)
        self.assertEqual(state.write_calls, 0)

    def test_distinct_controls_in_devflow_are_not_duplicates(self) -> None:
        # Every Repository Control lives in kinoko34077/devflow; the managed
        # repository identity comes from the canonical Control title.
        other_control = DurableIssueSource("kinoko34077/devflow", 108)
        reader = _IssueReader(
            {
                ("kinoko34077/devflow", 107): _document(
                    "kinoko34077/devflow", 107, _control_body(), title="[REPO] repo"
                ),
                ("kinoko34077/devflow", 108): _document(
                    "kinoko34077/devflow", 108, _control_body(), title="[REPO] other"
                ),
            }
        )
        state = _StateReader()

        result = enumerate_managed_frontier(
            [other_control, CONTROL],
            issue_reader=reader,
            state_reader=state,
            now=NOW,
        )

        self.assertEqual(result.source_failures, ())
        self.assertIsNotNone(result.read)
        self.assertEqual(result.sources, (CONTROL, other_control))
        self.assertEqual(
            sorted(set(reader.calls)),
            [("kinoko34077/devflow", 107), ("kinoko34077/devflow", 108)],
        )

    def test_duplicate_managed_repository_control_identity_fails_closed(self) -> None:
        duplicate_control = DurableIssueSource("kinoko34077/devflow", 108)
        reader = _IssueReader(
            {
                ("kinoko34077/devflow", 107): _document(
                    "kinoko34077/devflow", 107, _control_body(), title="[REPO] repo"
                ),
                ("kinoko34077/devflow", 108): _document(
                    "kinoko34077/devflow", 108, _control_body(), title="[REPO] Repo"
                ),
            }
        )
        state = _StateReader()

        result = enumerate_managed_frontier(
            [duplicate_control, CONTROL],
            issue_reader=reader,
            state_reader=state,
            now=NOW,
        )

        self.assertIsNone(result.read)
        self.assertEqual(result.sources, (CONTROL, duplicate_control))
        self.assertEqual(
            [failure.source_ref for failure in result.source_failures],
            ["kinoko34077/devflow#107", "kinoko34077/devflow#108"],
        )
        self.assertTrue(
            all("duplicate managed repository" in f.reason for f in result.source_failures)
        )
        self.assertEqual(state.calls, 0)

    def test_exact_duplicate_source_fails_closed_without_reads(self) -> None:
        reader = _IssueReader({})
        state = _StateReader()

        result = enumerate_managed_frontier(
            [CONTROL, DurableIssueSource("kinoko34077/devflow", 107)],
            issue_reader=reader,
            state_reader=state,
            now=NOW,
        )

        self.assertIsNone(result.read)
        self.assertEqual(len(result.source_failures), 1)
        self.assertIn("duplicate Repository Control source", result.source_failures[0].reason)
        self.assertEqual(reader.calls, [])
        self.assertEqual(state.calls, 0)

    def test_invalid_source_type_fails_closed_without_partial_reads(self) -> None:
        reader = _IssueReader({})
        state = _StateReader()

        result = enumerate_managed_frontier(
            [CONTROL, "kinoko34077/devflow#107"],  # type: ignore[list-item]
            issue_reader=reader,
            state_reader=state,
            now=NOW,
        )

        self.assertIsNone(result.read)
        self.assertEqual(result.candidates, ())
        self.assertEqual(len(result.source_failures), 1)
        self.assertIn("DurableIssueSource", result.source_failures[0].reason)
        self.assertEqual(reader.calls, [])
        self.assertEqual(state.calls, 0)

    def test_validator_failure_is_retained_inside_a_valid_frontier_read(self) -> None:
        reader = _IssueReader(
            {
                ("kinoko34077/devflow", 107): _document(
                    "kinoko34077/devflow",
                    107,
                    _control_body(
                        normal=f"{NORMAL_BEGIN}\n{{bad json}}\n{NORMAL_END}"
                    ),
                    title="[REPO] repo",
                )
            }
        )
        state = _StateReader()

        result = enumerate_managed_frontier(
            [CONTROL], issue_reader=reader, state_reader=state, now=NOW
        )

        self.assertIsNotNone(result.read)
        self.assertEqual(result.candidates, ())
        self.assertEqual(len(result.read.discovery.failures), 1)
        self.assertIn("malformed", result.read.discovery.failures[0].reason)
        self.assertEqual(reader.calls, [("kinoko34077/devflow", 107)])
        self.assertEqual(state.calls, 1)

    def test_runtime_blocker_is_preserved_as_nonclaimable_reason(self) -> None:
        task_body = "## Scope\n\nnormal task"
        reader = _IssueReader(
            {
                ("kinoko34077/devflow", 107): _document(
                    "kinoko34077/devflow",
                    107,
                    _control_body(normal=_normal_projection(task_body)),
                    title="[REPO] repo",
                ),
                ("owner/repo", 8): _document("owner/repo", 8, task_body),
            }
        )
        active = claim(
            CoordinatorState.empty(),
            task="owner/repo#8",
            role=Role.IMPLEMENTER,
            worker_id="worker-a",
            conflict_keys=(),
            now=NOW,
            idempotency_key="active-implementer",
        ).state
        state = _StateReader(active)

        result = enumerate_managed_frontier(
            [CONTROL], issue_reader=reader, state_reader=state, now=NOW
        )

        self.assertEqual(result.claimable, ())
        self.assertEqual(result.claimability[0].reason, ClaimabilityReason.BLOCKED_LIVE)
        self.assertEqual(state.write_calls, 0)


if __name__ == "__main__":
    unittest.main()
