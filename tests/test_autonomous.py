from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timezone

from execution_coordinator.autonomous import (
    AutonomousCycleKeys,
    CycleStatus,
    run_autonomous_cycle,
)
from execution_coordinator.agent import AgentSession
from execution_coordinator.capability import (
    CAPABILITY_SCHEMA_VERSION,
    CandidateRequirements,
    CapabilityMatch,
    CapabilityMatchResult,
)
from execution_coordinator.engine import CoordinationError
from execution_coordinator.model import CoordinatorState, MutationResult, Role
from execution_coordinator.mutate import apply_mutation
from execution_coordinator.query import ClaimCandidate
from execution_coordinator.ranking import candidate_fingerprint
from execution_coordinator.snapshot import render_issue_body


UTC = timezone.utc
NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


class _Store:
    def __init__(self) -> None:
        self.body = render_issue_body("# state\n", CoordinatorState.empty())

    def load_body(self) -> str:
        return self.body

    def save_body(self, body: str) -> None:
        self.body = body

    def add_comment(self, _body: str) -> None:
        pass


class _Gateway:
    def __init__(self) -> None:
        self.store = _Store()
        self.calls: list[tuple[str, dict[str, object], str]] = []

    def mutate(
        self,
        *,
        operation: str,
        payload: dict[str, object],
        idempotency_key: str,
    ) -> MutationResult:
        self.calls.append((operation, dict(payload), idempotency_key))
        return apply_mutation(
            self.store,
            operation=operation,
            payload=payload,
            idempotency_key=idempotency_key,
            now=NOW,
        )


def _candidate(issue_number: int) -> ClaimCandidate:
    return ClaimCandidate(
        task=f"owner/repo#{issue_number}",
        role=Role.IMPLEMENTER,
        entry_ref=f"https://github.com/owner/repo/issues/{issue_number}",
    )


def _match(candidate: ClaimCandidate, worker_id: str = "worker-a") -> CapabilityMatch:
    requirements = CandidateRequirements(
        schema_version=CAPABILITY_SCHEMA_VERSION,
        source_ref="kinoko34077/devflow#107",
        task=candidate.task,
        role=candidate.role,
        candidate_fingerprint=candidate_fingerprint(candidate),
        required_capabilities=frozenset(),
        required_environment=frozenset(),
        observed_at=NOW,
        fresh_until=datetime(2026, 9, 28, 13, 0, tzinfo=UTC),
    )
    return CapabilityMatch(
        candidate=candidate,
        requirements=requirements,
        worker_id=worker_id,
    )


def _frontier(*matches: CapabilityMatch, worker_id: str = "worker-a") -> CapabilityMatchResult:
    return CapabilityMatchResult(
        worker_id=worker_id,
        matches=matches,
        recovery_candidates=(),
        omissions=(),
        ranking_omissions=(),
        source_failures=(),
        discovery_failures=(),
    )


KEYS = AutonomousCycleKeys(
    claim_idempotency_key="cycle-claim",
    acknowledge_idempotency_key="cycle-ack",
    release_idempotency_key="cycle-release",
)


