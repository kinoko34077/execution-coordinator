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
    lease_until: datetime | None = None,
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
        lease_until=lease_until or NOW + timedelta(minutes=15),
        state=ExecutionState.RUNNING,
    )


class ListClaimableTests(unittest.TestCase):
    def _api(self):
        candidate_type, list_claimable = _symbols()
        self.assertIsNotNone(candidate_type, "ClaimCandidate must exist")
        self.assertIsNotNone(list_claimable, "list_claimable must exist")
        return candidate_type, list_claimable

    def _reason_api(self):
        reason_type = getattr(query, "ClaimabilityReason", None)
        project_claimability = getattr(query, "project_claimability", None)
        self.assertIsNotNone(reason_type, "ClaimabilityReason must exist")
        self.assertIsNotNone(project_claimability, "project_claimability must exist")
        return reason_type, project_claimability

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

    def test_duplicate_task_role_candidates_are_deduplicated_first_wins(self) -> None:
        Candidate, list_claimable = self._api()
        first = Candidate(
            task="kinoko34077/example#1",
            role=Role.IMPLEMENTER,
            entry_ref="issue:first",
            conflict_keys=("component:kinoko34077/example:first",),
        )
        duplicate = Candidate(
            task=first.task,
            role=first.role,
            entry_ref="issue:duplicate",
            conflict_keys=("component:kinoko34077/example:duplicate",),
        )

        result = list_claimable((first, duplicate), CoordinatorState.empty())

        self.assertEqual(result, (first,))

    def test_same_task_different_roles_remain_distinct(self) -> None:
        Candidate, list_claimable = self._api()
        reviewer = Candidate(
            task="kinoko34077/example#1",
            role=Role.REVIEWER,
            entry_ref="issue:review",
        )
        verifier = Candidate(
            task=reviewer.task,
            role=Role.VERIFIER,
            entry_ref="issue:verify",
        )

        result = list_claimable((reviewer, verifier), CoordinatorState.empty())

        self.assertEqual(result, (reviewer, verifier))

    def test_reason_projection_distinguishes_claimable_live_and_expired_unswept(self) -> None:
        Candidate, _ = self._api()
        Reason, project_claimability = self._reason_api()
        claimable = Candidate(
            task="kinoko34077/example#free",
            role=Role.IMPLEMENTER,
            entry_ref="issue:free",
        )
        live_blocked = Candidate(
            task="kinoko34077/example#live",
            role=Role.IMPLEMENTER,
            entry_ref="issue:live",
        )
        expired_blocked = Candidate(
            task="kinoko34077/example#expired",
            role=Role.IMPLEMENTER,
            entry_ref="issue:expired",
        )
        live = _claim(
            claim_id="clm_live",
            task=live_blocked.task,
            role=Role.IMPLEMENTER,
            lease_until=NOW + timedelta(seconds=1),
        )
        expired = _claim(
            claim_id="clm_expired",
            task=expired_blocked.task,
            role=Role.IMPLEMENTER,
            lease_until=NOW - timedelta(seconds=1),
        )
        state = CoordinatorState(claims={live.claim_id: live, expired.claim_id: expired})

        projected = project_claimability(
            (claimable, live_blocked, expired_blocked),
            state,
            now=NOW,
        )

        self.assertEqual(
            tuple(item.reason for item in projected),
            (Reason.CLAIMABLE, Reason.BLOCKED_LIVE, Reason.EXPIRED_UNSWEPT),
        )

    def test_mixed_live_and_expired_blockers_are_reported_as_live(self) -> None:
        Candidate, _ = self._api()
        Reason, project_claimability = self._reason_api()
        key = "component:kinoko34077/example:core"
        candidate = Candidate(
            task="kinoko34077/example#candidate",
            role=Role.IMPLEMENTER,
            entry_ref="issue:candidate",
            conflict_keys=(key,),
        )
        expired = _claim(
            claim_id="clm_expired",
            task="kinoko34077/example#expired",
            role=Role.IMPLEMENTER,
            conflict_keys=(key,),
            lease_until=NOW - timedelta(seconds=1),
        )
        live = _claim(
            claim_id="clm_live",
            task="kinoko34077/example#live",
            role=Role.IMPLEMENTER,
            conflict_keys=(key,),
            lease_until=NOW + timedelta(seconds=1),
        )
        state = CoordinatorState(claims={expired.claim_id: expired, live.claim_id: live})

        projected = project_claimability((candidate,), state, now=NOW)

        self.assertEqual(len(projected), 1)
        self.assertEqual(projected[0].reason, Reason.BLOCKED_LIVE)

    def test_expired_unswept_remains_excluded_from_legacy_list_claimable(self) -> None:
        Candidate, list_claimable = self._api()
        Reason, project_claimability = self._reason_api()
        candidate = Candidate(
            task="kinoko34077/example#expired",
            role=Role.IMPLEMENTER,
            entry_ref="issue:expired",
        )
        expired = _claim(
            claim_id="clm_expired",
            task=candidate.task,
            role=Role.IMPLEMENTER,
            lease_until=NOW - timedelta(seconds=1),
        )
        state = CoordinatorState(claims={expired.claim_id: expired})

        self.assertEqual(list_claimable((candidate,), state), ())
        projected = project_claimability((candidate,), state, now=NOW)
        self.assertEqual(projected[0].reason, Reason.EXPIRED_UNSWEPT)

    def test_reason_projection_preserves_first_eligible_dedupe_and_role_identity(self) -> None:
        Candidate, _ = self._api()
        Reason, project_claimability = self._reason_api()
        first = Candidate(
            task="kinoko34077/example#1",
            role=Role.REVIEWER,
            entry_ref="issue:first",
        )
        duplicate = Candidate(
            task=first.task,
            role=first.role,
            entry_ref="issue:duplicate",
        )
        verifier = Candidate(
            task=first.task,
            role=Role.VERIFIER,
            entry_ref="issue:verify",
        )

        projected = project_claimability((first, duplicate, verifier), CoordinatorState.empty(), now=NOW)

        self.assertEqual(tuple(item.candidate for item in projected), (first, verifier))
        self.assertEqual(tuple(item.reason for item in projected), (Reason.CLAIMABLE, Reason.CLAIMABLE))

    def test_reason_projection_omits_durably_ineligible_candidates(self) -> None:
        Candidate, _ = self._api()
        _, project_claimability = self._reason_api()
        candidates = (
            Candidate(task="kinoko34077/example#scope", role=Role.IMPLEMENTER, entry_ref="issue:1", scope_ready=False),
            Candidate(task="kinoko34077/example#blocked", role=Role.IMPLEMENTER, entry_ref="issue:2", blocked=True),
            Candidate(task="kinoko34077/example#user", role=Role.IMPLEMENTER, entry_ref="issue:3", requires_user_confirmation=True),
            Candidate(task="kinoko34077/example#entry", role=Role.IMPLEMENTER, entry_ref=None),
        )

        self.assertEqual(project_claimability(candidates, CoordinatorState.empty(), now=NOW), ())

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
