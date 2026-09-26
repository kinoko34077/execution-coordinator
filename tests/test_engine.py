from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from execution_coordinator.engine import (
    ClaimConflict,
    IdempotencyConflict,
    StaleGeneration,
    acknowledge,
    claim,
    expire,
    progress,
    release,
    renew,
    takeover,
    wait,
)
from execution_coordinator.model import CoordinatorState, ExecutionState, Role, WaitReason


UTC = timezone.utc
T0 = datetime(2026, 9, 27, 0, 0, tzinfo=UTC)


class ClaimLeaseEngineTests(unittest.TestCase):
    def test_same_task_role_cannot_be_claimed_twice(self) -> None:
        state = CoordinatorState.empty()
        first = claim(
            state,
            task="kinoko34077/example#1",
            role=Role.IMPLEMENTER,
            worker_id="worker-a",
            conflict_keys=("component:example:parser",),
            now=T0,
            idempotency_key="claim-a",
        )

        with self.assertRaises(ClaimConflict):
            claim(
                first.state,
                task="kinoko34077/example#1",
                role=Role.IMPLEMENTER,
                worker_id="worker-b",
                conflict_keys=("component:example:other",),
                now=T0,
                idempotency_key="claim-b",
            )

    def test_non_conflicting_claims_can_coexist(self) -> None:
        state = CoordinatorState.empty()
        first = claim(
            state,
            task="kinoko34077/example#1",
            role=Role.IMPLEMENTER,
            worker_id="worker-a",
            conflict_keys=("component:example:parser",),
            now=T0,
            idempotency_key="claim-a",
        )
        second = claim(
            first.state,
            task="kinoko34077/example#2",
            role=Role.IMPLEMENTER,
            worker_id="worker-b",
            conflict_keys=("component:example:ui",),
            now=T0,
            idempotency_key="claim-b",
        )
        self.assertEqual(2, len(second.state.claims))

    def test_incompatible_conflict_key_rejects_second_implementer(self) -> None:
        state = CoordinatorState.empty()
        first = claim(
            state,
            task="kinoko34077/example#1",
            role=Role.IMPLEMENTER,
            worker_id="worker-a",
            conflict_keys=("contract:agent-api-v2",),
            now=T0,
            idempotency_key="claim-a",
        )
        with self.assertRaises(ClaimConflict):
            claim(
                first.state,
                task="kinoko34077/other#4",
                role=Role.IMPLEMENTER,
                worker_id="worker-b",
                conflict_keys=("contract:agent-api-v2",),
                now=T0,
                idempotency_key="claim-b",
            )

    def test_reviewer_can_share_conflict_key_with_implementer(self) -> None:
        state = CoordinatorState.empty()
        implementation = claim(
            state,
            task="kinoko34077/example#1",
            role=Role.IMPLEMENTER,
            worker_id="worker-a",
            conflict_keys=("component:example:parser",),
            now=T0,
            idempotency_key="impl",
        )
        review = claim(
            implementation.state,
            task="kinoko34077/example#1",
            role=Role.REVIEWER,
            worker_id="worker-b",
            conflict_keys=("component:example:parser",),
            now=T0,
            idempotency_key="review",
        )
        self.assertEqual(2, len(review.state.claims))

    def test_renew_extends_current_generation_only(self) -> None:
        claimed = claim(
            CoordinatorState.empty(),
            task="kinoko34077/example#1",
            role=Role.IMPLEMENTER,
            worker_id="worker-a",
            conflict_keys=(),
            now=T0,
            idempotency_key="claim",
        )
        renewed = renew(
            claimed.state,
            claim_id=claimed.claim_id,
            generation=claimed.generation,
            now=T0 + timedelta(minutes=5),
            idempotency_key="renew",
        )
        current = renewed.state.claims[claimed.claim_id]
        self.assertEqual(T0 + timedelta(minutes=20), current.lease_until)
        self.assertEqual(T0 + timedelta(minutes=5), current.heartbeat_at)

        with self.assertRaises(StaleGeneration):
            renew(
                renewed.state,
                claim_id=claimed.claim_id,
                generation=claimed.generation - 1,
                now=T0 + timedelta(minutes=6),
                idempotency_key="stale-renew",
            )

    def test_progress_does_not_replace_heartbeat(self) -> None:
        claimed = claim(
            CoordinatorState.empty(),
            task="kinoko34077/example#1",
            role=Role.IMPLEMENTER,
            worker_id="worker-a",
            conflict_keys=(),
            now=T0,
            idempotency_key="claim",
        )
        advanced = progress(
            claimed.state,
            claim_id=claimed.claim_id,
            generation=claimed.generation,
            now=T0 + timedelta(minutes=3),
            idempotency_key="progress",
        )
        current = advanced.state.claims[claimed.claim_id]
        self.assertEqual(T0, current.heartbeat_at)
        self.assertEqual(T0 + timedelta(minutes=3), current.last_progress_at)

    def test_expiry_then_takeover_increments_generation_and_fences_old_worker(self) -> None:
        claimed = claim(
            CoordinatorState.empty(),
            task="kinoko34077/example#1",
            role=Role.IMPLEMENTER,
            worker_id="worker-a",
            conflict_keys=("component:example:parser",),
            now=T0,
            idempotency_key="claim-a",
        )
        expired = expire(
            claimed.state,
            now=T0 + timedelta(minutes=16),
            idempotency_key="expire",
        )
        self.assertNotIn(claimed.claim_id, expired.state.claims)
        self.assertEqual(1, len(expired.events))
        self.assertEqual("EXPIRED", expired.events[0].kind)

        replacement = takeover(
            expired.state,
            task="kinoko34077/example#1",
            role=Role.IMPLEMENTER,
            worker_id="worker-b",
            conflict_keys=("component:example:parser",),
            now=T0 + timedelta(minutes=16),
            idempotency_key="takeover",
        )
        self.assertEqual(claimed.generation + 1, replacement.generation)

        with self.assertRaises(StaleGeneration):
            acknowledge(
                replacement.state,
                claim_id=claimed.claim_id,
                generation=claimed.generation,
                now=T0 + timedelta(minutes=17),
                idempotency_key="stale-ack",
            )

    def test_wait_with_evidence_is_not_expired_as_lost_execution(self) -> None:
        claimed = claim(
            CoordinatorState.empty(),
            task="kinoko34077/example#1",
            role=Role.IMPLEMENTER,
            worker_id="worker-a",
            conflict_keys=(),
            now=T0,
            idempotency_key="claim",
        )
        waiting = wait(
            claimed.state,
            claim_id=claimed.claim_id,
            generation=claimed.generation,
            reason=WaitReason.CI,
            evidence_ref="pr#9/checks",
            now=T0 + timedelta(minutes=2),
            idempotency_key="wait",
        )
        current = waiting.state.claims[claimed.claim_id]
        self.assertEqual(ExecutionState.WAITING, current.state)
        self.assertEqual(WaitReason.CI, current.wait_reason)

        after_expiry_scan = expire(
            waiting.state,
            now=T0 + timedelta(minutes=30),
            idempotency_key="expire-scan",
        )
        self.assertIn(claimed.claim_id, after_expiry_scan.state.claims)

    def test_idempotent_retry_replays_same_result_but_mismatched_payload_fails(self) -> None:
        first = claim(
            CoordinatorState.empty(),
            task="kinoko34077/example#1",
            role=Role.IMPLEMENTER,
            worker_id="worker-a",
            conflict_keys=(),
            now=T0,
            idempotency_key="same-key",
        )
        replay = claim(
            first.state,
            task="kinoko34077/example#1",
            role=Role.IMPLEMENTER,
            worker_id="worker-a",
            conflict_keys=(),
            now=T0,
            idempotency_key="same-key",
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(first.claim_id, replay.claim_id)
        self.assertEqual(first.generation, replay.generation)

        with self.assertRaises(IdempotencyConflict):
            claim(
                replay.state,
                task="kinoko34077/example#2",
                role=Role.IMPLEMENTER,
                worker_id="worker-a",
                conflict_keys=(),
                now=T0,
                idempotency_key="same-key",
            )

    def test_release_removes_current_claim_without_marking_task_done(self) -> None:
        claimed = claim(
            CoordinatorState.empty(),
            task="kinoko34077/example#1",
            role=Role.IMPLEMENTER,
            worker_id="worker-a",
            conflict_keys=(),
            now=T0,
            idempotency_key="claim",
        )
        released = release(
            claimed.state,
            claim_id=claimed.claim_id,
            generation=claimed.generation,
            now=T0 + timedelta(minutes=1),
            idempotency_key="release",
        )
        self.assertNotIn(claimed.claim_id, released.state.claims)
        self.assertEqual("RELEASED", released.events[0].kind)


if __name__ == "__main__":
    unittest.main()