class AutonomousCycleTests(unittest.TestCase):
    def test_selects_first_match_once_and_runs_only_after_acknowledge(self) -> None:
        first = _match(_candidate(1))
        second = _match(_candidate(2))
        gateway = _Gateway()
        observed: list[tuple[str, str | None]] = []

        result = run_autonomous_cycle(
            _frontier(first, second),
            gateway,
            keys=KEYS,
            work=lambda session: observed.append(
                ("work", session.claim_id)
            ) or "done",
        )

        self.assertEqual(CycleStatus.COMPLETED, result.status)
        self.assertIs(result.selected, first)
        self.assertEqual("done", result.work_result)
        self.assertEqual(1, result.claim_attempts)
        self.assertIsNotNone(result.claim_id)
        self.assertEqual(["claim", "acknowledge", "release"], [item[0] for item in gateway.calls])
        self.assertEqual([("work", result.claim_id)], observed)

    def test_rejected_claim_ends_cycle_without_retrying_second_match(self) -> None:
        first = _match(_candidate(1))
        second = _match(_candidate(2))
        gateway = _Gateway()
        owner_session = AgentSession(
            gateway,
            task=first.candidate.task,
            role=first.candidate.role,
            worker_id="other-worker",
        )
        owner_session.claim(idempotency_key="owner-claim")
        work_called: list[bool] = []

        result = run_autonomous_cycle(
            _frontier(first, second),
            gateway,
            keys=KEYS,
            work=lambda _session: work_called.append(True),
        )

        self.assertEqual(CycleStatus.CLAIM_REJECTED, result.status)
        self.assertIs(result.selected, first)
        self.assertEqual(1, result.claim_attempts)
        self.assertIn("ClaimConflict", result.rejection_reason or "")
        self.assertEqual(["claim"], [item[0] for item in gateway.calls[1:]])
        self.assertEqual([], work_called)

    def test_empty_frontier_does_not_mutate(self) -> None:
        gateway = _Gateway()
        result = run_autonomous_cycle(
            _frontier(),
            gateway,
            keys=KEYS,
            work=lambda _session: self.fail("work must not run"),
        )

        self.assertEqual(CycleStatus.NO_CANDIDATE, result.status)
        self.assertEqual(0, result.claim_attempts)
        self.assertEqual([], gateway.calls)

    def test_acknowledge_failure_never_starts_work_or_reports_success(self) -> None:
        class FailingAcknowledgeGateway(_Gateway):
            def mutate(
                self,
                *,
                operation: str,
                payload: dict[str, object],
                idempotency_key: str,
            ) -> MutationResult:
                if operation == "acknowledge":
                    self.calls.append((operation, dict(payload), idempotency_key))
                    raise CoordinationError("acknowledge rejected")
                return super().mutate(
                    operation=operation,
                    payload=payload,
                    idempotency_key=idempotency_key,
                )

        gateway = FailingAcknowledgeGateway()
        work_called: list[bool] = []

        with self.assertRaisesRegex(CoordinationError, "acknowledge rejected"):
            run_autonomous_cycle(
                _frontier(_match(_candidate(1))),
                gateway,
                keys=KEYS,
                work=lambda _session: work_called.append(True),
            )

        self.assertEqual(["claim", "acknowledge"], [item[0] for item in gateway.calls])
        self.assertEqual([], work_called)

    def test_work_failure_releases_claim_and_never_reports_success(self) -> None:
        gateway = _Gateway()

        with self.assertRaisesRegex(RuntimeError, "work failed"):
            run_autonomous_cycle(
                _frontier(_match(_candidate(1))),
                gateway,
                keys=KEYS,
                work=lambda _session: (_ for _ in ()).throw(
                    RuntimeError("work failed")
                ),
            )

        self.assertEqual(
            ["claim", "acknowledge", "release"],
            [item[0] for item in gateway.calls],
        )

    def test_mismatched_worker_and_duplicate_keys_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "worker"):
            run_autonomous_cycle(
                _frontier(_match(_candidate(1), worker_id="worker-b")),
                _Gateway(),
                keys=KEYS,
                work=lambda _session: None,
            )

        inconsistent = _match(_candidate(2))
        inconsistent = CapabilityMatch(
            candidate=inconsistent.candidate,
            requirements=replace(
                inconsistent.requirements,
                task="owner/repo#3",
            ),
            worker_id=inconsistent.worker_id,
        )
        with self.assertRaisesRegex(ValueError, "inconsistent"):
            run_autonomous_cycle(
                _frontier(inconsistent),
                _Gateway(),
                keys=KEYS,
                work=lambda _session: None,
            )

        with self.assertRaisesRegex(ValueError, "distinct"):
            AutonomousCycleKeys(
                claim_idempotency_key="same",
                acknowledge_idempotency_key="same",
                release_idempotency_key="release",
            )


if __name__ == "__main__":
    unittest.main()
