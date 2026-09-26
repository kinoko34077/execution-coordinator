from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from execution_coordinator.engine import (
    MAX_IDEMPOTENCY_RECORDS,
    ClaimConflict,
    claim,
    progress,
    release,
)
from execution_coordinator.model import CoordinatorState, Role
from execution_coordinator.snapshot import parse_issue_body, render_issue_body


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

    def test_high_frequency_progress_does_not_grow_idempotency_without_bound(self) -> None:
        claimed = claim(
            CoordinatorState.empty(),
            task="kinoko34077/example#1",
            role=Role.IMPLEMENTER,
            worker_id="worker-a",
            conflict_keys=(),
            now=T0,
            idempotency_key="claim-authority-event",
        )
        state = claimed.state
        for index in range(MAX_IDEMPOTENCY_RECORDS + 40):
            state = progress(
                state,
                claim_id=claimed.claim_id,
                generation=claimed.generation,
                now=T0 + timedelta(seconds=index + 1),
                idempotency_key=f"progress-{index:04d}",
            ).state

        self.assertLessEqual(len(state.idempotency), MAX_IDEMPOTENCY_RECORDS)
        self.assertIn("claim-authority-event", state.idempotency)
        self.assertNotIn("progress-0000", state.idempotency)

    def test_idempotency_retention_order_survives_snapshot_round_trip(self) -> None:
        claimed = claim(
            CoordinatorState.empty(),
            task="kinoko34077/example#1",
            role=Role.IMPLEMENTER,
            worker_id="worker-a",
            conflict_keys=(),
            now=T0,
            idempotency_key="claim-authority-event",
        )
        state = claimed.state
        body = "# synthetic coordinator state\n"
        for index in range(MAX_IDEMPOTENCY_RECORDS + 40):
            state = progress(
                state,
                claim_id=claimed.claim_id,
                generation=claimed.generation,
                now=T0 + timedelta(seconds=index + 1),
                idempotency_key=f"progress-{index:04d}",
            ).state
            body = render_issue_body(body, state)
            state = parse_issue_body(body)

        self.assertLessEqual(len(state.idempotency), MAX_IDEMPOTENCY_RECORDS)
        self.assertIn("claim-authority-event", state.idempotency)
        self.assertNotIn("progress-0000", state.idempotency)


if __name__ == "__main__":
    unittest.main()
