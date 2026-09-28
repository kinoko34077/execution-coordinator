from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import unittest

from execution_coordinator.engine import ClaimConflict, claim
from execution_coordinator.model import CoordinatorState, Role
from execution_coordinator.query import ClaimCandidate, list_claimable
from execution_coordinator.snapshot import SnapshotError, state_to_data


NOW = datetime(2026, 9, 28, 5, 0, tzinfo=timezone.utc)
TASK = "owner/repo#7"


class RecoveryAuthorityInvariantTests(unittest.TestCase):
    def _recovery_state(self, *, worker_id: str = "worker-recovery") -> CoordinatorState:
        return claim(
            CoordinatorState.empty(),
            task=TASK,
            role=Role.RECOVERY,
            worker_id=worker_id,
            conflict_keys=(),
            now=NOW,
            idempotency_key="recovery",
        ).state

    def test_snapshot_rejects_recovery_racing_nonreviewer_on_same_task(self) -> None:
        state = claim(
            CoordinatorState.empty(),
            task=TASK,
            role=Role.IMPLEMENTER,
            worker_id="worker-implementer",
            conflict_keys=(),
            now=NOW,
            idempotency_key="implementer",
        ).state
        implementer = next(iter(state.claims.values()))
        recovery = replace(
            implementer,
            claim_id="clm_recovery_fixture",
            role=Role.RECOVERY,
            worker_id="worker-recovery",
        )
        claims = dict(state.claims)
        claims[recovery.claim_id] = recovery
        generations = dict(state.generations)
        generations[f"{TASK}|{Role.RECOVERY.value}"] = recovery.generation
        corrupt = state.with_maps(claims=claims, generations=generations)

        with self.assertRaises(SnapshotError):
            state_to_data(corrupt)

    def test_same_worker_recovery_cannot_claim_independent_reviewer_role(self) -> None:
        state = self._recovery_state(worker_id="worker-shared")

        with self.assertRaises(ClaimConflict):
            claim(
                state,
                task=TASK,
                role=Role.REVIEWER,
                worker_id="worker-shared",
                conflict_keys=(),
                now=NOW,
                idempotency_key="reviewer",
            )

    def test_same_worker_recovery_blocks_reviewer_claimability(self) -> None:
        state = self._recovery_state(worker_id="worker-shared")
        reviewer = ClaimCandidate(
            task=TASK,
            role=Role.REVIEWER,
            entry_ref="https://github.com/owner/repo/issues/7",
        )

        self.assertEqual(list_claimable((reviewer,), state, worker_id="worker-shared"), ())

    def test_snapshot_rejects_same_worker_recovery_and_reviewer(self) -> None:
        state = self._recovery_state(worker_id="worker-shared")
        recovery = next(iter(state.claims.values()))
        reviewer = replace(
            recovery,
            claim_id="clm_reviewer_fixture",
            role=Role.REVIEWER,
        )
        claims = dict(state.claims)
        claims[reviewer.claim_id] = reviewer
        generations = dict(state.generations)
        generations[f"{TASK}|{Role.REVIEWER.value}"] = reviewer.generation
        corrupt = state.with_maps(claims=claims, generations=generations)

        with self.assertRaises(SnapshotError):
            state_to_data(corrupt)


if __name__ == "__main__":
    unittest.main()
