from __future__ import annotations

import json
import unittest

from execution_coordinator.model import Role


BEGIN = "<!-- DEVFLOW_EXECUTION_CANDIDATE_V1_BEGIN -->"
END = "<!-- DEVFLOW_EXECUTION_CANDIDATE_V1_END -->"


def _marker(
    *,
    task_ref: str = "owner/repo#7",
    entry_ref: str = "https://github.com/owner/repo/issues/7",
    role: str = "implementer",
    scope_ready: bool = True,
    blocked: bool = False,
    requires_user_confirmation: bool = False,
    conflict_keys: list[str] | None = None,
    control_ref: str = "kinoko34077/devflow#17",
    work_order_ref: str | None = None,
    extra_top_level: dict[str, object] | None = None,
) -> str:
    payload: dict[str, object] = {
        "schema_version": 1,
        "task_ref": task_ref,
        "entry_ref": entry_ref,
        "role": role,
        "scope_ready": scope_ready,
        "blocked": blocked,
        "requires_user_confirmation": requires_user_confirmation,
        "provenance": {"control_ref": control_ref},
    }
    if conflict_keys is not None:
        payload["conflict_keys"] = conflict_keys
    if work_order_ref is not None:
        payload["provenance"]["work_order_ref"] = work_order_ref  # type: ignore[index]
    if extra_top_level:
        payload.update(extra_top_level)
    return f"{BEGIN}\n{json.dumps(payload, ensure_ascii=False)}\n{END}"


def _control_body(
    *,
    repository: str = "owner/repo",
    status: str = "READY_FOR_IMPLEMENTATION",
    next_action: str = "[IMPLEMENT] bounded task",
) -> str:
    return (
        f"## Repository\n\n`{repository}`\n\n"
        f"## Work Status\n\n`{status}`\n\n"
        f"## Next Action\n\n`{next_action}`"
    )


class _Reader:
    def __init__(self, documents: dict[tuple[str, int], object]) -> None:
        self.documents = documents
        self.calls: list[tuple[str, int]] = []

    def read_issue(self, repository: str, issue_number: int):
        key = (repository, issue_number)
        self.calls.append(key)
        return self.documents[key]


