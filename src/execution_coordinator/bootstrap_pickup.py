"""Repository-scoped broad-instruction pickup (devflow#190 Phase C).

Bridges an already-open chat worker to the accepted authorities:

* ``gather_evidence`` reads live devflow Controls, the accepted managed
  frontier and runtime state (GET-only) and emits
  ``chat-worker-bootstrap-evidence.v1`` for the devflow classifier
  (``tools/chat_worker_bootstrap.py``, the single classification authority).
* ``execute_selection`` turns a work disposition into the serialized
  claim -> acknowledge sequence, after re-observing that the selected
  candidate still exists with the same fingerprint.

No selection logic lives here; this module never picks a candidate itself.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from .agent import AgentSession, MutationGateway
from .discovery import (
    TRUSTED_AUTHOR_ASSOCIATIONS,
    DurableIssueSource,
    IssueDocument,
    IssueReader,
    _sections,
    _strip_code_value,
)
from .managed_frontier import enumerate_managed_frontier
from .model import Role
from .query import ClaimabilityReason, ClaimCandidate, StateReader, get_state_result
from .ranking import candidate_fingerprint

EVIDENCE_SCHEMA = "chat-worker-bootstrap-evidence.v1"
EVIDENCE_TTL = timedelta(minutes=10)
DEVFLOW = "kinoko34077/devflow"

# Accepted role -> default action tag when projecting a ClaimCandidate.
_ROLE_ACTION = {
    Role.IMPLEMENTER: "IMPLEMENT",
    Role.REVIEWER: "REVIEW",
    Role.RECOVERY: "RECOVERY_ASSESSMENT",
    Role.VERIFIER: "VERIFY",
    Role.INTEGRATOR: "MERGE",
}
_HUMAN_TOKENS = ("[HUMAN_GATE]", "[USER_DECISION]")


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def control_summary(document: IssueDocument) -> dict[str, Any]:
    """Project one devflow Control into bootstrap evidence (no authority added)."""

    sections = _sections(document.body)

    def section(name: str) -> str:
        # A duplicated section is ambiguous and therefore treated as absent.
        values = sections.get(name, [])
        return values[0] if len(values) == 1 else ""

    repository = _strip_code_value(section("repository")).strip()
    state = _strip_code_value(section("repository state")).strip()
    work_status = _strip_code_value(section("work status")).strip()
    next_action = section("next action")
    return {
        "ref": f"{document.repository}#{document.number}",
        "managed_repository": repository,
        "state": "open" if document.state.casefold() == "open" and not document.is_pull_request else "closed",
        "trusted": document.author_association in TRUSTED_AUTHOR_ASSOCIATIONS
        and document.title.strip() == f"[REPO] {repository.split('/', 1)[-1]}",
        "repository_state": state,
        "human_gate": any(token in next_action for token in _HUMAN_TOKENS),
        "external_blocker": work_status == "BLOCKED",
    }


def _candidate_evidence(
    candidate: ClaimCandidate,
    claimability: dict[tuple[str, Role], ClaimabilityReason],
    held_by_worker: set[tuple[str, Role]],
    published: frozenset[tuple[str, Role]],
) -> dict[str, Any]:
    key = (candidate.task, candidate.role)
    reason = claimability.get(key)
    independence_conflict = candidate.role is Role.REVIEWER and any(
        task == candidate.task and role in (Role.IMPLEMENTER, Role.RECOVERY)
        for task, role in held_by_worker
    )
    return {
        "task_ref": candidate.task,
        "role": candidate.role.value,
        "action": _ROLE_ACTION[candidate.role],
        "fingerprint": candidate_fingerprint(candidate),
        # Discovery already verified the exact owning-body digest; a stale
        # digest fails the source closed upstream (frontier incomplete).
        "digest_fresh": True,
        "dependency_ready": candidate.scope_ready,
        "human_gate": candidate.requires_user_confirmation,
        "external_blocker": candidate.blocked,
        "reviewer_independence_conflict": independence_conflict,
        "published_by_this_attempt": key in published,
        "claimability": reason.value if reason is not None else "BLOCKED_LIVE",
        # No accepted ranking metadata is published yet; the classifier then
        # falls back to canonical (task, role) order.
        "rank_key": [],
        # No accepted CandidateRequirements are published yet.
        "required_capabilities": [],
        "required_environment": [],
    }


@dataclass(frozen=True, slots=True)
class GatherInputs:
    target_repository: str
    worker_id: str
    control_documents: tuple[IssueDocument, ...]
    agents_md_read: bool
    published: frozenset[tuple[str, Role]] = frozenset()


def gather_evidence(
    inputs: GatherInputs,
    *,
    issue_reader: IssueReader,
    state_reader: StateReader,
    now: datetime,
) -> tuple[dict[str, Any], tuple[ClaimCandidate, ...]]:
    """Build bootstrap evidence for one target repository (GET-only)."""

    controls = [control_summary(document) for document in inputs.control_documents]
    matching = [
        (summary, document)
        for summary, document in zip(controls, inputs.control_documents)
        if summary["managed_repository"].casefold() == inputs.target_repository.casefold()
        and summary["state"] == "open"
    ]
    evidence: dict[str, Any] = {
        "schema_version": EVIDENCE_SCHEMA,
        "observed_at": _utc(now),
        "fresh_until": _utc(now + EVIDENCE_TTL),
        "agents_md_read": inputs.agents_md_read,
        "controls": controls,
        "coordinator_state_read": False,
        "frontier": {"complete": False, "candidates": []},
    }
    if len(matching) != 1:
        return evidence, ()
    document = matching[0][1]
    source = DurableIssueSource(document.repository, document.number)
    frontier = enumerate_managed_frontier(
        [source], issue_reader=issue_reader, state_reader=state_reader, now=now,
        worker_id=inputs.worker_id,
    )
    if frontier.read is None:
        return evidence, ()
    evidence["coordinator_state_read"] = True
    state = frontier.read.state.state
    held = {
        (claim.task, claim.role)
        for claim in state.claims.values()
        if claim.worker_id == inputs.worker_id
    }
    claimability = {
        (projection.candidate.task, projection.candidate.role): projection.reason
        for projection in frontier.claimability
    }
    candidates = frontier.candidates
    evidence["frontier"] = {
        "complete": not frontier.read.discovery.failures,
        "candidates": [
            _candidate_evidence(candidate, claimability, held, inputs.published)
            for candidate in candidates
            if candidate.role in (Role.IMPLEMENTER, Role.REVIEWER, Role.RECOVERY)
        ],
    }
    return evidence, candidates


class SelectionChanged(RuntimeError):
    """The selected candidate no longer matches live evidence; refresh."""


def execute_selection(
    result: dict[str, Any],
    live_candidates: Iterable[ClaimCandidate],
    gateway: MutationGateway,
    *,
    claim_key: str,
    acknowledge_key: str,
) -> AgentSession:
    """Claim then acknowledge the classifier's selection.  One claim only.

    Raises ``SelectionChanged`` without mutating when the re-observed
    candidate differs; a claim rejection propagates as ``CoordinationError``
    and ends the discovery cycle.
    """

    if result.get("disposition") not in ("CLAIM_AND_WORK", "REVIEW_WORK", "RECOVERY_WORK"):
        raise ValueError("result is not a work disposition")
    if result.get("claim_required") is not True:
        raise ValueError("work result must require a claim")
    role = Role(result["role"])
    matches = [
        candidate
        for candidate in live_candidates
        if candidate.task == result["task_ref"] and candidate.role is role
    ]
    if len(matches) != 1 or candidate_fingerprint(matches[0]) != result["claim_candidate_fingerprint"]:
        raise SelectionChanged("selected candidate changed since classification")
    candidate = matches[0]
    session = AgentSession(
        gateway,
        task=candidate.task,
        role=candidate.role,
        worker_id=result["coordinator_worker_id"],
        conflict_keys=candidate.conflict_keys,
    )
    session.claim(idempotency_key=claim_key)
    session.acknowledge(idempotency_key=acknowledge_key)
    return session


def list_control_documents(token: str, reader: IssueReader) -> tuple[IssueDocument, ...]:
    """Read every open ``[REPO]`` Control in devflow through the exact reader."""

    request = urllib.request.Request(
        f"https://api.github.com/repos/{DEVFLOW}/issues?state=open&per_page=100",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        issues = json.load(response)
    numbers = sorted(
        issue["number"]
        for issue in issues
        if isinstance(issue, dict)
        and str(issue.get("title", "")).startswith("[REPO] ")
        and "pull_request" not in issue
    )
    return tuple(reader.read_issue(DEVFLOW, number) for number in numbers)


def run_pickup(
    *,
    target_repository: str,
    observation: dict[str, Any],
    work_intent: str | None,
    devflow_tools: Any,
    issue_reader: IssueReader,
    state_reader: StateReader,
    control_documents: tuple[IssueDocument, ...],
    agents_md_read: bool,
    now: datetime,
    gateway_factory: Callable[[], MutationGateway] | None = None,
    published: frozenset[tuple[str, Role]] = frozenset(),
) -> dict[str, Any]:
    """One discovery cycle: profile -> evidence -> classify -> (claim+ack).

    ``devflow_tools`` provides ``chat_worker_profile`` and
    ``chat_worker_bootstrap`` from the devflow checkout: classification stays
    in devflow.  Without ``gateway_factory`` the cycle is read-only.
    """

    request = devflow_tools.chat_worker_profile.build_request(
        observation, target_repository=target_repository, work_intent=work_intent, now=now
    )
    worker_id = f"{request['worker_system']}:{request['worker_session_id']}"
    evidence, candidates = gather_evidence(
        GatherInputs(
            target_repository=target_repository,
            worker_id=worker_id,
            control_documents=control_documents,
            agents_md_read=agents_md_read,
            published=published,
        ),
        issue_reader=issue_reader,
        state_reader=state_reader,
        now=now,
    )
    result = devflow_tools.chat_worker_bootstrap.classify(request, evidence)
    devflow_tools.chat_worker_bootstrap.validate_result(result)
    outcome: dict[str, Any] = {"request": request, "evidence": evidence, "result": result, "claim_id": None}
    if gateway_factory is not None and result["claim_required"]:
        attempt = request["execution_attempt_id"]
        session = execute_selection(
            result,
            candidates,
            gateway_factory(),
            claim_key=f"{attempt}:claim",
            acknowledge_key=f"{attempt}:ack",
        )
        outcome["claim_id"] = session.claim_id
        outcome["session"] = session
    return outcome
