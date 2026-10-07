from __future__ import annotations

import unittest
from datetime import timedelta

from execution_coordinator.capability import (
    CAPABILITY_SCHEMA_VERSION,
    CandidateRequirements,
    WorkerProfile,
)
from execution_coordinator.controller_offer import (
    CONTROLLER_OFFER_SCHEMA_VERSION,
    OfferResponseCode,
    evaluate_controller_offer,
    select_controller_offer,
)
from execution_coordinator.model import Role
from execution_coordinator.query import ClaimCandidate
from execution_coordinator.ranking import (
    RANKING_SCHEMA_VERSION,
    ControlPriority,
    RankedCandidate,
    RankedFrontierResult,
    RankingMetadata,
    ReadinessClass,
    candidate_fingerprint,
)
from execution_coordinator.portfolio_runtime import PortfolioRuntimeRead, read_portfolio_runtime
from tests.test_bootstrap_pickup import (
    NOW,
    _Gateway,
    _Reader,
    _Store,
    _portfolio_control,
)


def _profile(*, capabilities=frozenset({"python"}), environment=frozenset({"windows"})):
    return WorkerProfile(
        schema_version=CAPABILITY_SCHEMA_VERSION,
        worker_id="claude-auto-1",
        source_ref="worker:claude-auto-1",
        capabilities=capabilities,
        environment=environment,
        observed_at=NOW - timedelta(minutes=1),
        fresh_until=NOW + timedelta(minutes=30),
    )


def _read(*, store=None, ready_at=None):
    ca, ta, _ = _portfolio_control("owner/a", 201, 8, priority="P1", ready_at=ready_at)
    cb, tb, _ = _portfolio_control("owner/b", 202, 9, priority="P2")
    store = store or _Store()
    return read_portfolio_runtime(
        (ca, cb),
        issue_reader=_Reader(ca, cb, ta, tb),
        state_reader=store,
        worker_id="controller-auto-1",
        now=NOW,
    ), store


def _review_read(*, review_pr_number=12, review_pr_head_sha="a" * 40):
    candidate = ClaimCandidate(
        task="owner/a#8",
        role=Role.REVIEWER,
        entry_ref="https://github.com/owner/a/issues/8",
        review_pr_number=review_pr_number,
        review_pr_head_sha=review_pr_head_sha,
    )
    fingerprint = candidate_fingerprint(candidate)
    metadata = RankingMetadata(
        schema_version=RANKING_SCHEMA_VERSION,
        source_ref="kinoko34077/devflow#201",
        task=candidate.task,
        role=candidate.role,
        candidate_fingerprint=fingerprint,
        control_priority=ControlPriority.P1,
        controller_urgency=None,
        dependency_ready=True,
        dependency_order=8,
        readiness_class=ReadinessClass.REVIEW,
        ready_at=None,
        observed_at=NOW - timedelta(minutes=1),
        fresh_until=NOW + timedelta(minutes=10),
    )
    ranked = RankedFrontierResult(
        ranked=(RankedCandidate(candidate=candidate, metadata=metadata, rank_key=(1,)),),
        recovery_candidates=(),
        omissions=(),
        source_failures=(),
        discovery_failures=(),
    )
    requirements = CandidateRequirements(
        schema_version=CAPABILITY_SCHEMA_VERSION,
        source_ref="kinoko34077/devflow#201",
        task=candidate.task,
        role=candidate.role,
        candidate_fingerprint=fingerprint,
        required_capabilities=frozenset({"repo-checkout"}),
        required_environment=frozenset({"linux"}),
        observed_at=NOW - timedelta(minutes=1),
        fresh_until=NOW + timedelta(minutes=10),
    )
    return PortfolioRuntimeRead(
        observed_at=NOW,
        frontier=None,  # select_controller_offer does not consume frontier evidence.
        ranked=ranked,
        requirements=(requirements,),
        complete=True,
    )


