from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from execution_coordinator.model import Claim, CoordinatorState, ExecutionState, Role, WaitReason
from execution_coordinator.query import get_state, main
from execution_coordinator.snapshot import SnapshotError, render_issue_body


NOW = datetime(2026, 9, 27, 4, 0, tzinfo=timezone.utc)


class RecordingStore:
    def __init__(self, body: str, *, read_error: Exception | None = None) -> None:
        self.body = body
        self.read_error = read_error
        self.load_calls = 0
        self.save_calls = 0
        self.comment_calls = 0

    def load_body(self) -> str:
        self.load_calls += 1
        if self.read_error is not None:
            raise self.read_error
        return self.body

    def save_body(self, body: str) -> None:
        self.save_calls += 1
        raise AssertionError("get_state must not write the system Issue")

    def add_comment(self, body: str) -> None:
        self.comment_calls += 1
        raise AssertionError("get_state must not append comments")


def _state_with_claim(*, state: ExecutionState) -> CoordinatorState:
    wait_reason = WaitReason.CI if state is ExecutionState.WAITING else None
    evidence_ref = "run:123" if state is ExecutionState.WAITING else None
    claim = Claim(
        claim_id="clm_query",
        generation=1,
        task="kinoko34077/example#1",
        role=Role.IMPLEMENTER,
        worker_id="worker-query",
        conflict_keys=("component:kinoko34077/example:core",),
        claimed_at=NOW,
        heartbeat_at=NOW,
        last_progress_at=NOW,
        lease_until=NOW + timedelta(minutes=15),
        state=state,
        wait_reason=wait_reason,
        evidence_ref=evidence_ref,
        base_sha="abc123",
        branch="work/example",
    )
    return CoordinatorState(
        claims={claim.claim_id: claim},
        generations={f"{claim.task}|{claim.role.value}": 1},
    )


class GetStateTests(unittest.TestCase):
    def test_empty_snapshot_is_returned_without_writes(self) -> None:
        store = RecordingStore(render_issue_body("", CoordinatorState.empty()))

        state = get_state(store)

        self.assertEqual(state.claims, {})
        self.assertEqual(store.load_calls, 1)
        self.assertEqual(store.save_calls, 0)
        self.assertEqual(store.comment_calls, 0)

    def test_active_claim_is_decoded(self) -> None:
        expected = _state_with_claim(state=ExecutionState.RUNNING)
        store = RecordingStore(render_issue_body("", expected))

        state = get_state(store)

        claim = state.claims["clm_query"]
        self.assertEqual(claim.state, ExecutionState.RUNNING)
        self.assertEqual(claim.generation, 1)
        self.assertEqual(claim.task, "kinoko34077/example#1")

    def test_waiting_claim_preserves_wait_evidence(self) -> None:
        expected = _state_with_claim(state=ExecutionState.WAITING)
        store = RecordingStore(render_issue_body("", expected))

        state = get_state(store)

        claim = state.claims["clm_query"]
        self.assertEqual(claim.state, ExecutionState.WAITING)
        self.assertEqual(claim.wait_reason, WaitReason.CI)
        self.assertEqual(claim.evidence_ref, "run:123")

    def test_malformed_snapshot_fails_closed_without_writes(self) -> None:
        store = RecordingStore(
            "<!-- EXECUTION_COORDINATOR_STATE_V1_BEGIN -->\n{bad json}\n"
            "<!-- EXECUTION_COORDINATOR_STATE_V1_END -->"
        )

        with self.assertRaises(SnapshotError):
            get_state(store)

        self.assertEqual(store.save_calls, 0)
        self.assertEqual(store.comment_calls, 0)

    def test_read_failure_is_propagated_without_writes(self) -> None:
        store = RecordingStore("", read_error=RuntimeError("read failed"))

        with self.assertRaisesRegex(RuntimeError, "read failed"):
            get_state(store)

        self.assertEqual(store.save_calls, 0)
        self.assertEqual(store.comment_calls, 0)

    def test_cli_prints_validated_state_json(self) -> None:
        store = RecordingStore(render_issue_body("", _state_with_claim(state=ExecutionState.RUNNING)))
        stdout = io.StringIO()

        with patch("execution_coordinator.query._build_store_from_env", return_value=store):
            with redirect_stdout(stdout):
                exit_code = main(["get_state"])

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["claims"]["clm_query"]["state"], "RUNNING")
        self.assertEqual(store.save_calls, 0)
        self.assertEqual(store.comment_calls, 0)


if __name__ == "__main__":
    unittest.main()
