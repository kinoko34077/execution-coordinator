from __future__ import annotations

import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from execution_coordinator import bootstrap_pickup as bp

NOW = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)


class _Gateway:
    def __init__(self, *, raise_on_mutate=False, events=None):
        self.raise_on_mutate = raise_on_mutate
        self.events = events if events is not None else []
        self.calls = []

    def mutate(self, *, operation, payload, idempotency_key):
        self.events.append(operation)
        self.calls.append((operation, payload, idempotency_key))
        if self.raise_on_mutate:
            raise RuntimeError("mutation unavailable")
        return types.SimpleNamespace(events=())


def _state(*lease_until):
    claims = {
        f"c{index}": types.SimpleNamespace(lease_until=value)
        for index, value in enumerate(lease_until, start=1)
    }
    return types.SimpleNamespace(state=types.SimpleNamespace(claims=claims))


class PreclassificationExpiryTests(unittest.TestCase):
    def test_expired_claim_dispatches_existing_expire_once(self):
        gateway = _Gateway()
        with patch.object(bp, "get_state_result", return_value=_state(NOW - timedelta(seconds=1))):
            changed = bp._expire_stale_before_pickup(
                state_reader=object(),
                gateway_factory=lambda: gateway,
                now=NOW,
                attempt_id="chatgpt-s1:c2",
            )
        self.assertTrue(changed)
        self.assertEqual([("expire", {}, "chatgpt-s1:c2:expire")], gateway.calls)

    def test_live_claim_does_not_dispatch_expire(self):
        gateway = _Gateway()
        with patch.object(bp, "get_state_result", return_value=_state(NOW + timedelta(minutes=1))):
            changed = bp._expire_stale_before_pickup(
                state_reader=object(),
                gateway_factory=lambda: gateway,
                now=NOW,
                attempt_id="chatgpt-s1:c2",
            )
        self.assertFalse(changed)
        self.assertEqual([], gateway.calls)

    def test_empty_state_does_not_dispatch_expire(self):
        gateway = _Gateway()
        with patch.object(bp, "get_state_result", return_value=_state()):
            changed = bp._expire_stale_before_pickup(
                state_reader=object(),
                gateway_factory=lambda: gateway,
                now=NOW,
                attempt_id="chatgpt-s1:c2",
            )
        self.assertFalse(changed)
        self.assertEqual([], gateway.calls)

    def test_expire_failure_fails_closed(self):
        gateway = _Gateway(raise_on_mutate=True)
        with patch.object(bp, "get_state_result", return_value=_state(NOW - timedelta(minutes=2))):
            with self.assertRaisesRegex(RuntimeError, "mutation unavailable"):
                bp._expire_stale_before_pickup(
                    state_reader=object(),
                    gateway_factory=lambda: gateway,
                    now=NOW,
                    attempt_id="chatgpt-s1:c2",
                )

    def test_run_pickup_expires_before_gathering_evidence(self):
        events = []
        gateway = _Gateway(events=events)

        def build_request(observation, *, target_repository, work_intent, now):
            return {
                "worker_system": "chatgpt",
                "worker_session_id": "s1",
                "execution_attempt_id": "s1:c2",
                "target_repository": target_repository,
            }

        def gather(*args, **kwargs):
            events.append("gather")
            return ({"schema_version": bp.EVIDENCE_SCHEMA}, ())

        tools = types.SimpleNamespace(
            chat_worker_profile=types.SimpleNamespace(build_request=build_request),
            chat_worker_bootstrap=types.SimpleNamespace(
                classify=lambda request, evidence: {
                    "claim_required": False,
                    "disposition": "NO_ELIGIBLE_WORK",
                },
                validate_result=lambda result: None,
            ),
        )

        def state_result(_reader):
            events.append("pre-read")
            return _state(NOW - timedelta(seconds=1))

        with (
            patch.object(bp, "get_state_result", side_effect=state_result),
            patch.object(bp, "gather_evidence", side_effect=gather),
        ):
            outcome = bp.run_pickup(
                target_repository="kinoko34077/execution-coordinator",
                observation={},
                work_intent="implement #101",
                devflow_tools=tools,
                issue_reader=object(),
                state_reader=object(),
                control_documents=(),
                agents_md_read=True,
                now=NOW,
                gateway_factory=lambda: gateway,
            )

        self.assertEqual(["pre-read", "expire", "gather"], events)
        self.assertIsNone(outcome["claim_id"])


if __name__ == "__main__":
    unittest.main()
