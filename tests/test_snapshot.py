from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from execution_coordinator.engine import claim, wait
from execution_coordinator.model import CoordinatorState, Role, WaitReason
from execution_coordinator.snapshot import (
    BEGIN_MARKER,
    END_MARKER,
    SnapshotError,
    parse_issue_body,
    render_issue_body,
)


UTC = timezone.utc
T0 = datetime(2026, 9, 27, 1, 0, tzinfo=UTC)


class SnapshotCodecTests(unittest.TestCase):
    def _state(self) -> CoordinatorState:
        claimed = claim(
            CoordinatorState.empty(),
            task="kinoko34077/example#1",
            role=Role.IMPLEMENTER,
            worker_id="worker-a",
            conflict_keys=("component:example:parser",),
            now=T0,
            idempotency_key="claim-1",
            base_sha="abc123",
            branch="work/example",
        )
        return wait(
            claimed.state,
            claim_id=claimed.claim_id,
            generation=claimed.generation,
            reason=WaitReason.CI,
            evidence_ref="example#2/checks",
            now=T0,
            idempotency_key="wait-1",
        ).state

    def _payload(self) -> dict[str, object]:
        rendered = render_issue_body("header\n", self._state())
        payload_text = rendered.split(BEGIN_MARKER, 1)[1].split(END_MARKER, 1)[0].strip()
        return json.loads(payload_text)

    def _body(self, payload: dict[str, object]) -> str:
        return f"human\n{BEGIN_MARKER}\n{json.dumps(payload)}\n{END_MARKER}\n"

    def _two_claim_payload(
        self,
        *,
        second_task: str,
        second_role: Role,
        second_worker: str,
        second_conflict_key: str,
    ) -> dict[str, object]:
        first = claim(
            CoordinatorState.empty(),
            task="kinoko34077/example#1",
            role=Role.IMPLEMENTER,
            worker_id="worker-a",
            conflict_keys=("component:example:parser",),
            now=T0,
            idempotency_key="claim-first",
        )
        second = claim(
            first.state,
            task=second_task,
            role=second_role,
            worker_id=second_worker,
            conflict_keys=(second_conflict_key,),
            now=T0,
            idempotency_key="claim-second",
        )
        rendered = render_issue_body("header\n", second.state)
        payload_text = rendered.split(BEGIN_MARKER, 1)[1].split(END_MARKER, 1)[0].strip()
        return json.loads(payload_text)

    def test_absent_marker_initializes_empty_state(self) -> None:
        body = "# Execution Coordination State\n\nHuman-readable warning only.\n"
        state = parse_issue_body(body)
        self.assertEqual(CoordinatorState.empty(), state)

    def test_render_preserves_human_text_and_round_trips_deterministically(self) -> None:
        prefix = "# Execution Coordination State\n\nDo not edit the machine snapshot manually.\n"
        state = self._state()
        first = render_issue_body(prefix, state)
        second = render_issue_body(first, state)

        self.assertEqual(first, second)
        self.assertTrue(first.startswith(prefix))
        self.assertEqual(1, first.count(BEGIN_MARKER))
        self.assertEqual(1, first.count(END_MARKER))
        self.assertEqual(state, parse_issue_body(first))

    def test_serialized_timestamps_are_utc_z_only(self) -> None:
        payload = self._payload()
        only_claim = next(iter(payload["claims"].values()))

        self.assertTrue(only_claim["claimed_at"].endswith("Z"))
        self.assertNotIn("+00:00", only_claim["claimed_at"])
        self.assertTrue(only_claim["lease_until"].endswith("Z"))

    def test_malformed_json_fails_closed(self) -> None:
        body = f"human\n{BEGIN_MARKER}\n{{not-json}}\n{END_MARKER}\n"
        with self.assertRaises(SnapshotError):
            parse_issue_body(body)

    def test_unsupported_schema_version_fails_closed(self) -> None:
        body = (
            f"human\n{BEGIN_MARKER}\n"
            '{"schema_version":2,"claims":{},"generations":{},"idempotency":{}}\n'
            f"{END_MARKER}\n"
        )
        with self.assertRaises(SnapshotError):
            parse_issue_body(body)

    def test_duplicate_marker_pairs_fail_closed(self) -> None:
        payload = '{"schema_version":1,"claims":{},"generations":{},"idempotency":{}}'
        body = (
            f"{BEGIN_MARKER}\n{payload}\n{END_MARKER}\n"
            f"{BEGIN_MARKER}\n{payload}\n{END_MARKER}\n"
        )
        with self.assertRaises(SnapshotError):
            parse_issue_body(body)
        with self.assertRaises(SnapshotError):
            render_issue_body(body, CoordinatorState.empty())

    def test_missing_single_end_marker_fails_closed(self) -> None:
        body = f"human\n{BEGIN_MARKER}\n{{}}\n"
        with self.assertRaises(SnapshotError):
            parse_issue_body(body)

    def test_active_claim_generation_must_match_generation_table(self) -> None:
        payload = self._payload()
        boundary = next(iter(payload["generations"]))
        payload["generations"][boundary] += 1
        with self.assertRaises(SnapshotError):
            parse_issue_body(self._body(payload))

    def test_duplicate_active_task_role_boundary_fails_closed(self) -> None:
        payload = self._payload()
        claim_id, claim_data = next(iter(payload["claims"].items()))
        duplicate = dict(claim_data)
        duplicate["claim_id"] = f"{claim_id}-duplicate"
        payload["claims"][duplicate["claim_id"]] = duplicate
        with self.assertRaises(SnapshotError):
            parse_issue_body(self._body(payload))

    def test_active_incompatible_conflict_keys_fail_closed(self) -> None:
        payload = self._two_claim_payload(
            second_task="kinoko34077/example#2",
            second_role=Role.IMPLEMENTER,
            second_worker="worker-b",
            second_conflict_key="component:example:renderer",
        )
        claims = list(payload["claims"].values())
        claims[1]["conflict_keys"] = list(claims[0]["conflict_keys"])
        with self.assertRaises(SnapshotError):
            parse_issue_body(self._body(payload))

    def test_same_worker_implementer_reviewer_overlap_fails_closed(self) -> None:
        payload = self._two_claim_payload(
            second_task="kinoko34077/example#1",
            second_role=Role.REVIEWER,
            second_worker="worker-b",
            second_conflict_key="component:example:review",
        )
        reviewer = next(
            claim_data
            for claim_data in payload["claims"].values()
            if claim_data["role"] == Role.REVIEWER.value
        )
        reviewer["worker_id"] = "worker-a"
        with self.assertRaises(SnapshotError):
            parse_issue_body(self._body(payload))

    def test_waiting_claim_requires_reason_and_evidence(self) -> None:
        payload = self._payload()
        claim_data = next(iter(payload["claims"].values()))
        claim_data["evidence_ref"] = None
        with self.assertRaises(SnapshotError):
            parse_issue_body(self._body(payload))

    def test_non_waiting_claim_must_not_carry_wait_metadata(self) -> None:
        payload = self._payload()
        claim_data = next(iter(payload["claims"].values()))
        claim_data["state"] = "RUNNING"
        with self.assertRaises(SnapshotError):
            parse_issue_body(self._body(payload))


if __name__ == "__main__":
    unittest.main()
