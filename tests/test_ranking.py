from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from execution_coordinator.discovery import DiscoveryResult, DurableIssueSource
from execution_coordinator.frontier import ComposedReadResult
from execution_coordinator.managed_frontier import ManagedFrontierResult
from execution_coordinator.model import CoordinatorState, Role
from execution_coordinator.query import (
    ClaimabilityProjection,
    ClaimabilityReason,
    ClaimCandidate,
    StateReadResult,
)
from execution_coordinator.ranking import (
    RANKING_SCHEMA_VERSION,
    ControlPriority,
    ReadinessClass,
    RankingMetadata,
    candidate_fingerprint,
    rank_managed_frontier,
)


NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
CONTROL = DurableIssueSource("kinoko34077/devflow", 107)


def _candidate(task_number: int, role: Role = Role.IMPLEMENTER) -> ClaimCandidate:
    return ClaimCandidate(
        task=f"owner/repo#{task_number}",
        role=role,
        entry_ref=f"https://github.com/owner/repo/issues/{task_number}",
    )


def _frontier(
    candidates: tuple[ClaimCandidate, ...],
    *,
    reasons: dict[ClaimCandidate, ClaimabilityReason] | None = None,
) -> ManagedFrontierResult:
    reasons = reasons or {candidate: ClaimabilityReason.CLAIMABLE for candidate in candidates}
    discovery = DiscoveryResult(candidates=candidates, failures=())
    projections = tuple(
        ClaimabilityProjection(candidate=candidate, reason=reasons[candidate])
        for candidate in candidates
    )
    claimable = tuple(
        candidate
        for candidate in candidates
        if reasons[candidate] is ClaimabilityReason.CLAIMABLE
    )
    read = ComposedReadResult(
        discovery=discovery,
        state=StateReadResult(state=CoordinatorState.empty(), source_updated_at=None),
        claimable=claimable,
        claimability=projections,
    )
    return ManagedFrontierResult(sources=(CONTROL,), source_failures=(), read=read)


def _metadata(
    candidate: ClaimCandidate,
    *,
    priority: ControlPriority = ControlPriority.P1,
    urgency: int | None = 10,
    dependency_ready: bool = True,
    dependency_order: int | None = 1,
    readiness: ReadinessClass = ReadinessClass.IMPLEMENT,
    ready_at: datetime | None = NOW,
    observed_at: datetime = NOW - timedelta(minutes=1),
    fresh_until: datetime = NOW + timedelta(minutes=10),
    source_ref: str = "kinoko34077/devflow#107",
    fingerprint: str | None = None,
) -> RankingMetadata:
    return RankingMetadata(
        schema_version=RANKING_SCHEMA_VERSION,
        source_ref=source_ref,
        task=candidate.task,
        role=candidate.role,
        candidate_fingerprint=fingerprint or candidate_fingerprint(candidate),
        control_priority=priority,
        controller_urgency=urgency,
        dependency_ready=dependency_ready,
        dependency_order=dependency_order,
        readiness_class=readiness,
        ready_at=ready_at,
        observed_at=observed_at,
        fresh_until=fresh_until,
    )


