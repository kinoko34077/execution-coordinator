from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from execution_coordinator.capability import (
    CAPABILITY_SCHEMA_VERSION,
    CandidateRequirements,
    CapabilityMatch,
)
from execution_coordinator.execution_request import (
    BOOTSTRAP_CONTEXT_SCHEMA_VERSION,
    EXECUTION_REQUEST_SCHEMA_VERSION,
    BootstrapContext,
    ClaimAuthority,
    DispatchProtocolError,
    DispatchOutcome,
    LaunchStatus,
    LaunchUnavailableError,
    ReconciliationReason,
    WorkerDiedBeforeAcknowledgeError,
    build_execution_request,
    dispatch_execution_request,
)
from execution_coordinator.model import ExecutionState, Role
from execution_coordinator.query import ClaimCandidate
from execution_coordinator.ranking import candidate_fingerprint


UTC = timezone.utc
NOW = datetime(2026, 9, 28, 13, 0, tzinfo=UTC)


def _match() -> CapabilityMatch:
    candidate = ClaimCandidate(
        task="owner/repo#1",
        role=Role.IMPLEMENTER,
        entry_ref="https://github.com/owner/repo/issues/1",
        conflict_keys=("owner/repo#1",),
    )
    requirements = CandidateRequirements(
        schema_version=CAPABILITY_SCHEMA_VERSION,
        source_ref="kinoko34077/devflow#107",
        task=candidate.task,
        role=candidate.role,
        candidate_fingerprint=candidate_fingerprint(candidate),
        required_capabilities=frozenset({"python"}),
        required_environment=frozenset({"linux"}),
        observed_at=NOW - timedelta(minutes=1),
        fresh_until=NOW + timedelta(minutes=30),
    )
    return CapabilityMatch(
        candidate=candidate,
        requirements=requirements,
        worker_id="worker-a",
    )


def _authority(*, state: ExecutionState = ExecutionState.RUNNING) -> ClaimAuthority:
    return ClaimAuthority(
        claim_id="clm_phase5",
        generation=2,
        task="owner/repo#1",
        role=Role.IMPLEMENTER,
        worker_id="worker-a",
        state=state,
    )


def _bootstrap() -> BootstrapContext:
    return BootstrapContext(
        schema_version=BOOTSTRAP_CONTEXT_SCHEMA_VERSION,
        context_ref="worktree://execution-coordinator/phase5",
        base_sha="f799b352a4565a9cbddf9a7245e2fd19b32034cf",
        branch="feature/phase5-execution-request",
        parameters=(("cwd", "C:/work"), ("mode", "bounded")),
    )


def _request() -> object:
    return build_execution_request(
        _match(),
        _authority(),
        _bootstrap(),
        request_id="req_phase5_1",
        now=NOW,
    )


class _Adapter:
    def __init__(self, result: DispatchOutcome | None = None, error: BaseException | None = None) -> None:
        self.result = result
        self.error = error
        self.called = False

    def start(self, request: object) -> DispatchOutcome:
        self.called = True
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


