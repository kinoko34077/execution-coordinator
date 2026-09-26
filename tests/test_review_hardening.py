from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from execution_coordinator.engine import ClaimConflict, claim, release
from execution_coordinator.model import CoordinatorState, Role


UTC = timezone.utc
T0 = datetime(2026, 9, 27, 3, 0, tzinfo=UTC)


class ReviewHardeningTests(unittest.TestCase):
    def test_idempotent_retry_ignores_transport_retry_time(self) -> None:
        first = claim(
            CoordinatorState.empty(),
            task="kinoko34077/example#1",
            role=Role.IMPLEMENTER,
            worker_id="worker-a",
            conflict_keys=("component:example:parser",),
            now=T0,
            idempotency_key="claim-transport-retry",
        )
        replay = claim(
            first.state,
            task="kinoko34077/example#1",
            role=Role.IMPLEMENTER,
            worker_id="worker-a",
            conflict_keys=("component:example:parser",),
            now=T0 + timedelta(minutes=2),
            idempotency_key="claim-transport-retry",
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(first.claim_id, replay.claim_id)
        self.assertEqual(first.generation, replay.generation)

        released = release(
            replay.state,
            claim_id=first.claim_id,
            generation=first.generation,
            now=T0 + timedelta(minutes=3),
            idempotency_key="release-transport-retry",
        )
        release_replay = release(
            released.state,
            claim_id=first.claim_id,
            generation=first.generation,
            now=T0 + timedelta(minutes=4),
            idempotency_key="release-transport-retry",
        )
        self.assertTrue(release_replay.replayed)

    def test_same_worker_cannot_claim_independent_reviewer_role_for_same_task(self) -> None:
        implementation = claim(
            CoordinatorState.empty(),
            task="kinoko34077/example#1",
            role=Role.IMPLEMENTER,
            worker_id="worker-a",
            conflict_keys=("component:example:parser",),
            now=T0,
            idempotency_key="impl",
        )
        with self.assertRaises(ClaimConflict):
            claim(
                implementation.state,
                task="kinoko34077/example#1",
                role=Role.REVIEWER,
                worker_id="worker-a",
                conflict_keys=("component:example:parser",),
                now=T0,
                idempotency_key="self-review",
            )


if __name__ == "__main__":
    unittest.main()
