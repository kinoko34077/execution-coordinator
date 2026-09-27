from __future__ import annotations

import hashlib
import json
import unittest

from execution_coordinator.discovery import (
    DurableIssueSource,
    IssueDocument,
    discover_claim_candidates,
)
from execution_coordinator.model import Role


BEGIN = "<!-- DEVFLOW_EXECUTION_CANDIDATES_V1_BEGIN -->"
END = "<!-- DEVFLOW_EXECUTION_CANDIDATES_V1_END -->"
CONTROL = DurableIssueSource("kinoko34077/devflow", 107)


def _digest(body: str) -> str:
    normalized = body.replace("\r\n", "\n").replace("\r", "\n")
    return "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _envelope(
    *,
    task_number: int,
    task_body: str,
    status: str = "READY_FOR_IMPLEMENTATION",
    roles: list[dict[str, str]] | None = None,
    scope_ready: bool = True,
    blocked: bool = False,
    requires_user_confirmation: bool = False,
    entry_ref: str | None = None,
    work_order_ref: str | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "task": f"owner/repo#{task_number}",
        "task_body_sha256": _digest(task_body),
        "task_work_status": status,
        "entry_ref": entry_ref
        or f"https://github.com/owner/repo/issues/{task_number}",
        "scope_ready": scope_ready,
        "blocked": blocked,
        "requires_user_confirmation": requires_user_confirmation,
        "roles": roles
        or [{"role": "implementer", "next_action_tag": "IMPLEMENT"}],
    }
    if work_order_ref is not None:
        record["work_order_ref"] = work_order_ref
    return record


def _block(
    *,
    candidates: list[dict[str, object]],
    source_ref: str = "kinoko34077/devflow#107",
    repository: str = "owner/repo",
) -> str:
    payload = {
        "schema_version": 1,
        "source_ref": source_ref,
        "repository": repository,
        "candidates": candidates,
    }
    return f"{BEGIN}\n{json.dumps(payload)}\n{END}"


def _control_body(
    *,
    block: str | None = None,
    repository: str = "owner/repo",
    repository_state: str = "ACTIVE",
    next_action: str = "[IMPLEMENT] bounded task",
) -> str:
    machine = f"\n\n{block}" if block else ""
    return (
        f"## Repository\n\n`{repository}`\n\n"
        f"## Repository State\n\n`{repository_state}`\n\n"
        "## Work Status\n\n`AUDITED`\n\n"
        f"## Next Action\n\n`{next_action}`{machine}\n"
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
    is_pull_request: bool = False,
) -> IssueDocument:
    return IssueDocument(
        repository=repository,
        number=number,
        state=state,
        body=body,
        html_url=url or f"https://github.com/{repository}/issues/{number}",
        title=title,
        author_association=author_association,
        is_pull_request=is_pull_request,
    )


class _Reader:
    def __init__(self, documents: dict[tuple[str, int], IssueDocument]) -> None:
        self.documents = documents
        self.calls: list[tuple[str, int]] = []

    def read_issue(self, repository: str, issue_number: int) -> IssueDocument:
        key = (repository, issue_number)
        self.calls.append(key)
        return self.documents[key]


