from __future__ import annotations

import unittest
from datetime import timedelta

from execution_coordinator.capability import CAPABILITY_SCHEMA_VERSION, WorkerProfile
from execution_coordinator.controller_offer import (
    CONTROLLER_OFFER_SCHEMA_VERSION,
    OfferResponseCode,
    evaluate_controller_offer,
    select_controller_offer,
)
from execution_coordinator.model import Role
from execution_coordinator.portfolio_runtime import read_portfolio_runtime
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