class ExecutionRequestTests(unittest.TestCase):
    def test_request_binds_running_authority_evidence_requirements_and_context(self) -> None:
        request = _request()

        self.assertEqual(EXECUTION_REQUEST_SCHEMA_VERSION, request.schema_version)
        self.assertEqual("req_phase5_1", request.request_id)
        self.assertEqual("clm_phase5", request.authority.claim_id)
        self.assertEqual(ExecutionState.RUNNING, request.authority.state)
        self.assertEqual("owner/repo#1", request.authority.task)
        self.assertEqual("kinoko34077/devflow#107", request.evidence.source_ref)
        self.assertEqual(frozenset({"python"}), request.required_capabilities)
        self.assertEqual("worktree://execution-coordinator/phase5", request.bootstrap.context_ref)

    def test_request_requires_acknowledged_authority_and_fresh_matching_identity(self) -> None:
        with self.assertRaisesRegex(ValueError, "RUNNING"):
            build_execution_request(
                _match(),
                _authority(state=ExecutionState.CLAIMED),
                _bootstrap(),
                request_id="req_claimed",
                now=NOW,
            )

        with self.assertRaisesRegex(ValueError, "stale"):
            build_execution_request(
                _match(),
                _authority(),
                _bootstrap(),
                request_id="req_stale",
                now=NOW + timedelta(hours=1),
            )

        with self.assertRaisesRegex(ValueError, "timezone"):
            build_execution_request(
                _match(),
                _authority(),
                _bootstrap(),
                request_id="req_naive",
                now=datetime(2026, 9, 28, 13, 0),
            )

        with self.assertRaisesRegex(ValueError, "task/role"):
            build_execution_request(
                _match(),
                replace(_authority(), task="other/repo#2"),
                _bootstrap(),
                request_id="req_mismatch",
                now=NOW,
            )

    def test_bootstrap_parameters_are_explicit_and_unique(self) -> None:
        with self.assertRaisesRegex(ValueError, "unique"):
            BootstrapContext(
                schema_version=BOOTSTRAP_CONTEXT_SCHEMA_VERSION,
                context_ref="worktree://duplicate",
                parameters=(("cwd", "C:/one"), ("cwd", "C:/two")),
            )

    def test_launch_accepted_returns_worker_and_session_identity(self) -> None:
        request = _request()
        adapter = _Adapter(
            result=DispatchOutcome.accepted(
                request_id=request.request_id,
                worker_id="worker-a",
                session_id="session-1",
            )
        )

        outcome = dispatch_execution_request(adapter, request, now=NOW)

        self.assertEqual(LaunchStatus.ACCEPTED, outcome.launch_status)
        self.assertEqual("worker-a", outcome.worker_id)
        self.assertEqual("session-1", outcome.session_id)
        self.assertFalse(outcome.reconciliation_required)

    def test_unavailable_and_pre_acknowledge_death_require_reconciliation(self) -> None:
        request = _request()
        unavailable = dispatch_execution_request(
            _Adapter(error=LaunchUnavailableError("no worker transport")),
            request,
            now=NOW,
        )
        self.assertEqual(LaunchStatus.UNAVAILABLE, unavailable.launch_status)
        self.assertTrue(unavailable.reconciliation_required)
        self.assertEqual(
            ReconciliationReason.CLAIM_PRESENT_LAUNCH_NOT_STARTED,
            unavailable.reconciliation_reason,
        )
        self.assertIsNone(unavailable.session_id)

        died = dispatch_execution_request(
            _Adapter(error=WorkerDiedBeforeAcknowledgeError("worker exited")),
            request,
            now=NOW,
        )
        self.assertEqual(LaunchStatus.FAILED, died.launch_status)
        self.assertTrue(died.reconciliation_required)
        self.assertEqual(
            ReconciliationReason.WORKER_DIED_BEFORE_ACKNOWLEDGE,
            died.reconciliation_reason,
        )

    def test_invalid_adapter_success_fails_closed_and_unexpected_error_is_not_success(self) -> None:
        request = _request()
        wrong_worker = _Adapter(
            result=DispatchOutcome.accepted(
                request_id=request.request_id,
                worker_id="worker-b",
                session_id="session-2",
            )
        )
        with self.assertRaisesRegex(DispatchProtocolError, "worker_id"):
            dispatch_execution_request(wrong_worker, request, now=NOW)

        unexpected = _Adapter(error=RuntimeError("transport crashed"))
        with self.assertRaisesRegex(RuntimeError, "transport crashed"):
            dispatch_execution_request(unexpected, request, now=NOW)

    def test_stale_request_never_calls_adapter(self) -> None:
        request = _request()
        adapter = _Adapter(
            result=DispatchOutcome.accepted(
                request_id=request.request_id,
                worker_id="worker-a",
                session_id="session-3",
            )
        )

        with self.assertRaisesRegex(DispatchProtocolError, "stale"):
            dispatch_execution_request(
                adapter,
                request,
                now=NOW + timedelta(hours=1),
            )
        self.assertFalse(adapter.called)


if __name__ == "__main__":
    unittest.main()
