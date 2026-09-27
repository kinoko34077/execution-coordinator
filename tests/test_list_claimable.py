from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

import execution_coordinator.query as query
from execution_coordinator.model import Claim, CoordinatorState, ExecutionState, Role


NOW = datetime(2026, 9, 27, 5, 20, tzinfo=timezone.utc)


def _symbols():
    candidate_type = getattr(query, "ClaimCandidate", None)
    list_claimable = getattr(query, "list_claimable", None)
    return candidate_type, list_claimable


def _claim(
    *,
    claim_id: str,
    task: str,
    role: Role,
    worker_id: str = "worker-active",
    conflict_keys: tuple[str, ...] = (),
) -> Claim:
    return Claim(
        claim_id=claim_id,
        generation=1,
        task=task,
        role=role,
        worker_id=worker_id,
        conflict_keys=conflict_keys,
        claimed_at=NOW,
        heartbeat_at=NOW,
        last_progress_at=NOW,
        lease_until=NOW + timedelta(minutes=15),
        state=ExecutionState.RUNNING,
    )


class ListClaimableTests(unittest.TestCase):
    def _api(self):
        candidate_type, list_claimable = _symbols()
        self.assertIsNotNone(candidate_type, "ClaimCandidate must exist")
        self.assertIsNotNone(list_claimable, "list_claimable must exist")
        return candidate_type, list_claimable

    def test_ready_candidates_are_returned_in_input_order(self) -> None:
        Candidate, list_claimable = self._api()
        first = Candidate(
            task="kinoko34077/example#1",
            role=Role.IMPLEMENTER,
            conflict_keys=("component:kinoko34077/example:core",),
            entry_ref="issue:1",
        )
        second = Candidate(
            task="kinoko34077/example#2",
            role=Role.REVIEWER,
            conflict_keys=("component:kinoko34077/example:docs",),
            entry_ref="issue:2",
        )

        result = list_claimable((first, second), CoordinatorState.empty())

        self.assertEqual(result, (first, second))

    def test_explicit_durable_eligibility_boundaries_are_filtered(self) -> None:
        Candidate, list_claimable = self._api()
        candidates = (
            Candidate(
                task="kinoko34077/example#scope",
                role=Role.IMPLEMENTER,
                entry_ref="issue:1",
                scope_ready=False,
            ),
            Candidate(
                task="kinoko34077/example#blocked",
                role=Role.IMPLEMENTER,
                entry_ref="issue:2",
                blocked=True,
            ),
            Candidate(
                task="kinoko34077/example#user",
                role=Role.IMPLEMENTER,
                entry_ref="issue:3",
                requires_user_confirmation=True,
            ),
            Candidate(
                task="kinoko34077/example#entry",
                role=Role.IMPLEMENTER,
                entry_ref=None,
            ),
        )

        self.assertEqual(list_claimable(candidates, CoordinatorState.empty()), ())

    def test_current_same_task_role_claim_filters_candidate(self) -> None:
        Candidate, list_claimable = self._api()
        candidate = Candidate(
            task="kinoko34077/example#1",
            role=Role.IMPLEMENTER,
            entry_ref="issue:1",
        )
        active = _claim(
            claim_id="clm_existing",
            task=candidate.task,
            role=Role.IMPLEMENTER,
        )
        state = CoordinatorState(
            claims={active.claim_id: active},
            generations={f"{active.task}|{active.role.value}": 1},
        )

        self.assertEqual(list_claimable((candidate,), state), ())

    def test_conflict_keys_filter_incompatible_roles_but_allow_reviewer_overlap(self) -> None:
        Candidate, list_claimable = self._api()
        conflict_key = "component:kinoko34077/example:core"
        active = _claim(
            claim_id="clm_active",
            task="kinoko34077/example#active",
            role=Role.IMPLEMENTER,
            conflict_keys=(conflict_key,),
        )
        state = CoordinatorState(claims={active.claim_id: active})
        implementer = Candidate(
            task="kinoko34077/example#2",
            role=Role.IMPLEMENTER,
            conflict_keys=(conflict_key,),
            entry_ref="issue:2",
        )
        reviewer = Candidate(
            task="kinoko34077/example#3",
            role=Role.REVIEWER,
            conflict_keys=(conflict_key,),
            entry_ref="issue:3",
        )

        self.assertEqual(list_claimable((implementer, reviewer), state), (reviewer,))

    def test_same_worker_implementer_reviewer_conflict_is_filtered(self) -> None:
        Candidate, list_claimable = self._api()
        active = _claim(
            claim_id="clm_impl",
            task="kinoko34077/example#1",
            role=Role.IMPLEMENTER,
            worker_id="worker-1",
        )
        state = CoordinatorState(claims={active.claim_id: active})
        candidate = Candidate(
            task=active.task,
            role=Role.REVIEWER,
            entry_ref="issue:1",
        )

        self.assertEqual(
            list_claimable((candidate,), state, worker_id="worker-1"),
            (),
        )
        self.assertEqual(
            list_claimable((candidate,), state, worker_id="worker-2"),
            (candidate,),
        )

    def test_projection_does_not_mutate_candidates_or_state(self) -> None:
        Candidate, list_claimable = self._api()
        candidate = Candidate(
            task="kinoko34077/example#1",
            role=Role.IMPLEMENTER,
            conflict_keys=("component:kinoko34077/example:core",),
            entry_ref="issue:1",
        )
        candidates = [candidate]
        state = CoordinatorState.empty()
        before_claims = dict(state.claims)
        before_generations = dict(state.generations)
        before_idempotency = dict(state.idempotency)

        result = list_claimable(candidates, state)

        self.assertEqual(result, (candidate,))
        self.assertEqual(candidates, [candidate])
        self.assertEqual(state.claims, before_claims)
        self.assertEqual(state.generations, before_generations)
        self.assertEqual(state.idempotency, before_idempotency)


if __name__ == "__main__":
    unittest.main()
