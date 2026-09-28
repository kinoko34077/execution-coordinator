from __future__ import annotations

import hashlib
import json
import unittest
from datetime import datetime, timezone

from execution_coordinator.discovery import DurableIssueSource, IssueDocument
from execution_coordinator.engine import ClaimConflict, claim
from execution_coordinator.model import CoordinatorState, Role
from execution_coordinator.query import list_claimable
from execution_coordinator.reconciliation import (
    MARKER_BEGIN,
    MARKER_END,
    discover_reconciliation_claim_candidates,
)
from execution_coordinator.snapshot import state_from_data, state_to_data


REPOSITORY = "owner/repo"
CONTROL_SOURCE = DurableIssueSource("kinoko34077/devflow", 107)
TASK_BODY = "## Work Status\n\n`AWAITING_REVIEW`\n"


def _digest(body: str) -> str:
    normalized = body.replace("\r\n", "\n").replace("\r", "\n")
    return "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _publication_id(publication: dict[str, object]) -> str:
    freshness = publication["freshness"]
    assert isinstance(freshness, dict)
    identity = {
        "schema_version": publication["schema_version"],
        "source_contract_version": freshness["source_contract_version"],
        "task_ref": publication["task_ref"],
        "task_body_sha256": publication["task_body_sha256"],
        "entry_ref": publication["entry_ref"],
        "role": publication["role"],
        "disposition": publication["disposition"],
        "reason_codes": publication["reason_codes"],
        "scope": publication["scope"],
        "context": publication["context"],
    }
    encoded = json.dumps(
        identity,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _publication(role: str = "reviewer") -> dict[str, object]:
    digest = _digest(TASK_BODY)
    if role == "reviewer":
        disposition = "NEEDS_REVIEWER"
        reasons = ["DIFFERENT_REVIEWER_REQUIRED"]
        context: dict[str, object] = {
            "pr_number": 12,
            "pr_head_sha": "a" * 40,
        }
    elif role == "recovery":
        disposition = "NEEDS_RECOVERY"
        reasons = ["STALE_SESSION_RECOVERY_READY"]
        context = {
            "predecessor_session_id": "session-7",
            "checkpoint": "implementation committed",
            "next_action": "run the repository regression suite",
            "recovery_transition": "RECOVERY_ASSESSMENT",
            "artifact_refs": ["refs/heads/work/7"],
        }
    else:
        disposition = "NEEDS_RECOVERY"
        reasons = ["UNSUPPORTED_ROLE"]
        context = {}

    publication: dict[str, object] = {
        "schema_version": "development-reconciliation-work.v1",
        "publication_id": "",
        "task_ref": f"{REPOSITORY}#7",
        "task_body_sha256": digest,
        "entry_ref": f"https://github.com/{REPOSITORY}/issues/7",
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
    publication["publication_id"] = _publication_id(publication)
    return publication


def _control_body(publications: list[dict[str, object]], *, repository: str = REPOSITORY) -> str:
    payload = {
        "schema_version": "development-reconciliation-work.v1",
        "repository": repository,
        "publications": publications,
    }
    return (
        "## Repository\n\n"
        f"`{repository}`\n\n"
        "## Repository State\n\n"
        "`ACTIVE`\n\n"
        "## Next Action\n\n"
        "`[IMPLEMENT] continue bounded work`\n\n"
        f"{MARKER_BEGIN}\n"
        f"{json.dumps(payload, indent=2, sort_keys=True)}\n"
        f"{MARKER_END}\n"
    )


def _control_document(publications: list[dict[str, object]], *, repository: str = REPOSITORY) -> IssueDocument:
    return IssueDocument(
        repository="kinoko34077/devflow",
        number=107,
        state="open",
        body=_control_body(publications, repository=repository),
        html_url="https://github.com/kinoko34077/devflow/issues/107",
        title=f"[REPO] {repository.split('/', 1)[1]}",
        author_association="OWNER",
    )


def _task_document(*, body: str = TASK_BODY) -> IssueDocument:
    return IssueDocument(
        repository=REPOSITORY,
        number=7,
        state="open",
        body=body,
        html_url=f"https://github.com/{REPOSITORY}/issues/7",
        title="task",
        author_association="OWNER",
    )


class _Reader:
    def __init__(self, control: IssueDocument, task: IssueDocument | None = None) -> None:
        self._documents = {
            (control.repository, control.number): control,
        }
        if task is not None:
            self._documents[(task.repository, task.number)] = task

    def read_issue(self, repository: str, issue_number: int) -> IssueDocument:
        return self._documents[(repository, issue_number)]


class ReconciliationAdoptionTests(unittest.TestCase):
    def test_reviewer_publication_maps_to_existing_claim_candidate(self) -> None:
        reader = _Reader(_control_document([_publication("reviewer")]), _task_document())

        result = discover_reconciliation_claim_candidates((CONTROL_SOURCE,), reader)

        self.assertEqual(result.failures, ())
        self.assertEqual(len(result.candidates), 1)
        candidate = result.candidates[0]
        self.assertEqual(candidate.task, f"{REPOSITORY}#7")
        self.assertEqual(candidate.role, Role.REVIEWER)
        self.assertEqual(candidate.entry_ref, f"https://github.com/{REPOSITORY}/issues/7")
        self.assertEqual(candidate.conflict_keys, ())
        self.assertTrue(candidate.scope_ready)
        self.assertFalse(candidate.blocked)
        self.assertFalse(candidate.requires_user_confirmation)
        self.assertEqual(list_claimable((candidate,), CoordinatorState.empty()), (candidate,))

    def test_recovery_publication_maps_to_recovery_role_and_round_trips_snapshot(self) -> None:
        reader = _Reader(_control_document([_publication("recovery")]), _task_document())
        result = discover_reconciliation_claim_candidates((CONTROL_SOURCE,), reader)
        self.assertEqual(result.failures, ())
        candidate = result.candidates[0]
        self.assertEqual(candidate.role, Role.RECOVERY)
        self.assertEqual(candidate.conflict_keys, ())

        claimed = claim(
            CoordinatorState.empty(),
            task=candidate.task,
            role=candidate.role,
            worker_id="worker-1",
            conflict_keys=candidate.conflict_keys,
            now=datetime(2026, 9, 28, 5, 0, tzinfo=timezone.utc),
            idempotency_key="recovery-claim",
        ).state
        restored = state_from_data(state_to_data(claimed))
        self.assertEqual(next(iter(restored.claims.values())).role, Role.RECOVERY)

    def test_stale_task_body_digest_fails_closed(self) -> None:
        reader = _Reader(
            _control_document([_publication("reviewer")]),
            _task_document(body=TASK_BODY + "\nchanged\n"),
        )

        result = discover_reconciliation_claim_candidates((CONTROL_SOURCE,), reader)

        self.assertEqual(result.candidates, ())
        self.assertEqual(len(result.failures), 1)
        self.assertIn("digest", result.failures[0].reason)

    def test_projection_repository_identity_mismatch_fails_closed(self) -> None:
        reader = _Reader(
            _control_document([_publication("reviewer")], repository="other/repo"),
            _task_document(),
        )

        result = discover_reconciliation_claim_candidates((CONTROL_SOURCE,), reader)

        self.assertEqual(result.candidates, ())
        self.assertEqual(len(result.failures), 1)
        self.assertIn("repository", result.failures[0].reason)

    def test_task_entry_identity_mismatch_fails_closed(self) -> None:
        publication = _publication("reviewer")
        publication["entry_ref"] = f"https://github.com/{REPOSITORY}/issues/8"
        publication["publication_id"] = _publication_id(publication)
        reader = _Reader(_control_document([publication]), _task_document())

        result = discover_reconciliation_claim_candidates((CONTROL_SOURCE,), reader)

        self.assertEqual(result.candidates, ())
        self.assertEqual(len(result.failures), 1)
        self.assertIn("entry_ref", result.failures[0].reason)

    def test_unsupported_integrator_role_fails_closed(self) -> None:
        publication = _publication("integrator")
        reader = _Reader(_control_document([publication]), _task_document())

        result = discover_reconciliation_claim_candidates((CONTROL_SOURCE,), reader)

        self.assertEqual(result.candidates, ())
        self.assertEqual(len(result.failures), 1)
        self.assertIn("role", result.failures[0].reason)

    def test_duplicate_task_role_publications_fail_closed(self) -> None:
        first = _publication("reviewer")
        second = dict(first)
        second["scope"] = "changed bounded scope"
        second["publication_id"] = _publication_id(second)
        reader = _Reader(_control_document([first, second]), _task_document())

        result = discover_reconciliation_claim_candidates((CONTROL_SOURCE,), reader)

        self.assertEqual(result.candidates, ())
        self.assertEqual(len(result.failures), 1)
        self.assertIn("task/role", result.failures[0].reason)

    def test_human_confirmation_publication_fails_closed(self) -> None:
        publication = _publication("reviewer")
        publication["requires_user_confirmation"] = True
        reader = _Reader(_control_document([publication]), _task_document())

        result = discover_reconciliation_claim_candidates((CONTROL_SOURCE,), reader)

        self.assertEqual(result.candidates, ())
        self.assertEqual(len(result.failures), 1)
        self.assertIn("confirmation", result.failures[0].reason)

    def test_publication_id_mismatch_fails_closed(self) -> None:
        publication = _publication("reviewer")
        publication["publication_id"] = "sha256:" + "0" * 64
        reader = _Reader(_control_document([publication]), _task_document())

        result = discover_reconciliation_claim_candidates((CONTROL_SOURCE,), reader)

        self.assertEqual(result.candidates, ())
        self.assertEqual(len(result.failures), 1)
        self.assertIn("publication_id", result.failures[0].reason)

    def test_recovery_candidate_is_blocked_by_live_nonreviewer_on_same_task(self) -> None:
        reader = _Reader(_control_document([_publication("recovery")]), _task_document())
        candidate = discover_reconciliation_claim_candidates((CONTROL_SOURCE,), reader).candidates[0]
        active = claim(
            CoordinatorState.empty(),
            task=f"{REPOSITORY}#7",
            role=Role.IMPLEMENTER,
            worker_id="worker-active",
            conflict_keys=(),
            now=datetime(2026, 9, 28, 5, 0, tzinfo=timezone.utc),
            idempotency_key="active-claim",
        ).state

        self.assertEqual(list_claimable((candidate,), active), ())
        with self.assertRaises(ClaimConflict):
            claim(
                active,
                task=candidate.task,
                role=candidate.role,
                worker_id="worker-recovery",
                conflict_keys=candidate.conflict_keys,
                now=datetime(2026, 9, 28, 5, 1, tzinfo=timezone.utc),
                idempotency_key="recovery-race",
            )

    def test_reviewer_candidate_can_coexist_with_recovery_or_implementation_on_same_task(self) -> None:
        reader = _Reader(_control_document([_publication("reviewer")]), _task_document())
        candidate = discover_reconciliation_claim_candidates((CONTROL_SOURCE,), reader).candidates[0]
        active = claim(
            CoordinatorState.empty(),
            task=f"{REPOSITORY}#7",
            role=Role.IMPLEMENTER,
            worker_id="worker-active",
            conflict_keys=(),
            now=datetime(2026, 9, 28, 5, 0, tzinfo=timezone.utc),
            idempotency_key="active-claim",
        ).state

        self.assertEqual(list_claimable((candidate,), active), (candidate,))


if __name__ == "__main__":
    unittest.main()