class DurableCandidateV1ContractTests(unittest.TestCase):
    def _api(self):
        from execution_coordinator import discovery as api

        return api

    def _doc(
        self,
        api,
        repository: str,
        number: int,
        body: str,
        *,
        title: str = "Task",
        state: str = "open",
        url: str | None = None,
        is_pull_request: bool = False,
    ):
        return api.IssueDocument(
            repository=repository,
            number=number,
            state=state,
            body=body,
            html_url=url or f"https://github.com/{repository}/issues/{number}",
            title=title,
            is_pull_request=is_pull_request,
        )

    def test_marker_is_source_authority_and_local_issue_needs_no_work_status_sections(self) -> None:
        api = self._api()
        source = api.DurableIssueSource(repository="owner/repo", issue_number=7)
        reader = _Reader(
            {
                ("owner/repo", 7): self._doc(
                    api,
                    "owner/repo",
                    7,
                    _marker(conflict_keys=["component:owner/repo:core"]),
                ),
                ("kinoko34077/devflow", 17): self._doc(
                    api,
                    "kinoko34077/devflow",
                    17,
                    _control_body(),
                    title="[REPO] repo",
                ),
            }
        )

        result = api.discover_claim_candidates((source,), reader)

        self.assertEqual(result.failures, ())
        self.assertEqual(len(result.candidates), 1)
        candidate = result.candidates[0]
        self.assertEqual(candidate.task, "owner/repo#7")
        self.assertEqual(candidate.role, Role.IMPLEMENTER)
        self.assertEqual(candidate.entry_ref, "https://github.com/owner/repo/issues/7")
        self.assertEqual(candidate.conflict_keys, ("component:owner/repo:core",))
        self.assertTrue(candidate.scope_ready)
        self.assertFalse(candidate.blocked)
        self.assertFalse(candidate.requires_user_confirmation)

    def test_absent_marker_is_valid_but_not_discoverable(self) -> None:
        api = self._api()
        source = api.DurableIssueSource(repository="owner/repo", issue_number=7)
        reader = _Reader(
            {
                ("owner/repo", 7): self._doc(
                    api, "owner/repo", 7, "## Objective\n\nOrdinary unmarked task."
                )
            }
        )

        result = api.discover_claim_candidates((source,), reader)

        self.assertEqual(result.candidates, ())
        self.assertEqual(result.failures, ())
        self.assertEqual(reader.calls, [("owner/repo", 7)])

    def test_duplicate_marker_unknown_field_and_task_mismatch_fail_closed(self) -> None:
        api = self._api()
        sources = tuple(
            api.DurableIssueSource(repository="owner/repo", issue_number=n)
            for n in (1, 2, 3)
        )
        duplicate = _marker(task_ref="owner/repo#1", entry_ref="https://github.com/owner/repo/issues/1")
        unknown = _marker(
            task_ref="owner/repo#2",
            entry_ref="https://github.com/owner/repo/issues/2",
            extra_top_level={"priority": "P1"},
        )
        mismatch = _marker(
            task_ref="owner/repo#999",
            entry_ref="https://github.com/owner/repo/issues/3",
        )
        reader = _Reader(
            {
                ("owner/repo", 1): self._doc(api, "owner/repo", 1, duplicate + "\n" + duplicate),
                ("owner/repo", 2): self._doc(api, "owner/repo", 2, unknown),
                ("owner/repo", 3): self._doc(api, "owner/repo", 3, mismatch),
            }
        )

        result = api.discover_claim_candidates(sources, reader)

        self.assertEqual(result.candidates, ())
        self.assertEqual(len(result.failures), 3)
        reasons = " ".join(failure.reason for failure in result.failures)
        self.assertIn("marker", reasons.lower())
        self.assertIn("unknown", reasons.lower())
        self.assertIn("task_ref", reasons)

    def test_duplicate_json_keys_fail_closed_at_top_level_and_provenance(self) -> None:
        api = self._api()
        top_level = (
            f"{BEGIN}\n"
            '{"schema_version":1,"task_ref":"owner/repo#1",'
            '"entry_ref":"https://github.com/owner/repo/issues/1",'
            '"role":"reviewer","role":"implementer",'
            '"scope_ready":true,"blocked":false,'
            '"requires_user_confirmation":false,'
            '"provenance":{"control_ref":"kinoko34077/devflow#17"}}'
            f"\n{END}"
        )
        provenance = (
            f"{BEGIN}\n"
            '{"schema_version":1,"task_ref":"owner/repo#2",'
            '"entry_ref":"https://github.com/owner/repo/issues/2",'
            '"role":"implementer","scope_ready":true,"blocked":false,'
            '"requires_user_confirmation":false,'
            '"provenance":{"control_ref":"kinoko34077/devflow#999",'
            '"control_ref":"kinoko34077/devflow#17"}}'
            f"\n{END}"
        )
        sources = (
            api.DurableIssueSource("owner/repo", 1),
            api.DurableIssueSource("owner/repo", 2),
        )
        reader = _Reader(
            {
                ("owner/repo", 1): self._doc(api, "owner/repo", 1, top_level),
                ("owner/repo", 2): self._doc(api, "owner/repo", 2, provenance),
                ("kinoko34077/devflow", 17): self._doc(
                    api,
                    "kinoko34077/devflow",
                    17,
                    _control_body(),
                    title="[REPO] repo",
                ),
            }
        )

        result = api.discover_claim_candidates(sources, reader)

        self.assertEqual(result.candidates, ())
        self.assertEqual(len(result.failures), 2)
        self.assertTrue(all("duplicate" in failure.reason.lower() for failure in result.failures))

    def test_control_status_is_guard_and_implementing_is_not_fresh_discovery(self) -> None:
        api = self._api()
        source = api.DurableIssueSource(repository="owner/repo", issue_number=7)
        reader = _Reader(
            {
                ("owner/repo", 7): self._doc(api, "owner/repo", 7, _marker()),
                ("kinoko34077/devflow", 17): self._doc(
                    api,
                    "kinoko34077/devflow",
                    17,
                    _control_body(status="IMPLEMENTING", next_action="[VERIFY] continue"),
                    title="[REPO] repo",
                ),
            }
        )

        result = api.discover_claim_candidates((source,), reader)

        self.assertEqual(result.candidates, ())
        self.assertEqual(len(result.failures), 1)
        self.assertIn("ordinary", result.failures[0].reason.lower())

    def test_control_user_decision_and_human_gate_veto_false_confirmation_marker(self) -> None:
        api = self._api()
        sources = tuple(
            api.DurableIssueSource(repository="owner/repo", issue_number=n)
            for n in (1, 2)
        )
        reader = _Reader(
            {
                ("owner/repo", 1): self._doc(
                    api,
                    "owner/repo",
                    1,
                    _marker(
                        task_ref="owner/repo#1",
                        entry_ref="https://github.com/owner/repo/issues/1",
                        role="reviewer",
                    ),
                ),
                ("owner/repo", 2): self._doc(
                    api,
                    "owner/repo",
                    2,
                    _marker(
                        task_ref="owner/repo#2",
                        entry_ref="https://github.com/owner/repo/issues/2",
                        role="reviewer",
                    ),
                ),
                ("kinoko34077/devflow", 17): self._doc(
                    api,
                    "kinoko34077/devflow",
                    17,
                    _control_body(status="AWAITING_REVIEW", next_action="[USER_DECISION] choose"),
                    title="[REPO] repo",
                ),
            }
        )

        first = api.discover_claim_candidates((sources[0],), reader)
        self.assertEqual(first.candidates, ())
        self.assertEqual(len(first.failures), 1)
        self.assertIn("confirmation", first.failures[0].reason.lower())

        reader.documents[("kinoko34077/devflow", 17)] = self._doc(
            api,
            "kinoko34077/devflow",
            17,
            _control_body(status="AWAITING_REVIEW", next_action="[HUMAN_GATE] browser judgement"),
            title="[REPO] repo",
        )
        second = api.discover_claim_candidates((sources[1],), reader)
        self.assertEqual(second.candidates, ())
        self.assertEqual(len(second.failures), 1)
        self.assertIn("confirmation", second.failures[0].reason.lower())

    def test_true_confirmation_flag_is_preserved_under_compatible_review_stage(self) -> None:
        api = self._api()
        source = api.DurableIssueSource(repository="owner/repo", issue_number=7)
        reader = _Reader(
            {
                ("owner/repo", 7): self._doc(
                    api,
                    "owner/repo",
                    7,
                    _marker(
                        role="reviewer",
                        requires_user_confirmation=True,
                    ),
                ),
                ("kinoko34077/devflow", 17): self._doc(
                    api,
                    "kinoko34077/devflow",
                    17,
                    _control_body(status="AWAITING_REVIEW", next_action="[HUMAN_GATE] inspect"),
                    title="[REPO] repo",
                ),
            }
        )

        result = api.discover_claim_candidates((source,), reader)

        self.assertEqual(result.failures, ())
        self.assertEqual(len(result.candidates), 1)
        self.assertTrue(result.candidates[0].requires_user_confirmation)

    def test_explicit_same_repository_pull_request_entry_is_allowed(self) -> None:
        api = self._api()
        source = api.DurableIssueSource(repository="owner/repo", issue_number=7)
        reader = _Reader(
            {
                ("owner/repo", 7): self._doc(
                    api,
                    "owner/repo",
                    7,
                    _marker(
                        role="reviewer",
                        entry_ref="https://github.com/owner/repo/pull/8",
                    ),
                ),
                ("owner/repo", 8): self._doc(
                    api,
                    "owner/repo",
                    8,
                    "PR body",
                    title="Review candidate",
                    url="https://github.com/owner/repo/pull/8",
                    is_pull_request=True,
                ),
                ("kinoko34077/devflow", 17): self._doc(
                    api,
                    "kinoko34077/devflow",
                    17,
                    _control_body(status="AWAITING_REVIEW", next_action="[REVIEW] inspect PR"),
                    title="[REPO] repo",
                ),
            }
        )

        result = api.discover_claim_candidates((source,), reader)

        self.assertEqual(result.failures, ())
        self.assertEqual(result.candidates[0].entry_ref, "https://github.com/owner/repo/pull/8")

    def test_control_and_optional_work_order_provenance_are_structural(self) -> None:
        api = self._api()
        source = api.DurableIssueSource(repository="owner/repo", issue_number=7)
        reader = _Reader(
            {
                ("owner/repo", 7): self._doc(
                    api,
                    "owner/repo",
                    7,
                    _marker(work_order_ref="kinoko34077/devflow#105"),
                ),
                ("kinoko34077/devflow", 17): self._doc(
                    api,
                    "kinoko34077/devflow",
                    17,
                    _control_body(),
                    title="[REPO] repo",
                ),
                ("kinoko34077/devflow", 105): self._doc(
                    api,
                    "kinoko34077/devflow",
                    105,
                    "## Objective\n\nCross-repository parent.",
                    title="[WORK ORDER] Parent",
                ),
            }
        )

        result = api.discover_claim_candidates((source,), reader)

        self.assertEqual(result.failures, ())
        self.assertEqual(len(result.candidates), 1)
        self.assertIn(("kinoko34077/devflow", 105), reader.calls)

    def test_malformed_source_does_not_hide_valid_sibling(self) -> None:
        api = self._api()
        bad = api.DurableIssueSource(repository="owner/repo", issue_number=1)
        good = api.DurableIssueSource(repository="owner/repo", issue_number=2)
        reader = _Reader(
            {
                ("owner/repo", 1): self._doc(
                    api,
                    "owner/repo",
                    1,
                    _marker(
                        task_ref="owner/repo#1",
                        entry_ref="https://github.com/other/repo/issues/9",
                    ),
                ),
                ("owner/repo", 2): self._doc(
                    api,
                    "owner/repo",
                    2,
                    _marker(
                        task_ref="owner/repo#2",
                        entry_ref="https://github.com/owner/repo/issues/2",
                    ),
                ),
                ("kinoko34077/devflow", 17): self._doc(
                    api,
                    "kinoko34077/devflow",
                    17,
                    _control_body(),
                    title="[REPO] repo",
                ),
            }
        )

        result = api.discover_claim_candidates((bad, good), reader)

        self.assertEqual(tuple(c.task for c in result.candidates), ("owner/repo#2",))
        self.assertEqual(len(result.failures), 1)
        self.assertIn("entry_ref", result.failures[0].reason)


if __name__ == "__main__":
    unittest.main()
