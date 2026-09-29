from __future__ import annotations

from dataclasses import dataclass

from .agent import AgentSession, MutationGateway
from .capability import CapabilityMatch
from .execution_request import (
    BootstrapContext,
    ClaimAuthority,
    DispatchOutcome,
    ExecutionRequest,
    LaunchStatus,
    build_auto_launch_execution_request,
)
from .model import CoordinatorState, ExecutionState, Role, WaitReason
from .ranking import candidate_fingerprint


@dataclass(frozen=True, slots=True)
class AutoLaunchKeys:
    claim: str
    acknowledge: str
    wait: str
    resume: str
    release: str
    fail: str

    def __post_init__(self) -> None:
        values = (self.claim, self.acknowledge, self.wait, self.resume, self.release, self.fail)
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError("auto-launch idempotency keys must not be empty")
        if len(set(values)) != len(values):
            raise ValueError("auto-launch idempotency keys must be distinct")


@dataclass(frozen=True, slots=True)
class ClaimedAutoLaunch:
    attempt_id: str
    task: str
    role: Role
    worker_id: str
    claim_id: str
    generation: int
    request_id: str
    candidate_fingerprint: str


@dataclass(frozen=True, slots=True)
class AutoLaunchTransition:
    state: str
    claim_id: str
    generation: int
    session_id: str | None = None


def _current_session(
    context: ClaimedAutoLaunch,
    current_state: CoordinatorState,
    gateway: MutationGateway,
) -> tuple[AgentSession, object]:
    claim = current_state.claims.get(context.claim_id)
    if claim is None or claim.generation != context.generation:
        raise RuntimeError("auto-launch claim is not current")
    if (
        claim.task != context.task
        or claim.role is not context.role
        or claim.worker_id != context.worker_id
    ):
        raise RuntimeError("auto-launch current claim identity changed")
    return (
        AgentSession.from_current_claim(
            gateway,
            claim,
            expected_task=context.task,
            expected_role=context.role,
            expected_worker_id=context.worker_id,
        ),
        claim,
    )


def claim_for_auto_launch(
    match: CapabilityMatch,
    gateway: MutationGateway,
    bootstrap: BootstrapContext,
    *,
    request_id: str,
    keys: AutoLaunchKeys,
    now,
) -> tuple[ClaimedAutoLaunch, ExecutionRequest]:
    if not isinstance(match, CapabilityMatch):
        raise TypeError("match must be a CapabilityMatch")
    if not isinstance(keys, AutoLaunchKeys):
        raise TypeError("keys must be an AutoLaunchKeys")
    if not match.requirements.observed_at <= now <= match.requirements.fresh_until:
        raise ValueError("candidate evidence is stale or from the future")

    candidate = match.candidate
    session = AgentSession(
        gateway,
        task=candidate.task,
        role=candidate.role,
        worker_id=match.worker_id,
        conflict_keys=candidate.conflict_keys,
        base_sha=bootstrap.base_sha,
        branch=bootstrap.branch,
    )
    session.claim(idempotency_key=keys.claim)
    claim_id = session.claim_id
    generation = session.generation
    if claim_id is None or generation is None:
        raise RuntimeError("claimed auto-launch session did not retain authority")

    authority = ClaimAuthority(
        claim_id=claim_id,
        generation=generation,
        task=candidate.task,
        role=candidate.role,
        worker_id=match.worker_id,
        state=ExecutionState.CLAIMED,
    )
    try:
        request = build_auto_launch_execution_request(
            match,
            authority,
            bootstrap,
            request_id=request_id,
            now=now,
        )
    except BaseException as error:
        try:
            session.release(idempotency_key=keys.release)
        except BaseException as release_error:
            error.add_note(f"release after auto-launch request build failure failed: {release_error}")
        raise

    context = ClaimedAutoLaunch(
        attempt_id=request_id,
        task=candidate.task,
        role=candidate.role,
        worker_id=match.worker_id,
        claim_id=claim_id,
        generation=generation,
        request_id=request_id,
        candidate_fingerprint=candidate_fingerprint(candidate),
    )
    return context, request


def apply_launch_outcome(
    context: ClaimedAutoLaunch,
    outcome: DispatchOutcome,
    *,
    gateway: MutationGateway,
    current_state: CoordinatorState,
    keys: AutoLaunchKeys,
    evidence_ref: str,
) -> AutoLaunchTransition:
    if outcome.request_id != context.request_id:
        raise ValueError("launch outcome request_id does not match auto-launch context")
    session, claim = _current_session(context, current_state, gateway)
    if claim.state is not ExecutionState.CLAIMED:
        raise RuntimeError("launch outcome requires current CLAIMED authority")

    if outcome.launch_status is LaunchStatus.ACCEPTED:
        if outcome.worker_id != context.worker_id or outcome.session_id is None:
            raise ValueError("accepted launch identity does not match auto-launch context")
        session.acknowledge(idempotency_key=keys.acknowledge)
        return AutoLaunchTransition(
            state="RUNNING",
            claim_id=context.claim_id,
            generation=context.generation,
            session_id=outcome.session_id,
        )
    if outcome.launch_status is LaunchStatus.UNAVAILABLE:
        session.release(idempotency_key=keys.release)
        return AutoLaunchTransition(
            state="RELEASED",
            claim_id=context.claim_id,
            generation=context.generation,
        )
    if outcome.launch_status is LaunchStatus.FAILED:
        session.fail(reason=outcome.reason or "provider launch failed", idempotency_key=keys.fail)
        return AutoLaunchTransition(
            state="FAILED",
            claim_id=context.claim_id,
            generation=context.generation,
        )
    if outcome.launch_status is LaunchStatus.AMBIGUOUS:
        session.wait(
            reason=WaitReason.PROVIDER,
            evidence_ref=evidence_ref,
            idempotency_key=keys.wait,
        )
        return AutoLaunchTransition(
            state="WAITING:PROVIDER",
            claim_id=context.claim_id,
            generation=context.generation,
        )
    raise ValueError("unsupported launch outcome")


def resume_confirmed_provider(
    context: ClaimedAutoLaunch,
    *,
    gateway: MutationGateway,
    current_state: CoordinatorState,
    keys: AutoLaunchKeys,
) -> AutoLaunchTransition:
    session, claim = _current_session(context, current_state, gateway)
    if claim.state is not ExecutionState.WAITING or claim.wait_reason is not WaitReason.PROVIDER:
        raise RuntimeError("provider resume requires current WAITING:PROVIDER authority")
    session.resume(idempotency_key=keys.resume)
    return AutoLaunchTransition(
        state="RUNNING",
        claim_id=context.claim_id,
        generation=context.generation,
    )