class ControllerOfferTests(unittest.TestCase):
    def test_selects_at_most_one_best_supported_offer_without_mutation(self) -> None:
        read, store = _read()
        before = store.body

        offer = select_controller_offer(read)

        self.assertIsNotNone(offer)
        assert offer is not None
        self.assertEqual(CONTROLLER_OFFER_SCHEMA_VERSION, offer.schema_version)
        self.assertEqual("owner/a#8", offer.task)
        self.assertEqual(Role.IMPLEMENTER, offer.role)
        self.assertEqual(frozenset({"python"}), offer.required_capabilities)
        self.assertEqual(frozenset({"windows"}), offer.required_environment)
        self.assertEqual(before, store.body)
        self.assertIsNone(
            select_controller_offer(read, supported_roles=frozenset({Role.REVIEWER}))
        )

    def test_explicit_reviewer_selection_carries_exact_pr_head_context(self) -> None:
        read = _review_read()

        self.assertIsNone(select_controller_offer(read))
        offer = select_controller_offer(
            read,
            supported_roles=frozenset({Role.REVIEWER}),
        )

        self.assertIsNotNone(offer)
        assert offer is not None
        self.assertEqual(Role.REVIEWER, offer.role)
        self.assertEqual(12, offer.review_pr_number)
        self.assertEqual("a" * 40, offer.review_pr_head_sha)

    def test_reviewer_without_structured_pr_context_is_not_offered(self) -> None:
        read = _review_read(review_pr_number=None, review_pr_head_sha=None)

        self.assertIsNone(
            select_controller_offer(
                read,
                supported_roles=frozenset({Role.REVIEWER}),
            )
        )

    def test_exact_match_accepts_but_never_claims(self) -> None:
        read, store = _read()
        offer = select_controller_offer(read)
        assert offer is not None
        before = store.body

        code, match = evaluate_controller_offer(
            offer,
            _profile(),
            current_read=read,
            provider_ready=True,
            human_gate=False,
        )

        self.assertEqual(OfferResponseCode.ACCEPTED, code)
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(offer.task, match.candidate.task)
        self.assertEqual(before, store.body)

    def test_capability_or_provider_boundary_declines_before_claim(self) -> None:
        read, _ = _read()
        offer = select_controller_offer(read)
        assert offer is not None

        code, match = evaluate_controller_offer(
            offer,
            _profile(capabilities=frozenset()),
            current_read=read,
            provider_ready=True,
            human_gate=False,
        )
        self.assertEqual(OfferResponseCode.DECLINED_CAPABILITY, code)
        self.assertIsNone(match)

        code, match = evaluate_controller_offer(
            offer,
            _profile(),
            current_read=read,
            provider_ready=False,
            human_gate=False,
        )
        self.assertEqual(OfferResponseCode.REQUIRES_USER_AUTHORITY, code)
        self.assertIsNone(match)

        code, match = evaluate_controller_offer(
            offer,
            _profile(),
            current_read=read,
            provider_ready=True,
            human_gate=True,
        )
        self.assertEqual(OfferResponseCode.REQUIRES_USER_AUTHORITY, code)
        self.assertIsNone(match)

    def test_live_conflict_or_dependency_change_cannot_accept_stale_offer(self) -> None:
        initial, _ = _read()
        offer = select_controller_offer(initial)
        assert offer is not None

        conflict_store = _Store()
        _Gateway(conflict_store).mutate(
            operation="claim",
            payload={"task": "owner/a#8", "role": "implementer", "worker_id": "other"},
            idempotency_key="conflict",
        )
        current, _ = _read(store=conflict_store)
        code, match = evaluate_controller_offer(
            offer, _profile(), current_read=current, provider_ready=True, human_gate=False
        )
        self.assertEqual(OfferResponseCode.DECLINED_CONFLICT, code)
        self.assertIsNone(match)

        blocked, _ = _read(ready_at="2026-09-28T16:05:00Z")
        code, match = evaluate_controller_offer(
            offer, _profile(), current_read=blocked, provider_ready=True, human_gate=False
        )
        self.assertEqual(OfferResponseCode.BLOCKED_DEPENDENCY, code)
        self.assertIsNone(match)


if __name__ == "__main__":
    unittest.main()
