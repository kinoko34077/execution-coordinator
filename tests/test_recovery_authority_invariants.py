from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import unittest

from execution_coordinator.engine import claim
from execution_coordinator.model import CoordinatorState, Role
from execution_coordinator.snapshot import SnapshotError, state_to_data


class RecoveryAuthorityInvariantTests(unittest.TestCase):
    def test_snapshot_rejects_recovery_racing_nonreviewer_on_same_task(self) -> None:
        task = "owner/repo#7"
        state = claim(
            CoordinatorState.empty(),
            task=task,
            role=Role.IMPLEMENTER,
            worker_id="worker-implementer",
            conflict_keys=(),
            now=datetime(2026, 9, 28, 5, 0, tzinfo=timezone.utc),
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
        generations[f"{task}|{Role.RECOVERY.value}"] = recovery.generation
        corrupt = state.with_maps(claims=claims, generations=generations)

        with self.assertRaises(SnapshotError):
            state_to_data(corrupt)


if __name__ == "__main__":
    unittest.main()
