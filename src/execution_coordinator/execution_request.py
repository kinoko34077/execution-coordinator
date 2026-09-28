from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from .capability import CapabilityMatch
from .model import ExecutionState, Role
from .ranking import candidate_fingerprint


EXECUTION_REQUEST_SCHEMA_VERSION = "execution-request.v1"
BOOTSTRAP_CONTEXT_SCHEMA_VERSION = "execution-bootstrap-context.v1"

_FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{64}$")
_TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")


def _require_nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _require_utc(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must use UTC timezone")


def _normalize_tags(value: object, field: str) -> frozenset[str]:
    if not isinstance(value, (frozenset, set, tuple, list)):
        raise ValueError(f"{field} must be a finite collection of tags")
    try:
        tags = frozenset(value)
    except TypeError as exc:
        raise ValueError(f"{field} must contain hashable string tags") from exc
    if any(not isinstance(tag, str) or _TAG.fullmatch(tag) is None for tag in tags):
        raise ValueError(f"{field} contains a malformed tag")
    return tags


@dataclass(frozen=True, slots=True)
class ClaimAuthority:
    """The already acknowledged runtime authority carried by one request."""

    claim_id: str
    generation: int
    task: str
    role: Role
    worker_id: str
    state: ExecutionState = ExecutionState.RUNNING

    def __post_init__(self) -> None:
        _require_nonempty(self.claim_id, "claim_id")
        if (
            not isinstance(self.generation, int)
            or isinstance(self.generation, bool)
            or self.generation <= 0
        ):
            raise ValueError("generation must be a positive integer")
        _require_nonempty(self.task, "task")
        _require_nonempty(self.worker_id, "worker_id")
        try:
            role = Role(self.role)
            state = ExecutionState(self.state)
        except ValueError as exc:
            raise ValueError("claim authority role/state is unsupported") from exc
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "state", state)
        if state is not ExecutionState.RUNNING:
            raise ValueError("execution request requires RUNNING authority after acknowledge")


@dataclass(frozen=True, slots=True)
class ExecutionEvidence:
    """The exact candidate-source evidence bound to an execution request."""

    schema_version: str
    source_ref: str
    candidate_fingerprint: str
    observed_at: datetime
    fresh_until: datetime

    def __post_init__(self) -> None:
        if self.schema_version != EXECUTION_REQUEST_SCHEMA_VERSION:
            raise ValueError("execution evidence schema_version is unsupported")
        _require_nonempty(self.source_ref, "source_ref")
        if not isinstance(self.candidate_fingerprint, str) or _FINGERPRINT.fullmatch(
            self.candidate_fingerprint
        ) is None:
            raise ValueError("candidate_fingerprint is malformed")
        _require_utc(self.observed_at, "observed_at")
        _require_utc(self.fresh_until, "fresh_until")
        if self.fresh_until <= self.observed_at:
            raise ValueError("fresh_until must be after observed_at")

    def is_fresh(self, now: datetime) -> bool:
        _require_utc(now, "now")
        return self.observed_at <= now <= self.fresh_until


@dataclass(frozen=True, slots=True)
class BootstrapContext:
    """Explicit execution bootstrap data; it carries no task authority."""

    schema_version: str
    context_ref: str
    base_sha: str | None = None
    branch: str | None = None
    parameters: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != BOOTSTRAP_CONTEXT_SCHEMA_VERSION:
            raise ValueError("bootstrap context schema_version is unsupported")
        _require_nonempty(self.context_ref, "context_ref")
        if self.base_sha is not None:
            _require_nonempty(self.base_sha, "base_sha")
        if self.branch is not None:
            _require_nonempty(self.branch, "branch")
        try:
            parameters = tuple(self.parameters)
        except TypeError as exc:
            raise ValueError("parameters must be finite key/value pairs") from exc
        seen: set[str] = set()
        normalized: list[tuple[str, str]] = []
        for item in parameters:
            if (
                not isinstance(item, (tuple, list))
                or len(item) != 2
                or not isinstance(item[0], str)
                or not isinstance(item[1], str)
            ):
                raise ValueError("bootstrap parameters must be string key/value pairs")
            name, value = item
            _require_nonempty(name, "bootstrap parameter name")
            if name in seen:
                raise ValueError("bootstrap parameter names must be unique")
            seen.add(name)
            normalized.append((name, value))
        object.__setattr__(self, "parameters", tuple(normalized))


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    """A provider-neutral request after claim and acknowledge succeeded."""

    schema_version: str
    request_id: str
    authority: ClaimAuthority
    entry_ref: str
    evidence: ExecutionEvidence
    required_capabilities: frozenset[str]
    required_environment: frozenset[str]
    bootstrap: BootstrapContext

    def __post_init__(self) -> None:
        if self.schema_version != EXECUTION_REQUEST_SCHEMA_VERSION:
            raise ValueError("execution request schema_version is unsupported")
        _require_nonempty(self.request_id, "request_id")
        if not isinstance(self.authority, ClaimAuthority):
            raise TypeError("authority must be a ClaimAuthority")
        _require_nonempty(self.entry_ref, "entry_ref")
        if not isinstance(self.evidence, ExecutionEvidence):
            raise TypeError("evidence must be ExecutionEvidence")
        if not isinstance(self.bootstrap, BootstrapContext):
            raise TypeError("bootstrap must be a BootstrapContext")
        object.__setattr__(
            self,
            "required_capabilities",
            _normalize_tags(self.required_capabilities, "required_capabilities"),
        )
        object.__setattr__(
            self,
            "required_environment",
            _normalize_tags(self.required_environment, "required_environment"),
        )

    def is_fresh(self, now: datetime) -> bool:
        return self.evidence.is_fresh(now)


