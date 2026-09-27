from __future__ import annotations

from typing import Callable, Protocol, TypeVar

from .engine import CoordinationError
from .model import MutationResult, Role, WaitReason


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
        except (TypeError, AttributeError, AdapterProtocolError):
            self._fence()
            raise
        self._claim_id = claim_id
        self._generation = generation
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
            self._require_same_authority(
                result,
                claim_id=claim_id,
                generation=generation,
                operation="acknowledge",
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
            self._require_same_authority(
                result,
                claim_id=claim_id,
                generation=generation,
                operation="renew",
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
        """Enter an evidence-backed, lease-bound WAITING state."""

        claim_id, generation = self._ensure_active()
        wait_reason = WaitReason(reason)
        if not evidence_ref.strip():
            raise ValueError("wait requires evidence_ref")
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
            self._require_same_authority(
                result,
                claim_id=claim_id,
                generation=generation,
                operation="wait",
            )
            return result
        except RuntimeError:
            self._fence()
            raise

    def resume(self, *, idempotency_key: str) -> MutationResult:
        """Return a live WAITING claim to RUNNING with the same authority."""

        claim_id, generation = self._ensure_active()
        try:
            result = self._gateway.mutate(
                operation="resume",
                payload={"claim_id": claim_id, "generation": generation},
                idempotency_key=idempotency_key,
            )
            self._require_same_authority(
                result,
                claim_id=claim_id,
                generation=generation,
                operation="resume",
            )
            return result
        except RuntimeError:
            self._fence()
            raise

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
            self._require_same_authority(
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
            return work(self)
        finally:
            self.release(idempotency_key=release_idempotency_key)