class DurableCandidateControlProjectionTests(unittest.TestCase):
    def _reader(
        self,
        *,
        block: str | None = None,
        task_documents: dict[int, IssueDocument] | None = None,
        control_body: str | None = None,
        control_author_association: str = "MEMBER",
    ) -> _Reader:
        body = control_body or _control_body(block=block)
        documents: dict[tuple[str, int], IssueDocument] = {
            ("kinoko34077/devflow", 107): _doc(
                "kinoko34077/devflow",
                107,
                body,
                title="[REPO] repo",
                author_association=control_author_association,
            )
        }
        for number, document in (task_documents or {}).items():
            documents[("owner/repo", number)] = document
        return _Reader(documents)

    def test_valid_task_envelope_maps_each_role_and_uses_common_task_gates(self) -> None:
        task_body = "## Scope\n\nBounded task."
        candidate = _envelope(
            task_number=7,
            task_body=task_body,
            roles=[
                {"role": "implementer", "next_action_tag": "IMPLEMENT"},
                {"role": "reviewer", "next_action_tag": "REVIEW"},
            ],
        )
        reader = self._reader(
            block=_block(candidates=[candidate]),
            task_documents={7: _doc("owner/repo", 7, task_body)},
        )

        result = discover_claim_candidates((CONTROL,), reader)

        self.assertEqual(result.failures, ())
        self.assertEqual([candidate.role for candidate in result.candidates], [Role.IMPLEMENTER, Role.REVIEWER])
        self.assertTrue(all(candidate.scope_ready for candidate in result.candidates))
        self.assertEqual({candidate.task for candidate in result.candidates}, {"owner/repo#7"})

    def test_block_absence_and_deprecated_issue_marker_are_not_candidate_sources(self) -> None:
        old_marker = (
            "<!-- DEVFLOW_EXECUTION_CANDIDATE_V1_BEGIN -->"
            '{"role":"implementer"}'
            "<!-- DEVFLOW_EXECUTION_CANDIDATE_V1_END -->"
        )
        reader = self._reader(control_body=_control_body() + old_marker)

        result = discover_claim_candidates((CONTROL,), reader)

        self.assertEqual(result.candidates, ())
        self.assertEqual(result.failures, ())

    def test_duplicate_or_unknown_outer_fields_fail_closed_for_the_whole_control(self) -> None:
        candidate = _envelope(task_number=7, task_body="task")
        payload = {
            "schema_version": 1,
            "source_ref": "kinoko34077/devflow#107",
            "repository": "owner/repo",
            "candidates": [candidate],
            "unexpected": True,
        }
        raw = f"{BEGIN}\n{json.dumps(payload)}\n{END}"
        reader = self._reader(
            block=raw,
            task_documents={7: _doc("owner/repo", 7, "task")},
        )

        result = discover_claim_candidates((CONTROL,), reader)

        self.assertEqual(result.candidates, ())
        self.assertEqual(len(result.failures), 1)
        self.assertIn("unknown", result.failures[0].reason)

    def test_duplicate_json_keys_and_duplicate_roles_fail_closed(self) -> None:
        duplicate_json = (
            f"{BEGIN}\n"
            '{"schema_version":1,"source_ref":"kinoko34077/devflow#107",'
            '"repository":"owner/repo","candidates":[],"candidates":[]}'
            f"\n{END}"
        )
        reader = self._reader(control_body=_control_body() + "\n" + duplicate_json)
        result = discover_claim_candidates((CONTROL,), reader)
        self.assertEqual(result.candidates, ())
        self.assertEqual(len(result.failures), 1)
        self.assertIn("duplicate", result.failures[0].reason.lower())

        task_body = "task"
        duplicate_role = _envelope(
            task_number=7,
            task_body=task_body,
            roles=[
                {"role": "implementer", "next_action_tag": "IMPLEMENT"},
                {"role": "implementer", "next_action_tag": "IMPLEMENT"},
            ],
        )
        reader = self._reader(
            block=_block(candidates=[duplicate_role]),
            task_documents={7: _doc("owner/repo", 7, task_body)},
        )
        result = discover_claim_candidates((CONTROL,), reader)
        self.assertEqual(result.candidates, ())
        self.assertIn("role", result.failures[0].reason.lower())

    def test_task_body_digest_mismatch_fails_closed(self) -> None:
        candidate = _envelope(task_number=7, task_body="published body")
        reader = self._reader(
            block=_block(candidates=[candidate]),
            task_documents={7: _doc("owner/repo", 7, "changed body")},
        )

        result = discover_claim_candidates((CONTROL,), reader)

        self.assertEqual(result.candidates, ())
        self.assertIn("digest", result.failures[0].reason.lower())

    def test_untrusted_control_and_task_authors_fail_closed(self) -> None:
        candidate = _envelope(task_number=7, task_body="task")
        reader = self._reader(
            block=_block(candidates=[candidate]),
            task_documents={7: _doc("owner/repo", 7, "task", author_association="NONE")},
            control_author_association="NONE",
        )
        result = discover_claim_candidates((CONTROL,), reader)
        self.assertEqual(result.candidates, ())
        self.assertIn("trusted", result.failures[0].reason.lower())

        reader = self._reader(
            block=_block(candidates=[candidate]),
            task_documents={7: _doc("owner/repo", 7, "task", author_association="NONE")},
        )
        result = discover_claim_candidates((CONTROL,), reader)
        self.assertEqual(result.candidates, ())
        self.assertIn("trusted", result.failures[0].reason.lower())

    def test_control_repository_state_and_current_human_gate_veto_candidates(self) -> None:
        task_body = "task"
        candidate = _envelope(task_number=7, task_body=task_body)
        for state, action in (("PAUSED", "[IMPLEMENT] bounded"), ("ACTIVE", "[USER_DECISION] choose")):
            reader = self._reader(
                block=_block(candidates=[candidate]),
                task_documents={7: _doc("owner/repo", 7, task_body)},
                control_body=_control_body(
                    block=_block(candidates=[candidate]),
                    repository_state=state,
                    next_action=action,
                ),
            )
            result = discover_claim_candidates((CONTROL,), reader)
            self.assertEqual(result.candidates, ())
            self.assertEqual(len(result.failures), 1)

    def test_role_status_action_matrix_is_enforced_without_flattening_control_status(self) -> None:
        task_body = "task"
        cases = [
            ("READY_FOR_IMPLEMENTATION", "implementer", "IMPLEMENT", True),
            ("AWAITING_REVIEW", "reviewer", "REVIEW", True),
            ("AWAITING_REVIEW", "verifier", "VERIFY", True),
            ("AWAITING_REVIEW", "integrator", "MERGE", True),
            ("IMPLEMENTING", "implementer", "IMPLEMENT", False),
            ("AWAITING_REVIEW", "implementer", "IMPLEMENT", False),
            ("READY_FOR_IMPLEMENTATION", "reviewer", "REVIEW", False),
        ]
        for status, role, action, valid in cases:
            with self.subTest(status=status, role=role, action=action):
                candidate = _envelope(
                    task_number=7,
                    task_body=task_body,
                    status=status,
                    roles=[{"role": role, "next_action_tag": action}],
                )
                reader = self._reader(
                    block=_block(candidates=[candidate]),
                    task_documents={7: _doc("owner/repo", 7, task_body)},
                )
                result = discover_claim_candidates((CONTROL,), reader)
                self.assertEqual(bool(result.candidates), valid)
                if not valid:
                    self.assertEqual(len(result.failures), 1)

    def test_work_order_provenance_is_structural_and_trusted(self) -> None:
        task_body = "task"
        candidate = _envelope(
            task_number=7,
            task_body=task_body,
            work_order_ref="kinoko34077/devflow#105",
        )
        reader = self._reader(
            block=_block(candidates=[candidate]),
            task_documents={7: _doc("owner/repo", 7, task_body)},
        )
        reader.documents[("kinoko34077/devflow", 105)] = _doc(
            "kinoko34077/devflow",
            105,
            "## Objective\n\nParent.",
            title="[WORK ORDER] Parent",
        )

        result = discover_claim_candidates((CONTROL,), reader)
        self.assertEqual(result.failures, ())
        self.assertEqual(len(result.candidates), 1)

        reader.documents[("kinoko34077/devflow", 105)] = _doc(
            "kinoko34077/devflow",
            105,
            "## Objective\n\nParent.",
            title="[WORK ORDER] Parent",
            author_association="NONE",
        )
        result = discover_claim_candidates((CONTROL,), reader)
        self.assertEqual(result.candidates, ())
        self.assertIn("trusted", result.failures[0].reason.lower())

    def test_invalid_entry_ref_and_task_mismatch_fail_closed(self) -> None:
        task_body = "task"
        candidate = _envelope(
            task_number=7,
            task_body=task_body,
            entry_ref="https://github.com/other/repo/issues/7",
        )
        reader = self._reader(
            block=_block(candidates=[candidate]),
            task_documents={7: _doc("owner/repo", 7, task_body)},
        )
        result = discover_claim_candidates((CONTROL,), reader)
        self.assertEqual(result.candidates, ())
        self.assertIn("entry_ref", result.failures[0].reason)

    def test_multiple_distinct_task_envelopes_are_emitted_and_invalid_sibling_invalidates_source(self) -> None:
        body7, body8 = "task 7", "task 8"
        candidates = [
            _envelope(task_number=7, task_body=body7),
            _envelope(task_number=8, task_body=body8),
        ]
        reader = self._reader(
            block=_block(candidates=candidates),
            task_documents={
                7: _doc("owner/repo", 7, body7),
                8: _doc("owner/repo", 8, body8),
            },
        )
        result = discover_claim_candidates((CONTROL,), reader)
        self.assertEqual(result.failures, ())
        self.assertEqual({candidate.task for candidate in result.candidates}, {"owner/repo#7", "owner/repo#8"})

        bad = _envelope(task_number=8, task_body="wrong")
        reader = self._reader(
            block=_block(candidates=[candidates[0], bad]),
            task_documents={
                7: _doc("owner/repo", 7, body7),
                8: _doc("owner/repo", 8, body8),
            },
        )
        result = discover_claim_candidates((CONTROL,), reader)
        self.assertEqual(result.candidates, ())
        self.assertEqual(len(result.failures), 1)


if __name__ == "__main__":
    unittest.main()