def build_execution_request(
    match: CapabilityMatch,
    authority: ClaimAuthority,
    bootstrap: BootstrapContext,
    *,
    request_id: str,
    now: datetime,
) -> ExecutionRequest:
    """Bind one Phase 3 match to already acknowledged runtime authority."""

    if not isinstance(match, CapabilityMatch):
        raise TypeError("match must be a CapabilityMatch")
    if not isinstance(authority, ClaimAuthority):
        raise TypeError("authority must be a ClaimAuthority")
    if not isinstance(bootstrap, BootstrapContext):
        raise TypeError("bootstrap must be a BootstrapContext")

    _require_utc(now, "now")
    candidate = match.candidate
    requirements = match.requirements
    if match.worker_id != authority.worker_id:
        raise ValueError("match worker_id does not match claim authority worker_id")
    if authority.task != candidate.task or authority.role is not candidate.role:
        raise ValueError("claim authority task/role does not match candidate task/role")
    if (
        requirements.task != candidate.task
        or requirements.role is not candidate.role
        or requirements.candidate_fingerprint != candidate_fingerprint(candidate)
    ):
        raise ValueError("candidate requirements task/role/fingerprint do not match candidate")
    if not isinstance(candidate.entry_ref, str) or not candidate.entry_ref.strip():
        raise ValueError("candidate entry_ref is required for execution")
    if not requirements.observed_at <= now <= requirements.fresh_until:
        raise ValueError("candidate evidence is stale or from the future")

    return ExecutionRequest(
        schema_version=EXECUTION_REQUEST_SCHEMA_VERSION,
        request_id=request_id,
        authority=authority,
        entry_ref=candidate.entry_ref,
        evidence=ExecutionEvidence(
            schema_version=EXECUTION_REQUEST_SCHEMA_VERSION,
            source_ref=requirements.source_ref,
            candidate_fingerprint=requirements.candidate_fingerprint,
            observed_at=requirements.observed_at,
            fresh_until=requirements.fresh_until,
        ),
        required_capabilities=requirements.required_capabilities,
        required_environment=requirements.required_environment,
        bootstrap=bootstrap,
    )


class LaunchStatus(StrEnum):
    ACCEPTED = "LAUNCH_ACCEPTED"
    UNAVAILABLE = "LAUNCH_UNAVAILABLE"
    FAILED = "LAUNCH_FAILED"


