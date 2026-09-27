from __future__ import annotations

from typing import Callable, Protocol, TypeVar

from .engine import CoordinationError
from .model import ExecutionState, MutationResult, Role, WaitReason


class MutationGateway(Protocol):
    """Transport boundary for the serialized coordinator mutation lane."""

    def mutate(
        self,
        *,
        operation: str,
        payload: dict[str, object],
        idempotency_key: str,
    ) -> MutationResult: ...


T = TypeVar("T")


class AdapterProtocolError(RuntimeError):
    """The mutation lane returned a response that cannot carry authority."""


class AgentSession:
    """Keep one agent's claim authority fenced and explicit.

    The session deliberately does not discover work or generate idempotency
    keys. The caller owns the logical retry key and this adapter forwards it
    unchanged to the existing serialized mutation authority.
    """

    def __init__(
        self,
        gateway: MutationGateway,
        *,
        task: str,
        role: Role | str,
        worker_id: str,
        conflict_keys: tuple[str, ...] = (),
        base_sha: str | None = None,
        branch: str | None = None,
    ) -> None:
        if not task.strip():
            raise ValueError("task must not be empty")
        if not worker_id.strip():
            raise ValueError("worker_id must not be empty")
        self._gateway = gateway
        self._task = task
        self._role = Role(role)
        self._worker_id = worker_id
        self._conflict_keys = tuple(conflict_keys)
        self._base_sha = base_sha
        self._branch = branch
        self._claim_id: str | None = None
        self._generation: int | None = None
        self._execution_state: ExecutionState | None = None
        self._fenced = False
        self._released = False

    @property
    def claim_id(self) -> str | None:
        return self._claim_id

    @property
    def generation(self) -> int | None:
        return self._generation

    def _ensure_claimable(self) -> None:
        if self._fenced:
            raise RuntimeError("agent session is fenced")
        if self._released:
            raise RuntimeError("agent session has already released its claim")

    def _ensure_active(self) -> tuple[str, int]:
        self._ensure_claimable()
        if self._claim_id is None or self._generation is None:
            raise RuntimeError("agent session has no active claim")
        return self._claim_id, self._generation

    def _fence(self) -> None:
        self._claim_id = None
        self._generation = None
        self._execution_state = None
        self._fenced = True

    @staticmethod
    def _require_authority(result: MutationResult) -> tuple[str, int]:
        if result.claim_id is None or result.generation is None:
            raise AdapterProtocolError("mutation response did not return claim authority")
        return result.claim_id, result.generation

    @classmethod
    def _require_same_authority(
        cls,
        result: MutationResult,
        *,
        claim_id: str,
        generation: int,
        operation: str,
    ) -> None:
        try:
            returned_claim_id, returned_generation = cls._require_authority(result)
        except (TypeError, AttributeError, AdapterProtocolError) as exc:
            raise AdapterProtocolError(
                f"{operation} response did not return usable claim authority"
            ) from exc
        if (returned_claim_id, returned_generation) != (claim_id, generation):
            raise AdapterProtocolError(f"{operation} response changed claim authority")

    @classmethod
    def _require_live_claim(
        cls,
        result: MutationResult,
        *,
        claim_id: str,
        generation: int,
        operation: str,
        expected_state: ExecutionState | None = None,
    ) -> ExecutionState:
        cls._require_same_authority(
            result,
            claim_id=claim_id,
            generation=generation,
            operation=operation,
        )
        try:
            claim = result.state.claims.get(claim_id)
            returned_generation = claim.generation if claim is not None else None
            returned_state = claim.state if claim is not None else None
        except (AttributeError, TypeError) as exc:
            raise AdapterProtocolError(
                f"{operation} response did not return a usable live claim"
            ) from exc
        if (
            claim is None
            or returned_generation != generation
            or not isinstance(returned_state, ExecutionState)
        ):
            raise AdapterProtocolError(
                f"{operation} response did not return the current live claim"
            )
        if expected_state is not None and returned_state is not expected_state:
            raise AdapterProtocolError(
                f"{operation} response returned unexpected execution state"
            )
        return returned_state

    @classmethod
    def _require_terminal(
        cls,
        result: MutationResult,
        *,
        claim_id: str,
        generation: int,
        operation: str,
    ) -> None:
        cls._require_same_authority(
            result,
            claim_id=claim_id,
            generation=generation,
            operation=operation,
        )
        try:
            still_live = claim_id in result.state.claims
        except (AttributeError, TypeError) as exc:
            raise AdapterProtocolError(
                f"{operation} response did not return a usable terminal state"
            ) from exc
        if still_live:
            raise AdapterProtocolError(
                f"{operation} response retained the terminal claim"
            )

    def claim(self, *, idempotency_key: str) -> MutationResult:
        self._ensure_claimable()
        result = self._gateway.mutate(
            operation="claim",
            payload={
                "task": self._task,
                "role": self._role.value,
                "worker_id": self._worker_id,
                "conflict_keys": list(self._conflict_keys),
                "base_sha": self._base_sha,
                "branch": self._branch,
            },
            idempotency_key=idempotency_key,
        )
        try:
            claim_id, generation = self._require_authority(result)
            execution_state = self._require_live_claim(
                result,
                claim_id=claim_id,
                generation=generation,
                operation="claim",
                expected_state=ExecutionState.CLAIMED,
            )
        except (TypeError, AttributeError, AdapterProtocolError):
            self._fence()
            raise
        self._claim_id = claim_id
        self._generation = generation
        self._execution_state = execution_state
        return result

    def acknowledge(self, *, idempotency_key: str) -> MutationResult:
        """Transition this session from CLAIMED to RUNNING before work."""

        claim_id, generation = self._ensure_active()
        try:
            result = self._gateway.mutate(
                operation="acknowledge",
                payload={"claim_id": claim_id, "generation": generation},
                idempotency_key=idempotency_key,
            )
            self._execution_state = self._require_live_claim(
                result,
                claim_id=claim_id,
                generation=generation,
                operation="acknowledge",
                expected_state=ExecutionState.RUNNING,
            )
            return result
        except RuntimeError:
            # A failed or ambiguous acknowledge means this session no longer
            # has a safe local authority assumption. Do not attempt an
            # unsafe compensating release: remote authority remains lease-bound.
            self._fence()
            raise

    def renew(self, *, idempotency_key: str) -> MutationResult:
        claim_id, generation = self._ensure_active()
        try:
            result = self._gateway.mutate(
                operation="renew",
                payload={"claim_id": claim_id, "generation": generation},
                idempotency_key=idempotency_key,
            )
            self._execution_state = self._require_live_claim(
                result,
                claim_id=claim_id,
                generation=generation,
                operation="renew",
            )
            return result
        except (CoordinationError, AdapterProtocolError):
            self._fence()
            raise

    def progress(self, *, idempotency_key: str) -> MutationResult:
        claim_id, generation = self._ensure_active()
        try:
            result = self._gateway.mutate(
                operation="progress",
                payload={"claim_id": claim_id, "generation": generation},
                idempotency_key=idempotency_key,
            )
            self._execution_state = self._require_live_claim(
                result,
                claim_id=claim_id,
                generation=generation,
                operation="progress",
            )
            return result
        except (CoordinationError, AdapterProtocolError):
            self._fence()
            raise

    def wait(
        self,
        *,
        reason: WaitReason | str,
        evidence_ref: str,
        idempotency_key: str,
    ) -> MutationResult:
        wait_reason = WaitReason(reason)
        if not evidence_ref.strip():
            raise ValueError("wait requires evidence_ref")
        claim_id, generation = self._ensure_active()
        try:
            result = self._gateway.mutate(
                operation="wait",
                payload={
                    "claim_id": claim_id,
                    "generation": generation,
                    "reason": wait_reason.value,
                    "evidence_ref": evidence_ref,
                },
                idempotency_key=idempotency_key,
            )
            self._execution_state = self._require_live_claim(
                result,
                claim_id=claim_id,
                generation=generation,
                operation="wait",
                expected_state=ExecutionState.WAITING,
            )
            return result
        except (CoordinationError, AdapterProtocolError):
            self._fence()
            raise

    def resume(self, *, idempotency_key: str) -> MutationResult:
        claim_id, generation = self._ensure_active()
        if self._execution_state is not ExecutionState.WAITING:
            raise RuntimeError("resume requires WAITING state")
        try:
            result = self._gateway.mutate(
                operation="resume",
                payload={"claim_id": claim_id, "generation": generation},
                idempotency_key=idempotency_key,
            )
            self._execution_state = self._require_live_claim(
                result,
                claim_id=claim_id,
                generation=generation,
                operation="resume",
                expected_state=ExecutionState.RUNNING,
            )
            return result
        except (CoordinationError, AdapterProtocolError):
            self._fence()
            raise

    def fail(self, *, reason: str, idempotency_key: str) -> MutationResult:
        claim_id, generation = self._ensure_active()
        try:
            result = self._gateway.mutate(
                operation="fail",
                payload={
                    "claim_id": claim_id,
                    "generation": generation,
                    "reason": reason,
                },
                idempotency_key=idempotency_key,
            )
            self._require_terminal(
                result,
                claim_id=claim_id,
                generation=generation,
                operation="fail",
            )
        except (CoordinationError, AdapterProtocolError):
            self._fence()
            raise
        self._claim_id = None
        self._generation = None
        self._execution_state = None
        self._released = True
        return result

    def release(self, *, idempotency_key: str) -> MutationResult | None:
        if self._released:
            return None
        claim_id, generation = self._ensure_active()
        try:
            result = self._gateway.mutate(
                operation="release",
                payload={"claim_id": claim_id, "generation": generation},
                idempotency_key=idempotency_key,
            )
            self._require_terminal(
                result,
                claim_id=claim_id,
                generation=generation,
                operation="release",
            )
        except (CoordinationError, AdapterProtocolError):
            self._fence()
            raise
        self._claim_id = None
        self._generation = None
        self._execution_state = None
        self._released = True
        return result

    def run(
        self,
        work: Callable[["AgentSession"], T],
        *,
        claim_idempotency_key: str,
        acknowledge_idempotency_key: str,
        release_idempotency_key: str,
    ) -> T:
        """Run work only after claim and acknowledge succeed."""

        self.claim(idempotency_key=claim_idempotency_key)
        self.acknowledge(idempotency_key=acknowledge_idempotency_key)
        try:
            result = work(self)
        except BaseException as work_error:
            if self.claim_id is not None:
                try:
                    self.release(idempotency_key=release_idempotency_key)
                except BaseException as release_error:
                    work_error.add_note(
                        f"release after work failure failed: {release_error}"
                    )
            raise
        self.release(idempotency_key=release_idempotency_key)
        return result
