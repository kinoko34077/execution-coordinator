from __future__ import annotations

import hashlib
import json
import unittest
from datetime import datetime, timedelta, timezone

from execution_coordinator.discovery import DurableIssueSource, IssueDocument
from execution_coordinator.engine import claim
from execution_coordinator.model import CoordinatorState, Role
from execution_coordinator.query import list_claimable
from execution_coordinator.reconciliation import discover_reconciliation_candidates
from execution_coordinator.snapshot import parse_issue_body, render_issue_body


BEGIN = "<!-- DEVFLOW_RECONCILIATION_WORK_V1_BEGIN -->"
END = "<!-- DEVFLOW_RECONCILIATION_WORK_V1_END -->"
CONTROL = DurableIssueSource("kinoko34077/devflow", 107)
NOW = datetime(2026, 9, 28, 7, 30, tzinfo=timezone.utc)


def _digest(body: str) -> str:
    normalized = body.replace("\r\n", "\n").replace("\r", "\n")
    return "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _publication_id(
    *,
    task_ref: str,
    task_digest: str,
    entry_ref: str,
    role: str,
    disposition: str,
    reason_codes: list[str],
    scope: str,
    context: dict[str, object],
) -> str:
    logical_identity = {
        "schema_version": "development-reconciliation-work.v1",
        "source_contract_version": "development-reconciliation.v1",
        "task_ref": task_ref,
        "task_body_sha256": task_digest,
        "entry_ref": entry_ref,
        "role": role,
        "disposition": disposition,
        "reason_codes": sorted(reason_codes),
        "scope": scope,
        "context": context,
    }
    encoded = json.dumps(
        logical_identity,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _publication(
    *,
    task_number: int,
    task_body: str,
    role: str = "reviewer",
    entry_ref: str | None = None,
    requires_user_confirmation: bool = False,
) -> dict[str, object]:
    task_ref = f"owner/repo#{task_number}"
    digest = _digest(task_body)
    entry = entry_ref or f"https://github.com/owner/repo/issues/{task_number}"
    if role == "reviewer":
        disposition = "NEEDS_REVIEWER"
        reasons = ["DIFFERENT_REVIEWER_REQUIRED"]
        context: dict[str, object] = {
            "pr_number": 12,
            "pr_head_sha": "a" * 40,
        }
    elif role == "recovery":
        disposition = "NEEDS_RECOVERY"
        reasons = ["STALE_EXECUTION_SESSION"]
        context = {
            "predecessor_session_id": "session-7",
            "checkpoint": "implementation committed",
            "next_action": "run the repository regression suite",
            "recovery_transition": "RECOVERY_ASSESSMENT",
            "artifact_refs": ["refs/heads/work/7"],
        }
    else:
        disposition = "NEEDS_REVIEWER"
        reasons = ["UNSUPPORTED_ROLE"]
        context = {}
    scope = "bounded role acceptance scope"
    publication = {
        "schema_version": "development-reconciliation-work.v1",
        "task_ref": task_ref,
        "task_body_sha256": digest,
        "entry_ref": entry,
        "role": role,
        "disposition": disposition,
        "reason_codes": reasons,
        "scope": scope,
        "requires_user_confirmation": requires_user_confirmation,
        "observed_at": "2026-09-28T07:20:00Z",
        "freshness": {
            "source_contract_version": "development-reconciliation.v1",
            "task_body_sha256": digest,
        },
        "context": context,
    }
    publication["publication_id"] = _publication_id(
        task_ref=task_ref,
        task_digest=digest,
        entry_ref=entry,
        role=role,
        disposition=disposition,
        reason_codes=reasons,
        scope=scope,
        context=context,
    )
    return publication


def _block(
    publications: list[dict[str, object]],
    *,
    repository: str = "owner/repo",
) -> str:
    payload = {
        "schema_version": "development-reconciliation-work.v1",
        "repository": repository,
        "publications": publications,
    }
    return f"{BEGIN}\n{json.dumps(payload)}\n{END}"


def _control_body(
    block: str,
    *,
    repository: str = "owner/repo",
    repository_state: str = "ACTIVE",
    next_action: str = "[IMPLEMENT] bounded task",
) -> str:
    return (
        f"## Repository\n\n`{repository}`\n\n"
        f"## Repository State\n\n`{repository_state}`\n\n"
        "## Work Status\n\n`AUDITED`\n\n"
        f"## Next Action\n\n`{next_action}`\n\n{block}\n"
    )


def _doc(
    repository: str,
    number: int,
    body: str,
    *,
    title: str = "Task",
    state: str = "open",
    author_association: str = "MEMBER",
    url: str | None = None,
) -> IssueDocument:
    return IssueDocument(
        repository=repository,
        number=number,
        state=state,
        body=body,
        html_url=url or f"https://github.com/{repository}/issues/{number}",
        title=title,
        author_association=author_association,
        is_pull_request=False,
    )


class _Reader:
    def __init__(self, documents: dict[tuple[str, int], IssueDocument]) -> None:
        self.documents = documents
        self.calls: list[tuple[str, int]] = []

    def read_issue(self, repository: str, issue_number: int) -> IssueDocument:
        key = (repository, issue_number)
        self.calls.append(key)
        return self.documents[key]


def _reader(
    publications: list[dict[str, object]],
    task_bodies: dict[int, str],
    *,
    control_repository: str = "owner/repo",
    control_author_association: str = "MEMBER",
    next_action: str = "[IMPLEMENT] bounded task",
) -> _Reader:
    block = _block(publications, repository=control_repository)
    documents: dict[tuple[str, int], IssueDocument] = {
        ("kinoko34077/devflow", 107): _doc(
            "kinoko34077/devflow",
            107,
            _control_body(
                block,
                repository=control_repository,
                next_action=next_action,
            ),
            title="[REPO] repo",
            author_association=control_author_association,
        )
    }
    for number, body in task_bodies.items():
        documents[("owner/repo", number)] = _doc("owner/repo", number, body)
    return _Reader(documents)


class ReconciliationDemandDiscoveryTests(unittest.TestCase):
    def test_reviewer_and_recovery_publications_enter_existing_candidate_surface(self) -> None:
        body7 = "## Scope\n\nReview exact PR head."
        body8 = "## Scope\n\nRecover interrupted implementation."
        reader = _reader(
            [
                _publication(task_number=7, task_body=body7, role="reviewer"),
                _publication(task_number=8, task_body=body8, role="recovery"),
            ],
            {7: body7, 8: body8},
        )

        result = discover_reconciliation_candidates((CONTROL,), reader)

        self.assertEqual(result.failures, ())
        self.assertEqual(
            [(candidate.task, candidate.role.value) for candidate in result.candidates],
            [("owner/repo#7", "reviewer"), ("owner/repo#8", "recovery")],
        )
        self.assertTrue(all(candidate.scope_ready for candidate in result.candidates))
        self.assertTrue(all(not candidate.blocked for candidate in result.candidates))
        self.assertTrue(
            all(not candidate.requires_user_confirmation for candidate in result.candidates)
        )

    def test_stale_owning_task_digest_fails_closed(self) -> None:
        publication = _publication(task_number=7, task_body="published")
        reader = _reader([publication], {7: "changed"})

        result = discover_reconciliation_candidates((CONTROL,), reader)

        self.assertEqual(result.candidates, ())
        self.assertEqual(len(result.failures), 1)
        self.assertIn("digest", result.failures[0].reason.lower())

    def test_control_and_task_identity_or_trust_mismatch_fails_closed(self) -> None:
        task_body = "task"
        publication = _publication(task_number=7, task_body=task_body)

        reader = _reader(
            [publication],
            {7: task_body},
            control_author_association="NONE",
        )
        result = discover_reconciliation_candidates((CONTROL,), reader)
        self.assertEqual(result.candidates, ())
        self.assertIn("trusted", result.failures[0].reason.lower())

        reader = _reader([publication], {7: task_body})
        reader.documents[("owner/repo", 7)] = _doc(
            "owner/repo",
            7,
            task_body,
            author_association="NONE",
        )
        result = discover_reconciliation_candidates((CONTROL,), reader)
        self.assertEqual(result.candidates, ())
        self.assertIn("trusted", result.failures[0].reason.lower())

        mismatched = _publication(
            task_number=7,
            task_body=task_body,
            entry_ref="https://github.com/other/repo/issues/7",
        )
        reader = _reader([mismatched], {7: task_body})
        result = discover_reconciliation_candidates((CONTROL,), reader)
        self.assertEqual(result.candidates, ())
        self.assertIn("entry_ref", result.failures[0].reason)

    def test_human_gate_is_never_promoted_to_claimability(self) -> None:
        task_body = "task"
        publication = _publication(task_number=7, task_body=task_body)

        reader = _reader(
            [publication],
            {7: task_body},
            next_action="[USER_DECISION] choose reviewer",
        )
        result = discover_reconciliation_candidates((CONTROL,), reader)
        self.assertEqual(result.candidates, ())
        self.assertIn("Gate", result.failures[0].reason)

        gated_body = "## Next Action\n\n[HUMAN_GATE] confirm takeover"
        publication = _publication(task_number=7, task_body=gated_body, role="recovery")
        reader = _reader([publication], {7: gated_body})
        result = discover_reconciliation_candidates((CONTROL,), reader)
        self.assertEqual(result.candidates, ())
        self.assertIn("Gate", result.failures[0].reason)

    def test_unsupported_or_duplicate_role_publications_fail_closed(self) -> None:
        task_body = "task"
        integrator = _publication(task_number=7, task_body=task_body, role="integrator")
        reader = _reader([integrator], {7: task_body})
        result = discover_reconciliation_candidates((CONTROL,), reader)
        self.assertEqual(result.candidates, ())
        self.assertIn("unsupported", result.failures[0].reason.lower())

        reviewer = _publication(task_number=7, task_body=task_body, role="reviewer")
        duplicate = dict(reviewer)
        duplicate["publication_id"] = "sha256:" + "f" * 64
        reader = _reader([reviewer, duplicate], {7: task_body})
        result = discover_reconciliation_candidates((CONTROL,), reader)
        self.assertEqual(result.candidates, ())
        self.assertIn("duplicate", result.failures[0].reason.lower())

    def test_malformed_projection_and_publication_identity_fail_closed(self) -> None:
        task_body = "task"
        publication = _publication(task_number=7, task_body=task_body)
        publication["publication_id"] = "sha256:" + "0" * 64
        reader = _reader([publication], {7: task_body})
        result = discover_reconciliation_candidates((CONTROL,), reader)
        self.assertEqual(result.candidates, ())
        self.assertIn("publication_id", result.failures[0].reason)

        publication = _publication(task_number=7, task_body=task_body)
        publication["unexpected"] = True
        reader = _reader([publication], {7: task_body})
        result = discover_reconciliation_candidates((CONTROL,), reader)
        self.assertEqual(result.candidates, ())
        self.assertIn("unknown", result.failures[0].reason.lower())

    def test_role_specific_context_and_freshness_are_validated(self) -> None:
        task_body = "task"
        reviewer = _publication(task_number=7, task_body=task_body)
        reviewer["context"] = {"pr_number": 12, "pr_head_sha": "short"}
        reader = _reader([reviewer], {7: task_body})
        result = discover_reconciliation_candidates((CONTROL,), reader)
        self.assertEqual(result.candidates, ())
        self.assertIn("pr_head_sha", result.failures[0].reason)

        recovery = _publication(task_number=7, task_body=task_body, role="recovery")
        recovery["freshness"] = {
            "source_contract_version": "development-reconciliation.v0",
            "task_body_sha256": recovery["task_body_sha256"],
        }
        reader = _reader([recovery], {7: task_body})
        result = discover_reconciliation_candidates((CONTROL,), reader)
        self.assertEqual(result.candidates, ())
        self.assertIn("source contract", result.failures[0].reason.lower())

    def test_recovery_candidate_uses_existing_runtime_claim_and_snapshot_authority(self) -> None:
        task_body = "task"
        publication = _publication(task_number=7, task_body=task_body, role="recovery")
        reader = _reader([publication], {7: task_body})
        result = discover_reconciliation_candidates((CONTROL,), reader)
        self.assertEqual(result.failures, ())
        self.assertEqual(len(result.candidates), 1)
        candidate = result.candidates[0]

        runtime_role = Role("recovery")
        claimed = claim(
            CoordinatorState.empty(),
            task=candidate.task,
            role=runtime_role,
            worker_id="worker-a",
            conflict_keys=candidate.conflict_keys,
            now=NOW,
            idempotency_key="recovery-claim-1",
        )
        self.assertEqual(
            list_claimable((candidate,), claimed.state, worker_id="worker-b"),
            (),
        )

        body = render_issue_body("", claimed.state)
        restored = parse_issue_body(body)
        restored_claim = next(iter(restored.claims.values()))
        self.assertEqual(restored_claim.role.value, "recovery")
        self.assertEqual(restored_claim.lease_until, NOW + timedelta(minutes=15))


if __name__ == "__main__":
    unittest.main()
