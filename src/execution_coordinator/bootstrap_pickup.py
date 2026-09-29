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

import argparse
import importlib
import json
import os
import platform
import secrets
import subprocess
import sys
import types
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
from .managed_frontier import ManagedFrontierResult, enumerate_managed_frontier
from .model import Role
from .query import ClaimabilityReason, ClaimCandidate, StateReader, get_state_result
from .portfolio_runtime import read_portfolio_runtime
from .ranking import candidate_fingerprint, portable_rank_class_key

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
    *,
    work_class: str | None = None,
) -> dict[str, Any]:
    key = (candidate.task, candidate.role)
    reason = claimability.get(key)
    independence_conflict = candidate.role is Role.REVIEWER and any(
        task == candidate.task and role in (Role.IMPLEMENTER, Role.RECOVERY)
        for task, role in held_by_worker
    )
    item = {
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
    if work_class is not None:
        item["work_class"] = work_class
    return item


@dataclass(frozen=True, slots=True)
class GatherInputs:
    target_repository: str | None
    worker_id: str
    control_documents: tuple[IssueDocument, ...]
    agents_md_read: bool
    published: frozenset[tuple[str, Role]] = frozenset()


def _frontier_projection_context(
    frontier: ManagedFrontierResult,
    *,
    worker_id: str,
) -> tuple[dict[tuple[str, Role], ClaimabilityReason], set[tuple[str, Role]]]:
    assert getattr(frontier, "read", None) is not None
    state = frontier.read.state.state
    held = {
        (claim.task, claim.role)
        for claim in state.claims.values()
        if claim.worker_id == worker_id
    }
    claimability = {
        (projection.candidate.task, projection.candidate.role): projection.reason
        for projection in frontier.claimability
    }
    return claimability, held


def gather_evidence(
    inputs: GatherInputs,
    *,
    issue_reader: IssueReader,
    state_reader: StateReader,
    now: datetime,
) -> tuple[dict[str, Any], tuple[ClaimCandidate, ...]]:
    """Build one GET-only bootstrap evidence snapshot.

    Repository scope preserves the accepted v1 path.  Portfolio scope uses all
    eligible trusted Control snapshots and requires complete accepted companion
    metadata for every ordinary fresh candidate; any gap fails the portfolio
    frontier closed rather than falling back to repository-scoped ordering.
    """

    controls = [control_summary(document) for document in inputs.control_documents]
    evidence: dict[str, Any] = {
        "schema_version": EVIDENCE_SCHEMA,
        "observed_at": _utc(now),
        "fresh_until": _utc(now + EVIDENCE_TTL),
        "agents_md_read": inputs.agents_md_read,
        "controls": controls,
        "coordinator_state_read": False,
        "frontier": {"complete": False, "candidates": []},
    }

    if inputs.target_repository is not None:
        matching = [
            (summary, document)
            for summary, document in zip(controls, inputs.control_documents)
            if summary["managed_repository"].casefold() == inputs.target_repository.casefold()
            and summary["state"] == "open"
        ]
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
        claimability, held = _frontier_projection_context(
            frontier, worker_id=inputs.worker_id
        )
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

    runtime = read_portfolio_runtime(
        inputs.control_documents,
        issue_reader=issue_reader,
        state_reader=state_reader,
        worker_id=inputs.worker_id,
        now=now,
    )
    frontier = runtime.frontier
    if frontier.read is None:
        return evidence, ()
    evidence["coordinator_state_read"] = True
    claimability, held = _frontier_projection_context(
        frontier, worker_id=inputs.worker_id
    )
    candidates = frontier.candidates

    projected: list[dict[str, Any]] = []
    if runtime.complete and runtime.ranked is not None:
        ranked_by_key = {
            (item.candidate.task, item.candidate.role): item
            for item in runtime.ranked.ranked
        }
        requirements_by_key = {
            (item.task, item.role): item for item in runtime.requirements
        }
        work_class_by_key = {
            (task, role): work_class
            for task, role, work_class in runtime.work_classes
        }
        for candidate in candidates:
            if candidate.role not in (Role.IMPLEMENTER, Role.REVIEWER, Role.RECOVERY):
                continue
            key = (candidate.task, candidate.role)
            if candidate.role is not Role.RECOVERY and key not in ranked_by_key:
                continue
            item = _candidate_evidence(
                candidate,
                claimability,
                held,
                inputs.published,
                work_class=work_class_by_key.get(key),
            )
            if candidate.role is not Role.RECOVERY:
                ranked_item = ranked_by_key[key]
                requirements = requirements_by_key[key]
                item["dependency_ready"] = ranked_item.metadata.dependency_ready
                item["rank_key"] = list(portable_rank_class_key(ranked_item.metadata))
                item["required_capabilities"] = sorted(requirements.required_capabilities)
                item["required_environment"] = sorted(requirements.required_environment)
            projected.append(item)

    evidence["frontier"] = {
        "complete": runtime.complete,
        "candidates": projected,
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
    target_repository: str | None,
    observation: dict[str, Any],
    work_intent: str | None,
    accepted_work_classes: tuple[str, ...] | None = None,
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
    if accepted_work_classes is not None:
        request["accepted_work_classes"] = list(accepted_work_classes)
        request = devflow_tools.chat_worker_bootstrap.normalize_request(request)
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


# ---------------------------------------------------------------------------
# Common entry command for already-open chats (devflow#199, #190 Phase E).
# Same command for every provider; provider differences only show up in the
# probe results.  The session file is chat-local identity, never task truth.
# ---------------------------------------------------------------------------



def _command_ok(runner: Callable[..., Any], command: list[str]) -> bool:
    try:
        return runner(command, capture_output=True, timeout=20).returncode == 0
    except Exception:
        return False


def probe_environment(
    *,
    token: str,
    devflow_tools: Any,
    repository_checkout: bool,
    runner: Callable[..., Any] = subprocess.run,
    system: Callable[[], str] = platform.system,
    fetch: Callable[[str], bool] | None = None,
) -> tuple[dict[str, bool], bool]:
    """Run the Phase B probes for real.  Returns (probes, agents_md_read)."""

    def default_fetch(url: str) -> bool:
        try:
            request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
            with urllib.request.urlopen(request, timeout=20) as response:
                response.read()
            return True
        except Exception:
            return False

    fetch = fetch or default_fetch
    agents = fetch(f"https://api.github.com/repos/{DEVFLOW}/contents/AGENTS.md")
    os_name = system()
    probes = {name: False for name in devflow_tools.chat_worker_profile.PROBES}
    probes.update(
        {
            "exec.python3": _command_ok(runner, [sys.executable, "--version"]),
            "exec.git": _command_ok(runner, ["git", "--version"]),
            "exec.node": _command_ok(runner, ["node", "--version"]),
            "exec.unittest": _command_ok(runner, [sys.executable, "-c", "import unittest"]),
            "fs.repository_checkout": repository_checkout,
            "os.linux": os_name == "Linux",
            "os.macos": os_name == "Darwin",
            "os.windows": os_name == "Windows",
            "net.github_api": agents,
            "lane.github_actions": agents and bool(token),
            "surface.github_read": agents,
            # A token is the only write/dispatch surface this command uses.
            "surface.github_write": agents and bool(token),
            "surface.coordinator_claim": agents and bool(token),
        }
    )
    return probes, agents


def load_session(path: Path, worker_system: str, *, now: datetime, devflow_tools: Any) -> dict[str, Any]:
    """Load or create the chat-local session file and advance the cycle."""

    data: dict[str, Any] = {}
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        if not str(data.get("worker_session_id", "")).startswith(worker_system + "-"):
            raise ValueError("session file belongs to another worker_system")
    else:
        data = {
            "worker_session_id": devflow_tools.chat_worker_profile.new_session_id(
                worker_system, now.replace(microsecond=0), secrets.token_hex(3)
            ),
            "cycle": 0,
        }
    data["cycle"] = int(data.get("cycle", 0)) + 1
    path.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8")
    return data


def load_devflow_tools(devflow_path: str) -> Any:
    root = str(Path(devflow_path).resolve())
    if root not in sys.path:
        sys.path.insert(0, root)
    return types.SimpleNamespace(
        chat_worker_profile=importlib.import_module("tools.chat_worker_profile"),
        chat_worker_bootstrap=importlib.import_module("tools.chat_worker_bootstrap"),
    )


def main(argv: list[str] | None = None) -> int:
    from .actions_gateway import ActionsMutationGateway
    from .discovery import GitHubIssueReader
    from .github_state import GitHubStateStore

    parser = argparse.ArgumentParser(prog="python -m execution_coordinator.bootstrap_pickup")
    sub = parser.add_subparsers(dest="command", required=True)
    pick = sub.add_parser("pickup", help="one discovery cycle for one repository or the managed portfolio")
    pick.add_argument("--target", default=None, help="owner/name of the managed repository; omit for portfolio scope")
    pick.add_argument("--worker-system", required=True, choices=("codex", "claude", "chatgpt"))
    pick.add_argument("--devflow", required=True, help="path to a devflow checkout (contract tools)")
    pick.add_argument("--session-file", default=".chat-worker-session.json")
    pick.add_argument("--intent", default=None, help="the user's broad instruction (audit only)")
    pick.add_argument("--work-class", dest="accepted_work_classes", action="append", default=None, help="accepted Stage-1 work class; repeat to accept multiple classes")
    pick.add_argument("--repository-checkout", action="store_true", help="a target working tree is present")
    pick.add_argument("--execute", action="store_true", help="claim + acknowledge a work disposition")
    rel = sub.add_parser("release", help="release a claim obtained by pickup --execute")
    rel.add_argument("--claim-id", required=True)
    rel.add_argument("--generation", required=True, type=int)
    rel.add_argument("--idempotency-key", required=True)
    args = parser.parse_args(argv)

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    ec = "kinoko34077/execution-coordinator"
    state = GitHubStateStore(token=token, repository=ec, issue_number=3)
    if args.command == "release":
        gateway = ActionsMutationGateway(token=token, repository=ec, state_reader=state.load_body)
        gateway.mutate(
            operation="release",
            payload={"claim_id": args.claim_id, "generation": args.generation},
            idempotency_key=args.idempotency_key,
        )
        print(json.dumps({"released": args.claim_id}))
        return 0

    tools = load_devflow_tools(args.devflow)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    session = load_session(Path(args.session_file), args.worker_system, now=now, devflow_tools=tools)
    probes, agents = probe_environment(token=token, devflow_tools=tools, repository_checkout=args.repository_checkout)
    observation = {
        "schema_version": tools.chat_worker_profile.OBSERVATION_SCHEMA,
        "worker_system": args.worker_system,
        "worker_session_id": session["worker_session_id"],
        "cycle": session["cycle"],
        "observed_at": _utc(now),
        "probes": probes,
    }
    reader = GitHubIssueReader(token=token)
    outcome = run_pickup(
        target_repository=args.target,
        observation=observation,
        work_intent=args.intent,
        accepted_work_classes=tuple(args.accepted_work_classes) if args.accepted_work_classes is not None else None,
        devflow_tools=tools,
        issue_reader=reader,
        state_reader=state,
        control_documents=list_control_documents(token, reader) if agents else (),
        agents_md_read=agents,
        now=now,
        gateway_factory=(lambda: ActionsMutationGateway(token=token, repository=ec, state_reader=state.load_body))
        if args.execute
        else None,
    )
    live = outcome.get("session")
    print(
        json.dumps(
            {
                "result": outcome["result"],
                "probes": probes,
                "claim_id": outcome["claim_id"],
                "generation": live.generation if live is not None else None,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