class RankingTests(unittest.TestCase):
    def test_deterministic_lexicographic_order_and_task_tie_break(self) -> None:
        p1 = _candidate(1)
        p2 = _candidate(2)
        p3 = _candidate(3)
        p4 = _candidate(4)
        frontier = _frontier((p4, p2, p3, p1))

        result = rank_managed_frontier(
            frontier,
            (
                _metadata(p1, priority=ControlPriority.P1, urgency=5, dependency_order=2),
                _metadata(p2, priority=ControlPriority.P0, urgency=1, dependency_order=9),
                _metadata(p3, priority=ControlPriority.P1, urgency=5, dependency_order=1),
                _metadata(p4, priority=ControlPriority.P1, urgency=5, dependency_order=2),
            ),
            now=NOW,
        )

        self.assertEqual(
            [item.candidate.task for item in result.ranked],
            ["owner/repo#2", "owner/repo#3", "owner/repo#1", "owner/repo#4"],
        )
        self.assertEqual(result.omissions, ())

    def test_fresh_and_recovery_tracks_remain_separate(self) -> None:
        fresh = _candidate(1)
        recovery = _candidate(2, Role.RECOVERY)

        result = rank_managed_frontier(
            _frontier((fresh, recovery)),
            (_metadata(fresh),),
            now=NOW,
        )

        self.assertEqual([item.candidate for item in result.ranked], [fresh])
        self.assertEqual(result.recovery_candidates, (recovery,))
        self.assertEqual(result.omissions, ())

    def test_optional_fields_have_explicit_missing_value_order(self) -> None:
        ready = _candidate(1)
        missing_ready_at = _candidate(2)
        missing_urgency = _candidate(3)
        frontier = _frontier((missing_urgency, missing_ready_at, ready))

        result = rank_managed_frontier(
            frontier,
            (
                _metadata(ready, urgency=5, ready_at=NOW - timedelta(minutes=2)),
                _metadata(missing_ready_at, urgency=5, ready_at=None),
                _metadata(missing_urgency, urgency=None, ready_at=NOW - timedelta(minutes=5)),
            ),
            now=NOW,
        )

        self.assertEqual(
            [item.candidate.task for item in result.ranked],
            ["owner/repo#1", "owner/repo#2", "owner/repo#3"],
        )

    def test_hard_filters_preserve_omission_evidence(self) -> None:
        missing = _candidate(1)
        blocked = _candidate(2)
        dependency = _candidate(3)
        stale = _candidate(4)
        untrusted = _candidate(5)
        frontier = _frontier(
            (missing, blocked, dependency, stale, untrusted),
            reasons={
                missing: ClaimabilityReason.CLAIMABLE,
                blocked: ClaimabilityReason.BLOCKED_LIVE,
                dependency: ClaimabilityReason.CLAIMABLE,
                stale: ClaimabilityReason.CLAIMABLE,
                untrusted: ClaimabilityReason.CLAIMABLE,
            },
        )

        result = rank_managed_frontier(
            frontier,
            (
                _metadata(blocked),
                _metadata(dependency, dependency_ready=False, dependency_order=None),
                _metadata(stale, fresh_until=NOW - timedelta(seconds=1)),
                _metadata(untrusted, source_ref="kinoko34077/devflow#999"),
            ),
            now=NOW,
        )

        self.assertEqual(result.ranked, ())
        self.assertEqual(len(result.omissions), 5)
        reasons = {omission.task: omission.reason for omission in result.omissions}
        self.assertIn("ranking metadata is missing", reasons[missing.task])
        self.assertIn("runtime claimability is BLOCKED_LIVE", reasons[blocked.task])
        self.assertIn("dependency", reasons[dependency.task])
        self.assertIn("stale", reasons[stale.task])
        self.assertIn("source", reasons[untrusted.task])

    def test_mismatched_fingerprint_and_duplicate_metadata_fail_closed(self) -> None:
        candidate = _candidate(1)
        frontier = _frontier((candidate,))
        metadata = _metadata(candidate, fingerprint="sha256:" + "0" * 64)

        result = rank_managed_frontier(
            frontier,
            (metadata, metadata),
            now=NOW,
        )

        self.assertEqual(result.ranked, ())
        self.assertEqual(len(result.omissions), 1)
        self.assertIn("duplicate", result.omissions[0].reason)

    def test_metadata_constructor_rejects_noncanonical_or_stale_shape(self) -> None:
        candidate = _candidate(1)
        with self.assertRaisesRegex(ValueError, "source_ref"):
            _metadata(candidate, source_ref="not-a-control")

        with self.assertRaisesRegex(ValueError, "timezone"):
            _metadata(candidate, ready_at=datetime(2026, 9, 28, 12, 0))

        with self.assertRaisesRegex(ValueError, "dependency_order"):
            _metadata(candidate, dependency_ready=True, dependency_order=None)


if __name__ == "__main__":
    unittest.main()
