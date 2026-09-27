from __future__ import annotations

import json
import unittest

from execution_coordinator import discovery
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
    control_ref: str = "kinoko34077/devflow#107",
    work_order_ref: str | None = "kinoko34077/devflow#105",
    extra: dict[str, object] | None = None,
    schema_version: int = 1,
) -> str:
    payload: dict[str, object] = {
        "schema_version": schema_version,
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
    if extra:
        payload.update(extra)
    return f"{BEGIN}\n{json.dumps(payload, indent=2)}\n{END}"


def _body(
    marker: str | None,
    *,
    status: str = "READY_FOR_IMPLEMENTATION",
    next_action: str = "`[IMPLEMENT] proceed`",
    extra: str = "",
) -> str:
    parts = [
        f"## Work Status\n\n`{status}`",
        "## Objective\n\nDurable source fixture.",
        "## Scope\n\nBounded scope.",
        "## Acceptance criteria\n\n- [ ] Verified.",
        f"## Next Action\n\n{next_action}",
    ]
    if marker is not None:
        parts.append(marker)
    if extra:
        parts.append(extra)
    return "\n\n".join(parts)


class _Reader:
    def __init__(self, docs: dict[tuple[str, int], object]) -> None:
        self.docs = docs
        self.calls: list[tuple[str, int]] = []

    def read_issue(self, repository: str, issue_number: int):
        self.calls.append((repository, issue_number))
        return self.docs[(repository, issue_number)]


def _source_document(
    *,
    marker: str | None,
    status: str = "READY_FOR_IMPLEMENTATION",
    next_action: str = "`[IMPLEMENT] proceed`",
    extra: str = "",
    number: int = 7,
    repository: str = "owner/repo",
    state: str = "open",
    is_pull_request: bool = False,
):
    return discovery.IssueDocument(
        repository=repository,
        number=number,
        state=state,
        body=_body(marker, status=status, next_action=next_action, extra=extra),
        html_url=f"https://github.com/{repository}/issues/{number}",
        is_pull_request=is_pull_request,
        title="Durable task",
    )


def _control_document(*, state: str = "open", repository: str = "owner/repo"):
    return discovery.IssueDocument(
        repository="kinoko34077/devflow",
        number=107,
        state=state,
        body=f"## Repository\n\n`{repository}`\n\n## Work Status\n\n`AUDITED`",
        html_url="https://github.com/kinoko34077/devflow/issues/107",
        title=f"[REPO] {repository}",
    )


def _work_order_document(*, state: str = "open", title: str = "[WORK ORDER] Parent"):
    return discovery.IssueDocument(
        repository="kinoko34077/devflow",
        number=105,
        state=state,
        body="## Work Status\n\n`IMPLEMENTING`",
        html_url="https://github.com/kinoko34077/devflow/issues/105",
        title=title,
    )


def _reader_for_source(document) -> _Reader:
    return _Reader(
        {
            ("owner/repo", 7): document,
            ("kinoko34077/devflow", 107): _control_document(),
            ("kinoko34077/devflow", 105): _work_order_document(),
        }
    )


class DurableMarkerContractTests(unittest.TestCase):
    def test_marker_absence_is_valid_but_not_discoverable(self) -> None:
        source = discovery.DurableIssueSource("owner/repo", 7)
        reader = _reader_for_source(_source_document(marker=None))

        result = discovery.discover_claim_candidates((source,), reader)

        self.assertEqual(result.candidates, ())
        self.assertEqual(result.failures, ())
        self.assertEqual(reader.calls, [("owner/repo", 7)])

    def test_valid_marker_is_source_authority_for_exact_candidate_fields(self) -> None:
        source = discovery.DurableIssueSource("owner/repo", 7)
        marker = _marker(
            role="implementer",
            scope_ready=True,
            blocked=False,
            requires_user_confirmation=False,
            conflict_keys=["component:owner/repo:core"],
        )
        reader = _reader_for_source(_source_document(marker=marker))

        result = discovery.discover_claim_candidates((source,), reader)

        self.assertEqual(result.failures, ())
        self.assertEqual(len(result.candidates), 1)
        candidate = result.candidates[0]
        self.assertEqual(candidate.task, "owner/repo#7")
        self.assertEqual(candidate.entry_ref, "https://github.com/owner/repo/issues/7")
        self.assertEqual(candidate.role, Role.IMPLEMENTER)
        self.assertTrue(candidate.scope_ready)
        self.assertFalse(candidate.blocked)
        self.assertFalse(candidate.requires_user_confirmation)
        self.assertEqual(candidate.conflict_keys, ("component:owner/repo:core",))
        self.assertEqual(
            reader.calls,
            [
                ("owner/repo", 7),
                ("kinoko34077/devflow", 107),
                ("kinoko34077/devflow", 105),
            ],
        )

    def test_role_state_guard_matches_protocol_v1_ordinary_discovery(self) -> None:
        cases = (
            (Role.IMPLEMENTER, "READY_FOR_IMPLEMENTATION", True),
            (Role.IMPLEMENTER, "AWAITING_REVIEW", False),
            (Role.REVIEWER, "AWAITING_REVIEW", True),
            (Role.VERIFIER, "AWAITING_REVIEW", True),
            (Role.INTEGRATOR, "AWAITING_REVIEW", True),
            (Role.REVIEWER, "READY_FOR_IMPLEMENTATION", False),
            (Role.IMPLEMENTER, "IMPLEMENTING", False),
        )
        for role, status, expected in cases:
            with self.subTest(role=role, status=status):
                source = discovery.DurableIssueSource("owner/repo", 7)
                reader = _reader_for_source(
                    _source_document(marker=_marker(role=role.value), status=status)
                )
                result = discovery.discover_claim_candidates((source,), reader)
                self.assertEqual(bool(result.candidates), expected)
                if not expected:
                    self.assertEqual(len(result.failures), 1)
                    self.assertIn("ordinary", result.failures[0].reason.lower())

    def test_compatible_false_blocked_and_user_confirmation_flags_are_preserved(self) -> None:
        variants = (
            ({"scope_ready": False}, (False, False, False)),
            ({"blocked": True}, (True, True, False)),
            ({"requires_user_confirmation": True}, (True, False, True)),
        )
        for changes, expected in variants:
            with self.subTest(changes=changes):
                source = discovery.DurableIssueSource("owner/repo", 7)
                marker = _marker(**changes)
                reader = _reader_for_source(_source_document(marker=marker))
                result = discovery.discover_claim_candidates((source,), reader)
                self.assertEqual(result.failures, ())
                self.assertEqual(len(result.candidates), 1)
                candidate = result.candidates[0]
                self.assertEqual(
                    (candidate.scope_ready, candidate.blocked, candidate.requires_user_confirmation),
                    expected,
                )

    def test_malformed_duplicate_partial_unknown_and_versioned_markers_fail_closed(self) -> None:
        valid = _marker()
        payload = valid.split(BEGIN, 1)[1].split(END, 1)[0]
        variants = (
            valid + "\n" + valid,
            BEGIN + payload,
            END + "\n" + BEGIN + payload,
            f"{BEGIN}\n{{not-json}}\n{END}",
            _marker(extra={"priority": 99}),
            _marker(schema_version=2),
        )
        for body_marker in variants:
            with self.subTest(marker=body_marker[:50]):
                source = discovery.DurableIssueSource("owner/repo", 7)
                reader = _reader_for_source(_source_document(marker=body_marker))
                result = discovery.discover_claim_candidates((source,), reader)
                self.assertEqual(result.candidates, ())
                self.assertEqual(len(result.failures), 1)

    def test_task_entry_role_and_conflict_keys_are_strict(self) -> None:
        variants = (
            _marker(task_ref="owner/repo#8"),
            _marker(entry_ref="http://github.com/owner/repo/issues/7"),
            _marker(entry_ref="https://github.com/other/repo/issues/7"),
            _marker(role="scheduler"),
            _marker(conflict_keys=["dup", "dup"]),
            _marker(conflict_keys=[""]),
        )
        for marker in variants:
            with self.subTest(marker=marker):
                source = discovery.DurableIssueSource("owner/repo", 7)
                reader = _reader_for_source(_source_document(marker=marker))
                result = discovery.discover_claim_candidates((source,), reader)
                self.assertEqual(result.candidates, ())
                self.assertEqual(len(result.failures), 1)

    def test_control_and_optional_work_order_provenance_are_validated(self) -> None:
        bad_controls = (
            _control_document(state="closed"),
            _control_document(repository="other/repo"),
            discovery.IssueDocument(
                repository="kinoko34077/devflow",
                number=107,
                state="open",
                body="## Repository\n\n`owner/repo`",
                html_url="https://github.com/kinoko34077/devflow/issues/107",
                title="not a repository control",
            ),
        )
        for control in bad_controls:
            with self.subTest(control=control):
                source = discovery.DurableIssueSource("owner/repo", 7)
                reader = _Reader(
                    {
                        ("owner/repo", 7): _source_document(marker=_marker()),
                        ("kinoko34077/devflow", 107): control,
                        ("kinoko34077/devflow", 105): _work_order_document(),
                    }
                )
                result = discovery.discover_claim_candidates((source,), reader)
                self.assertEqual(result.candidates, ())
                self.assertEqual(len(result.failures), 1)

        bad_work_order = _work_order_document(title="not a work order")
        reader = _Reader(
            {
                ("owner/repo", 7): _source_document(marker=_marker()),
                ("kinoko34077/devflow", 107): _control_document(),
                ("kinoko34077/devflow", 105): bad_work_order,
            }
        )
        result = discovery.discover_claim_candidates((discovery.DurableIssueSource("owner/repo", 7),), reader)
        self.assertEqual(result.candidates, ())
        self.assertEqual(len(result.failures), 1)

    def test_human_gate_contradictions_fail_closed_but_true_confirmation_is_preserved(self) -> None:
        for gate in ("`[USER_DECISION] choose A or B`", "`[HUMAN_GATE] wait for approval`"):
            with self.subTest(gate=gate):
                source = discovery.DurableIssueSource("owner/repo", 7)
                reader = _reader_for_source(
                    _source_document(marker=_marker(requires_user_confirmation=False), next_action=gate)
                )
                result = discovery.discover_claim_candidates((source,), reader)
                self.assertEqual(result.candidates, ())
                self.assertEqual(len(result.failures), 1)
                self.assertIn("gate", result.failures[0].reason.lower())

        source = discovery.DurableIssueSource("owner/repo", 7)
        reader = _reader_for_source(
            _source_document(
                marker=_marker(requires_user_confirmation=True),
                next_action="`[HUMAN_GATE] wait for approval`",
            )
        )
        result = discovery.discover_claim_candidates((source,), reader)
        self.assertEqual(result.failures, ())
        self.assertTrue(result.candidates[0].requires_user_confirmation)

    def test_blocked_status_contradiction_and_implementing_fresh_discovery_fail_closed(self) -> None:
        source = discovery.DurableIssueSource("owner/repo", 7)
        blocked_reader = _reader_for_source(
            _source_document(marker=_marker(blocked=False), status="BLOCKED")
        )
        blocked = discovery.discover_claim_candidates((source,), blocked_reader)
        self.assertEqual(blocked.candidates, ())
        self.assertEqual(len(blocked.failures), 1)
        self.assertIn("contradict", blocked.failures[0].reason.lower())

        implementing_reader = _reader_for_source(
            _source_document(marker=_marker(), status="IMPLEMENTING")
        )
        implementing = discovery.discover_claim_candidates((source,), implementing_reader)
        self.assertEqual(implementing.candidates, ())
        self.assertEqual(len(implementing.failures), 1)
        self.assertIn("ordinary", implementing.failures[0].reason.lower())

    def test_malformed_sibling_does_not_hide_valid_candidate_and_order_is_preserved(self) -> None:
        source1 = discovery.DurableIssueSource("owner/repo", 7)
        source2 = discovery.DurableIssueSource("owner/repo", 8)
        source3 = discovery.DurableIssueSource("owner/repo", 9)
        reader = _Reader(
            {
                ("owner/repo", 7): _source_document(marker=_marker(task_ref="owner/repo#7"), number=7),
                ("owner/repo", 8): _source_document(marker=f"{BEGIN}\n{{bad}}\n{END}", number=8),
                ("owner/repo", 9): _source_document(
                    marker=_marker(task_ref="owner/repo#9", entry_ref="https://github.com/owner/repo/issues/9"),
                    number=9,
                ),
                ("kinoko34077/devflow", 107): _control_document(),
                ("kinoko34077/devflow", 105): _work_order_document(),
            }
        )

        result = discovery.discover_claim_candidates((source1, source2, source3), reader)

        self.assertEqual(tuple(item.task for item in result.candidates), ("owner/repo#7", "owner/repo#9"))
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.failures[0].source, source2)


if __name__ == "__main__":
    unittest.main()
