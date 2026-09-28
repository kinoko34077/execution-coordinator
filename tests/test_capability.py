from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from execution_coordinator.capability import (
    CAPABILITY_SCHEMA_VERSION,
    CandidateRequirements,
    WorkerProfile,
    match_ranked_frontier,
    match_ranked_frontier_for_workers,
)
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
    RankedFrontierResult,
    RankedCandidate,
    ReadinessClass,
    RankingMetadata,
    candidate_fingerprint,
)


NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
CONTROL = DurableIssueSource("kinoko34077/devflow", 107)
SOURCE_REF = "kinoko34077/devflow#107"


def _candidate(
    issue_number: int,
    *,
    role: Role = Role.IMPLEMENTER,
) -> ClaimCandidate:
    return ClaimCandidate(
        task=f"owner/repo#{issue_number}",
        role=role,
        entry_ref=f"https://github.com/owner/repo/issues/{issue_number}",
    )


def _ranked_candidate(candidate: ClaimCandidate) -> RankedCandidate:
    metadata = RankingMetadata(
        schema_version=RANKING_SCHEMA_VERSION,
        source_ref=SOURCE_REF,
        task=candidate.task,
        role=candidate.role,
        candidate_fingerprint=candidate_fingerprint(candidate),
        control_priority=ControlPriority.P1,
        controller_urgency=1,
        dependency_ready=True,
        dependency_order=1,
        readiness_class=ReadinessClass.IMPLEMENT,
        ready_at=NOW,
        observed_at=NOW - timedelta(minutes=1),
        fresh_until=NOW + timedelta(minutes=10),
    )
    return RankedCandidate(candidate=candidate, metadata=metadata, rank_key=(1,))


def _frontier(
    *candidates: ClaimCandidate,
    recovery: tuple[ClaimCandidate, ...] = (),
    omissions=(),
) -> RankedFrontierResult:
    projections = tuple(
        ClaimabilityProjection(candidate=candidate, reason=ClaimabilityReason.CLAIMABLE)
        for candidate in candidates
    )
    discovery = DiscoveryResult(candidates=candidates + recovery, failures=())
    read = ComposedReadResult(
        discovery=discovery,
        state=StateReadResult(state=CoordinatorState.empty(), source_updated_at=None),
        claimable=candidates + recovery,
        claimability=projections,
    )
    managed = ManagedFrontierResult(sources=(CONTROL,), source_failures=(), read=read)
    return RankedFrontierResult(
        ranked=tuple(_ranked_candidate(candidate) for candidate in candidates),
        recovery_candidates=recovery,
        omissions=tuple(omissions),
        source_failures=managed.source_failures,
        discovery_failures=(),
    )


def _worker(
    worker_id: str = "worker-a",
    *,
    source_ref: str | None = None,
    capabilities=frozenset({"python:3.11", "git"}),
    environment=frozenset({"os:windows", "network:github"}),
    observed_at: datetime = NOW - timedelta(minutes=1),
    fresh_until: datetime = NOW + timedelta(minutes=10),
) -> WorkerProfile:
    return WorkerProfile(
        schema_version=CAPABILITY_SCHEMA_VERSION,
        worker_id=worker_id,
        source_ref=source_ref or f"worker:{worker_id}",
        capabilities=capabilities,
        environment=environment,
        observed_at=observed_at,
        fresh_until=fresh_until,
    )


def _requirements(
    candidate: ClaimCandidate,
    *,
    source_ref: str = SOURCE_REF,
    fingerprint: str | None = None,
    required_capabilities=frozenset({"python:3.11"}),
    required_environment=frozenset({"os:windows"}),
    observed_at: datetime = NOW - timedelta(minutes=1),
    fresh_until: datetime = NOW + timedelta(minutes=10),
) -> CandidateRequirements:
    return CandidateRequirements(
        schema_version=CAPABILITY_SCHEMA_VERSION,
        source_ref=source_ref,
        task=candidate.task,
        role=candidate.role,
        candidate_fingerprint=fingerprint or candidate_fingerprint(candidate),
        required_capabilities=required_capabilities,
        required_environment=required_environment,
        observed_at=observed_at,
        fresh_until=fresh_until,
    )