class ReconciliationReason(StrEnum):
    CLAIM_PRESENT_LAUNCH_NOT_STARTED = "CLAIM_PRESENT_LAUNCH_NOT_STARTED"
    WORKER_DIED_BEFORE_ACKNOWLEDGE = "WORKER_DIED_BEFORE_ACKNOWLEDGE"


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    """Validated adapter result; failed starts remain explicitly recoverable."""

    schema_version: str
    request_id: str
    launch_status: LaunchStatus
    worker_id: str | None = None
    session_id: str | None = None
    reason: str | None = None
    reconciliation_reason: ReconciliationReason | None = None

    def __post_init__(self) -> None:
        if self.schema_version != EXECUTION_REQUEST_SCHEMA_VERSION:
            raise ValueError("dispatch outcome schema_version is unsupported")
        _require_nonempty(self.request_id, "request_id")
        try:
            launch_status = LaunchStatus(self.launch_status)
        except ValueError as exc:
            raise ValueError("launch_status is unsupported") from exc
        object.__setattr__(self, "launch_status", launch_status)
        if self.reconciliation_reason is not None:
            try:
                reconciliation_reason = ReconciliationReason(self.reconciliation_reason)
            except ValueError as exc:
                raise ValueError("reconciliation_reason is unsupported") from exc
            object.__setattr__(self, "reconciliation_reason", reconciliation_reason)

        if launch_status is LaunchStatus.ACCEPTED:
            _require_nonempty(self.worker_id, "worker_id")
            _require_nonempty(self.session_id, "session_id")
            if self.reason is not None or self.reconciliation_reason is not None:
                raise ValueError("accepted launch cannot carry failure or reconciliation evidence")
            return

        if self.worker_id is not None or self.session_id is not None:
            raise ValueError("non-accepted launch cannot return worker/session identity")
        _require_nonempty(self.reason, "reason")
        if self.reconciliation_reason is None:
            raise ValueError("non-accepted launch requires reconciliation evidence")

    @classmethod
    def accepted(cls, *, request_id: str, worker_id: str, session_id: str) -> "DispatchOutcome":
        return cls(
            schema_version=EXECUTION_REQUEST_SCHEMA_VERSION,
            request_id=request_id,
            launch_status=LaunchStatus.ACCEPTED,
            worker_id=worker_id,
            session_id=session_id,
        )

    @classmethod
    def unavailable(
        cls,
        *,
        request_id: str,
        reason: str,
    ) -> "DispatchOutcome":
        return cls(
            schema_version=EXECUTION_REQUEST_SCHEMA_VERSION,
            request_id=request_id,
            launch_status=LaunchStatus.UNAVAILABLE,
            reason=reason,
            reconciliation_reason=ReconciliationReason.CLAIM_PRESENT_LAUNCH_NOT_STARTED,
        )

    @classmethod
    def failed(
        cls,
        *,
        request_id: str,
        reason: str,
        reconciliation_reason: ReconciliationReason = ReconciliationReason.CLAIM_PRESENT_LAUNCH_NOT_STARTED,
    ) -> "DispatchOutcome":
        return cls(
            schema_version=EXECUTION_REQUEST_SCHEMA_VERSION,
            request_id=request_id,
            launch_status=LaunchStatus.FAILED,
            reason=reason,
            reconciliation_reason=reconciliation_reason,
        )

    @property
    def reconciliation_required(self) -> bool:
        return self.reconciliation_reason is not None


class DispatchProtocolError(RuntimeError):
    """The adapter returned an outcome that cannot be trusted as a start."""


class DispatchAdapterError(RuntimeError):
    """Base class for bounded adapter launch dispositions."""


class LaunchUnavailableError(DispatchAdapterError):
    """No worker/session transport is currently available."""


class LaunchFailedError(DispatchAdapterError):
    """A launch attempt failed before a usable worker/session started."""


class WorkerDiedBeforeAcknowledgeError(LaunchFailedError):
    """The worker started but died before it could acknowledge execution."""


class WorkerDispatchAdapter(Protocol):
    def start(self, request: ExecutionRequest) -> DispatchOutcome: ...


def _error_reason(error: BaseException) -> str:
    message = str(error).strip()
    return message or type(error).__name__


def dispatch_execution_request(
    adapter: WorkerDispatchAdapter,
    request: ExecutionRequest,
    *,
    now: datetime,
) -> DispatchOutcome:
    """Call one provider-neutral adapter without creating task authority."""

    if not isinstance(request, ExecutionRequest):
        raise TypeError("request must be an ExecutionRequest")
    if not request.is_fresh(now):
        raise DispatchProtocolError("execution request evidence is stale or from the future")
    start = getattr(adapter, "start", None)
    if not callable(start):
        raise TypeError("adapter must expose callable start(request)")

    try:
        outcome = start(request)
    except LaunchUnavailableError as exc:
        return DispatchOutcome.unavailable(
            request_id=request.request_id,
            reason=_error_reason(exc),
        )
    except WorkerDiedBeforeAcknowledgeError as exc:
        return DispatchOutcome.failed(
            request_id=request.request_id,
            reason=_error_reason(exc),
            reconciliation_reason=ReconciliationReason.WORKER_DIED_BEFORE_ACKNOWLEDGE,
        )
    except LaunchFailedError as exc:
        return DispatchOutcome.failed(
            request_id=request.request_id,
            reason=_error_reason(exc),
        )

    if not isinstance(outcome, DispatchOutcome):
        raise DispatchProtocolError("adapter returned an unsupported dispatch outcome")
    if outcome.request_id != request.request_id:
        raise DispatchProtocolError("adapter outcome request_id does not match request")
    if (
        outcome.launch_status is LaunchStatus.ACCEPTED
        and outcome.worker_id != request.authority.worker_id
    ):
        raise DispatchProtocolError("accepted outcome worker_id does not match claim authority")
    return outcome
