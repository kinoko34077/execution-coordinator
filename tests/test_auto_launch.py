from __future__ import annotations

import unittest
from datetime import timedelta

from execution_coordinator.auto_launch import (
    AutoLaunchKeys,
    apply_launch_outcome,
    claim_for_auto_launch,
    resume_confirmed_provider,
)
from execution_coordinator.engine import CoordinationError
from execution_coordinator.execution_request import (
    AUTO_LAUNCH_EXECUTION_REQUEST_SCHEMA_VERSION,
    DispatchOutcome,
)
from execution_coordinator.model import ExecutionState, WaitReason
from execution_coordinator.snapshot import parse_issue_body
from tests.test_agent import _Gateway
from tests.test_execution_request import NOW, _bootstrap, _match


def _keys(prefix: str = "a") -> AutoLaunchKeys:
    return AutoLaunchKeys(
        claim=f"{prefix}:claim",
        acknowledge=f"{prefix}:ack",
        wait=f"{prefix}:wait",
        resume=f"{prefix}:resume",
        release=f"{prefix}:release",
        fail=f"{prefix}:fail",
    )


class AutoLaunchTests(unittest.TestCase):
    def test_claim_for_auto_launch_stops_at_claimed_and_builds_request(self) -> None:
        gateway = _Gateway(now=NOW)
        context, request = claim_for_auto_launch(
            _match(), gateway, _bootstrap(), request_id="req-auto-1", keys=_keys(), now=NOW
        )
        state = parse_issue_body(gateway.store.body)
        claim = state.claims[context.claim_id]

        self.assertEqual(ExecutionState.CLAIMED, claim.state)
        self.assertEqual(AUTO_LAUNCH_EXECUTION_REQUEST_SCHEMA_VERSION, request.schema_version)
        self.assertEqual(["claim"], [call[0] for call in gateway.calls])
        self.assertEqual(context.claim_id, request.authority.claim_id)
        self.assertEqual(context.generation, request.authority.generation)

    def test_accepted_launch_acknowledges_only_after_provider_context(self) -> None:
        gateway = _Gateway(now=NOW)
        context, request = claim_for_auto_launch(
            _match(), gateway, _bootstrap(), request_id="req-auto-2", keys=_keys(), now=NOW
        )
        outcome = DispatchOutcome.accepted(
            request_id=request.request_id,
            worker_id=context.worker_id,
            session_id="claude-session-1",
            schema_version=request.schema_version,
        )
        transition = apply_launch_outcome(
            context, outcome, gateway=gateway,
            current_state=parse_issue_body(gateway.store.body),
            keys=_keys(), evidence_ref="run:1",
        )
        claim = parse_issue_body(gateway.store.body).claims[context.claim_id]
        self.assertEqual(ExecutionState.RUNNING, claim.state)
        self.assertEqual("RUNNING", transition.state)
        self.assertEqual(["claim", "acknowledge"], [call[0] for call in gateway.calls])

    def test_unavailable_and_failed_are_terminal_without_live_claim(self) -> None:
        for status in ("unavailable", "failed"):
            gateway = _Gateway(now=NOW)
            keys = _keys(status)
            context, request = claim_for_auto_launch(
                _match(), gateway, _bootstrap(), request_id=f"req-{status}", keys=keys, now=NOW
            )
            outcome = (
                DispatchOutcome.unavailable(
                    request_id=request.request_id,
                    reason="provider unavailable",
                    schema_version=request.schema_version,
                )
                if status == "unavailable"
                else DispatchOutcome.failed(
                    request_id=request.request_id,
                    reason="provider failed",
                    schema_version=request.schema_version,
                )
            )
            transition = apply_launch_outcome(
                context, outcome, gateway=gateway,
                current_state=parse_issue_body(gateway.store.body),
                keys=keys, evidence_ref="run:terminal",
            )
            self.assertNotIn(context.claim_id, parse_issue_body(gateway.store.body).claims)
            self.assertEqual("RELEASED" if status == "unavailable" else "FAILED", transition.state)

    def test_ambiguous_launch_waits_then_confirmed_provider_resumes(self) -> None:
        gateway = _Gateway(now=NOW)
        keys = _keys("amb")
        context, request = claim_for_auto_launch(
            _match(), gateway, _bootstrap(), request_id="req-amb", keys=keys, now=NOW
        )
        outcome = DispatchOutcome.ambiguous(
            request_id=request.request_id,
            reason="start result unknown",
            schema_version=request.schema_version,
        )
        transition = apply_launch_outcome(
            context, outcome, gateway=gateway,
            current_state=parse_issue_body(gateway.store.body),
            keys=keys, evidence_ref="run:ambiguous",
        )
        waiting = parse_issue_body(gateway.store.body).claims[context.claim_id]
        self.assertEqual(ExecutionState.WAITING, waiting.state)
        self.assertEqual(WaitReason.PROVIDER, waiting.wait_reason)
        self.assertEqual("WAITING:PROVIDER", transition.state)

        resumed = resume_confirmed_provider(
            context, gateway=gateway,
            current_state=parse_issue_body(gateway.store.body),
            keys=keys,
        )
        running = parse_issue_body(gateway.store.body).claims[context.claim_id]
        self.assertEqual(ExecutionState.RUNNING, running.state)
        self.assertEqual("RUNNING", resumed.state)
        self.assertEqual(
            ["claim", "wait", "resume"],
            [call[0] for call in gateway.calls],
        )

    def test_racing_claim_or_stale_generation_never_falls_through(self) -> None:
        gateway = _Gateway(now=NOW)
        context, request = claim_for_auto_launch(
            _match(), gateway, _bootstrap(), request_id="req-race-1", keys=_keys("r1"), now=NOW
        )
        with self.assertRaises(CoordinationError):
            claim_for_auto_launch(
                _match(), gateway, _bootstrap(), request_id="req-race-2", keys=_keys("r2"), now=NOW
            )
        self.assertEqual(2, [call[0] for call in gateway.calls].count("claim"))

        gateway.now = NOW + timedelta(minutes=16)
        from execution_coordinator.mutate import apply_mutation
        apply_mutation(
            gateway.store,
            operation="expire",
            payload={},
            idempotency_key="expire-old",
            now=gateway.now,
        )
        replacement, _ = claim_for_auto_launch(
            _match(), gateway, _bootstrap(), request_id="req-race-3", keys=_keys("r3"), now=gateway.now
        )
        self.assertGreater(replacement.generation, context.generation)
        outcome = DispatchOutcome.accepted(
            request_id=request.request_id,
            worker_id=context.worker_id,
            session_id="late-session",
            schema_version=request.schema_version,
        )
        with self.assertRaisesRegex(RuntimeError, "current"):
            apply_launch_outcome(
                context, outcome, gateway=gateway,
                current_state=parse_issue_body(gateway.store.body),
                keys=_keys("r1"), evidence_ref="run:late",
            )


if __name__ == "__main__":
    unittest.main()