class CapabilityMatchingTests(unittest.TestCase):
    def test_exact_subset_match_preserves_rank_and_recovery_separation(self) -> None:
        fresh = _candidate(1)
        recovery = _candidate(2, role=Role.RECOVERY)
        ranked = _frontier(fresh, recovery=(recovery,))

        result = match_ranked_frontier(
            ranked,
            _worker(),
            (_requirements(fresh),),
            now=NOW,
        )

        self.assertEqual([item.candidate for item in result.matches], [fresh])
        self.assertEqual(result.recovery_candidates, (recovery,))
        self.assertEqual(result.omissions, ())

    def test_missing_capability_or_environment_fails_closed_for_one_worker(self) -> None:
        candidate = _candidate(1)
        ranked = _frontier(candidate)
        requirements = (_requirements(candidate),)

        result = match_ranked_frontier(
            ranked,
            _worker(capabilities=frozenset({"git"})),
            requirements,
            now=NOW,
        )

        self.assertEqual(result.matches, ())
        self.assertIn("capabilit", result.omissions[0].reason)

        environment_result = match_ranked_frontier(
            ranked,
            _worker(environment=frozenset({"network:github"})),
            requirements,
            now=NOW,
        )
        self.assertEqual(environment_result.matches, ())
        self.assertIn("environment", environment_result.omissions[0].reason)

    def test_unknown_and_malformed_boundary_evidence_is_not_inferred(self) -> None:
        candidate = _candidate(1)
        ranked = _frontier(candidate)
        unknown = _requirements(
            candidate,
            required_capabilities=frozenset({"capability:never-observed"}),
        )

        result = match_ranked_frontier(
            ranked,
            _worker(),
            (unknown,),
            now=NOW,
        )

        self.assertEqual(result.matches, ())
        self.assertIn("capability", result.omissions[0].reason)

        with self.assertRaisesRegex(ValueError, "tag"):
            _worker(capabilities=frozenset({"bad tag"}))
        with self.assertRaisesRegex(ValueError, "tag"):
            _requirements(candidate, required_environment=frozenset({"bad env"}))
        with self.assertRaisesRegex(ValueError, "source_ref"):
            _worker(source_ref="not-a-worker")

    def test_stale_future_and_provenance_mismatches_fail_closed(self) -> None:
        candidate = _candidate(1)
        ranked = _frontier(candidate)

        stale_worker = match_ranked_frontier(
            ranked,
            _worker(fresh_until=NOW - timedelta(seconds=1)),
            (_requirements(candidate),),
            now=NOW,
        )
        self.assertEqual(stale_worker.matches, ())
        self.assertIn("stale", stale_worker.omissions[0].reason)

        future_requirements = match_ranked_frontier(
            ranked,
            _worker(),
            (
                _requirements(
                    candidate,
                    observed_at=NOW + timedelta(minutes=1),
                ),
            ),
            now=NOW,
        )
        self.assertEqual(future_requirements.matches, ())
        self.assertIn("future", future_requirements.omissions[0].reason)

        mismatch = match_ranked_frontier(
            ranked,
            _worker(),
            (
                _requirements(
                    candidate,
                    source_ref="kinoko34077/devflow#999",
                ),
            ),
            now=NOW,
        )
        self.assertEqual(mismatch.matches, ())
        self.assertIn("source", mismatch.omissions[0].reason)

    def test_fingerprint_and_missing_requirement_fail_closed(self) -> None:
        first = _candidate(1)
        missing = _candidate(2)
        ranked = _frontier(first, missing)

        result = match_ranked_frontier(
            ranked,
            _worker(),
            (_requirements(first, fingerprint="sha256:" + "0" * 64),),
            now=NOW,
        )

        self.assertEqual(result.matches, ())
        reasons = {item.task: item.reason for item in result.omissions}
        self.assertIn("fingerprint", reasons[first.task])
        self.assertIn("missing", reasons[missing.task])

    def test_rank_blockers_remain_evidence_and_workers_are_independent(self) -> None:
        candidate = _candidate(1)
        blocker = object()
        ranked = _frontier(candidate, omissions=(blocker,))
        requirements = (_requirements(candidate),)

        results = match_ranked_frontier_for_workers(
            ranked,
            (
                _worker("worker-b", capabilities=frozenset({"git"})),
                _worker("worker-a"),
            ),
            requirements,
            now=NOW,
        )

        self.assertEqual([result.worker_id for result in results], ["worker-a", "worker-b"])
        self.assertEqual([item.candidate for item in results[0].matches], [candidate])
        self.assertEqual(results[1].matches, ())
        self.assertEqual(results[0].ranking_omissions, (blocker,))
        self.assertEqual(results[1].ranking_omissions, (blocker,))


if __name__ == "__main__":
    unittest.main()
