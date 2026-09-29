from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from uuid import UUID, uuid4

from .agent import AgentSession, MutationGateway
from .auto_launch import (
    AutoLaunchKeys,
    ClaimedAutoLaunch,
    apply_launch_outcome,
    claim_for_auto_launch,
)
from .capability import CAPABILITY_SCHEMA_VERSION, WorkerProfile
from .controller_offer import (
    CONTROLLER_OFFER_SCHEMA_VERSION,
    ControllerOffer,
    OfferResponseCode,
    evaluate_controller_offer,
    select_controller_offer,
)
from .discovery import IssueDocument, IssueReader
from .execution_request import (
    AUTO_LAUNCH_EXECUTION_REQUEST_SCHEMA_VERSION,
    BOOTSTRAP_CONTEXT_SCHEMA_VERSION,
    BootstrapContext,
    DispatchOutcome,
    ExecutionRequest,
)
from .model import CoordinatorState, ExecutionState, Role, WaitReason
from .portfolio_runtime import read_portfolio_runtime
from .ranking import ControlPriority

CONTEXT_SCHEMA_VERSION = "controller-auto-launch-context.v1"
_DEFAULT_CONTEXT_DIR = ".controller-auto-launch"
_TASK_REF = re.compile(r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([1-9][0-9]*)$")
_SECRET = re.compile(
    r"(ghp_[A-Za-z0-9]+|github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]+|Bearer\s+[A-Za-z0-9._-]+|BEGIN [A-Z ]*PRIVATE KEY|ANTHROPIC_API_KEY\s*=)",
    re.IGNORECASE,
)


def _task_parts(task: str) -> tuple[str, int]:
    match = _TASK_REF.fullmatch(task)
    if match is None:
        raise ValueError("task must be an exact owner/repo#issue reference")
    return match.group(1), int(match.group(2))


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def _offer_dict(offer: ControllerOffer) -> dict[str, Any]:
    return {
        "schema_version": offer.schema_version,
        "task": offer.task,
        "role": offer.role.value,
        "source_ref": offer.source_ref,
        "candidate_fingerprint": offer.candidate_fingerprint,
        "control_priority": offer.control_priority.value,
        "controller_urgency": offer.controller_urgency,
        "rank_key": list(offer.rank_key),
        "required_capabilities": sorted(offer.required_capabilities),
        "required_environment": sorted(offer.required_environment),
        "conflict_keys": list(offer.conflict_keys),
        "risk": offer.risk,
        "authority_requirements": list(offer.authority_requirements),
    }


def _offer_from_dict(value: dict[str, Any]) -> ControllerOffer:
    return ControllerOffer(
        schema_version=value["schema_version"],
        task=value["task"],
        role=Role(value["role"]),
        source_ref=value["source_ref"],
        candidate_fingerprint=value["candidate_fingerprint"],
        control_priority=ControlPriority(value["control_priority"]),
        controller_urgency=value.get("controller_urgency"),
        rank_key=tuple(value.get("rank_key", ())),
        required_capabilities=frozenset(value.get("required_capabilities", ())),
        required_environment=frozenset(value.get("required_environment", ())),
        conflict_keys=tuple(value.get("conflict_keys", ())),
        risk=value.get("risk"),
        authority_requirements=tuple(value.get("authority_requirements", ())),
    )


def _request_dict(request: ExecutionRequest) -> dict[str, Any]:
    return {
        "schema_version": request.schema_version,
        "request_id": request.request_id,
        "authority": {
            "claim_id": request.authority.claim_id,
            "generation": request.authority.generation,
            "task": request.authority.task,
            "role": request.authority.role.value,
            "worker_id": request.authority.worker_id,
            "state": request.authority.state.value,
        },
        "entry_ref": request.entry_ref,
        "evidence": {
            "schema_version": request.evidence.schema_version,
            "source_ref": request.evidence.source_ref,
            "candidate_fingerprint": request.evidence.candidate_fingerprint,
            "observed_at": request.evidence.observed_at.isoformat(),
            "fresh_until": request.evidence.fresh_until.isoformat(),
        },
        "required_capabilities": sorted(request.required_capabilities),
        "required_environment": sorted(request.required_environment),
        "bootstrap": {
            "schema_version": request.bootstrap.schema_version,
            "context_ref": request.bootstrap.context_ref,
            "base_sha": request.bootstrap.base_sha,
            "branch": request.bootstrap.branch,
            "parameters": list(request.bootstrap.parameters),
        },
    }


def _keys(attempt_id: str) -> AutoLaunchKeys:
    return AutoLaunchKeys(
        claim=f"{attempt_id}:claim",
        acknowledge=f"{attempt_id}:ack",
        wait=f"{attempt_id}:wait-provider",
        resume=f"{attempt_id}:resume-provider",
        release=f"{attempt_id}:release",
        fail=f"{attempt_id}:fail",
    )


def _context_dict(
    context: ClaimedAutoLaunch,
    *,
    expected_session_id: str,
    base_sha: str,
    branch: str,
    target_repository: str,
    target_issue: int,
) -> dict[str, Any]:
    return {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "attempt_id": context.attempt_id,
        "task": context.task,
        "role": context.role.value,
        "worker_id": context.worker_id,
        "claim_id": context.claim_id,
        "generation": context.generation,
        "request_id": context.request_id,
        "candidate_fingerprint": context.candidate_fingerprint,
        "expected_session_id": expected_session_id,
        "base_sha": base_sha,
        "branch": branch,
        "target_repository": target_repository,
        "target_issue": target_issue,
    }


def _context_from_dict(value: dict[str, Any]) -> ClaimedAutoLaunch:
    if value.get("schema_version") != CONTEXT_SCHEMA_VERSION:
        raise ValueError("launch context schema_version is unsupported")
    return ClaimedAutoLaunch(
        attempt_id=value["attempt_id"],
        task=value["task"],
        role=Role(value["role"]),
        worker_id=value["worker_id"],
        claim_id=value["claim_id"],
        generation=int(value["generation"]),
        request_id=value["request_id"],
        candidate_fingerprint=value["candidate_fingerprint"],
    )


def _assert_no_secret(text: str) -> None:
    if _SECRET.search(text):
        raise ValueError("controller context/prompt contains secret-shaped material")


def _work_prompt(
    *,
    task: str,
    task_body: str,
    claim_id: str,
    generation: int,
    base_sha: str,
    branch: str,
) -> str:
    prompt = f"""Implement the bounded owning task below inside the already checked-out repository.\n\nOwning task: {task}\nClaim authority:\n- claim_id: {claim_id}\n- generation: {generation}\n- base_sha: {base_sha}\n- branch: {branch}\n\nOwning Issue scope / acceptance:\n{task_body}\n\nHard boundaries:\n- Modify only files needed by the owning task and run relevant tests.\n- Do not push.\n- Do not create or update pull requests.\n- Do not merge, release, deploy, or publish.\n- Do not create, read, rotate, or modify credentials, secrets, sessions, or permissions.\n- Do not rewrite shared history.\n- Stop if the task requires a Human/User/security boundary.\n"""
    _assert_no_secret(prompt)
    return prompt


def prepare_offer(
    control_documents: Iterable[IssueDocument],
    *,
    issue_reader: IssueReader,
    state_reader,
    now: datetime,
    context_dir: Path,
) -> dict[str, Any]:
    controls = tuple(control_documents)
    read = read_portfolio_runtime(
        controls,
        issue_reader=issue_reader,
        state_reader=state_reader,
        worker_id="controller-auto-v1",
        now=now,
    )
    offer = select_controller_offer(read)
    offer_path = context_dir / "offer.json"
    if offer is None:
        if offer_path.exists():
            offer_path.unlink()
        return {"has_offer": False, "target_repository": "", "target_issue": ""}
    repository, issue = _task_parts(offer.task)
    _write_json(offer_path, _offer_dict(offer))
    return {"has_offer": True, "target_repository": repository, "target_issue": issue}


def accept_and_claim(
    control_documents: Iterable[IssueDocument],
    *,
    issue_reader: IssueReader,
    state_reader,
    gateway: MutationGateway,
    now: datetime,
    context_dir: Path,
    preflight: dict[str, Any],
    base_sha: str,
    attempt_id: str,
    uuid_factory: Callable[[], UUID] = uuid4,
) -> dict[str, Any]:
    offer = _offer_from_dict(_read_json(context_dir / "offer.json"))
    provider_ready = all(
        preflight.get(name) is True
        for name in ("github_app_ready", "wif_ready", "repository_checkout")
    )
    if not provider_ready:
        return {"result": OfferResponseCode.REQUIRES_USER_AUTHORITY.value, "claim_id": None}
    capabilities = frozenset(preflight.get("capabilities", ()))
    environment = frozenset(preflight.get("environment", ()))
    session_id = str(uuid_factory())
    UUID(session_id)
    worker_id = "claude-" + session_id.replace("-", "")
    worker = WorkerProfile(
        schema_version=CAPABILITY_SCHEMA_VERSION,
        worker_id=worker_id,
        source_ref=f"worker:{worker_id}",
        capabilities=capabilities,
        environment=environment,
        observed_at=now,
        fresh_until=now + timedelta(minutes=60),
    )
    current = read_portfolio_runtime(
        tuple(control_documents),
        issue_reader=issue_reader,
        state_reader=state_reader,
        worker_id=worker_id,
        now=now,
    )
    code, match = evaluate_controller_offer(
        offer,
        worker,
        current_read=current,
        provider_ready=True,
        human_gate=False,
    )
    if code is not OfferResponseCode.ACCEPTED or match is None:
        return {"result": code.value, "claim_id": None}
    repository, issue = _task_parts(offer.task)
    task_document = issue_reader.read_issue(repository, issue)
    _assert_no_secret(task_document.body)
    if not isinstance(base_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", base_sha):
        raise ValueError("base_sha must be an exact 40-hex commit SHA")
    attempt8 = hashlib.sha256(attempt_id.encode("utf-8")).hexdigest()[:8]
    branch = f"agent/controller-{issue}-{attempt8}"
    bootstrap = BootstrapContext(
        schema_version=BOOTSTRAP_CONTEXT_SCHEMA_VERSION,
        context_ref=f"github-actions:{attempt_id}",
        base_sha=base_sha.lower(),
        branch=branch,
        parameters=(("provider", "claude-code"), ("session_id", session_id)),
    )
    keys = _keys(attempt_id)
    context, request = claim_for_auto_launch(
        match,
        gateway,
        bootstrap,
        request_id=f"{attempt_id}:launch",
        keys=keys,
        now=now,
    )
    prompt = _work_prompt(
        task=context.task,
        task_body=task_document.body,
        claim_id=context.claim_id,
        generation=context.generation,
        base_sha=base_sha.lower(),
        branch=branch,
    )
    context_dir.mkdir(parents=True, exist_ok=True)
    _write_json(
        context_dir / "launch-context.json",
        _context_dict(
            context,
            expected_session_id=session_id,
            base_sha=base_sha.lower(),
            branch=branch,
            target_repository=repository,
            target_issue=issue,
        ),
    )
    _write_json(context_dir / "execution-request.json", _request_dict(request))
    (context_dir / "bootstrap-prompt.txt").write_text(
        "Establish this Claude execution session only. Do not use tools or modify repository files. Reply READY.\n",
        encoding="utf-8",
    )
    (context_dir / "work-prompt.txt").write_text(prompt, encoding="utf-8")
    return {
        "result": OfferResponseCode.ACCEPTED.value,
        "claim_id": context.claim_id,
        "generation": context.generation,
        "session_id": session_id,
        "worker_id": worker_id,
        "branch": branch,
        "request_id": request.request_id,
    }


def _require_current_unexpired(
    context: ClaimedAutoLaunch,
    state: CoordinatorState,
    now: datetime,
):
    claim = state.claims.get(context.claim_id)
    if claim is None or claim.generation != context.generation:
        raise RuntimeError("auto-launch claim is not current")
    if claim.lease_until <= now.astimezone(timezone.utc):
        raise RuntimeError("auto-launch claim lease is expired")
    return claim


def reconcile_bootstrap(
    *,
    context_dir: Path,
    bootstrap_result: dict[str, Any],
    gateway: MutationGateway,
    current_state: CoordinatorState,
    now: datetime,
    evidence_ref: str,
) -> dict[str, Any]:
    stored = _read_json(context_dir / "launch-context.json")
    context = _context_from_dict(stored)
    _require_current_unexpired(context, current_state, now)
    expected = stored["expected_session_id"]
    status = bootstrap_result.get("status")
    session_id = bootstrap_result.get("session_id")
    if status == "success" and session_id == expected:
        outcome = DispatchOutcome.accepted(
            request_id=context.request_id,
            worker_id=context.worker_id,
            session_id=expected,
            schema_version=AUTO_LAUNCH_EXECUTION_REQUEST_SCHEMA_VERSION,
        )
    elif status == "failed" and session_id == expected and bootstrap_result.get("terminal") is True:
        outcome = DispatchOutcome.failed(
            request_id=context.request_id,
            reason=str(bootstrap_result.get("reason") or "provider ended before work"),
            schema_version=AUTO_LAUNCH_EXECUTION_REQUEST_SCHEMA_VERSION,
        )
    elif status == "unavailable" and bootstrap_result.get("session_started") is False:
        outcome = DispatchOutcome.unavailable(
            request_id=context.request_id,
            reason=str(bootstrap_result.get("reason") or "provider unavailable"),
            schema_version=AUTO_LAUNCH_EXECUTION_REQUEST_SCHEMA_VERSION,
        )
    else:
        outcome = DispatchOutcome.ambiguous(
            request_id=context.request_id,
            reason=str(bootstrap_result.get("reason") or "provider session evidence is missing or mismatched"),
            schema_version=AUTO_LAUNCH_EXECUTION_REQUEST_SCHEMA_VERSION,
        )
    transition = apply_launch_outcome(
        context,
        outcome,
        gateway=gateway,
        current_state=current_state,
        keys=_keys(context.attempt_id),
        evidence_ref=evidence_ref,
    )
    result = {
        "state": transition.state,
        "claim_id": transition.claim_id,
        "generation": transition.generation,
        "session_id": transition.session_id,
        "launch_status": outcome.launch_status.value,
    }
    _write_json(context_dir / "bootstrap-evidence.json", result)
    return result


def finalize_work(
    *,
    context_dir: Path,
    work_result: dict[str, Any],
    gateway: MutationGateway,
    current_state: CoordinatorState,
    now: datetime,
    evidence_ref: str,
) -> dict[str, Any]:
    stored = _read_json(context_dir / "launch-context.json")
    context = _context_from_dict(stored)
    claim = _require_current_unexpired(context, current_state, now)
    if claim.state is not ExecutionState.RUNNING:
        raise RuntimeError("work finalization requires current RUNNING authority")
    evidence_text = json.dumps(work_result, ensure_ascii=False, sort_keys=True)
    _assert_no_secret(evidence_text)
    evidence = {
        "schema_version": "controller-auto-launch-task-evidence.v1",
        "task": context.task,
        "claim_id": context.claim_id,
        "generation": context.generation,
        "evidence_ref": evidence_ref,
        "work_result": work_result,
    }
    _write_json(context_dir / "task-evidence.json", evidence)
    session = AgentSession.from_current_claim(
        gateway,
        claim,
        expected_task=context.task,
        expected_role=context.role,
        expected_worker_id=context.worker_id,
        now=now,
    )
    keys = _keys(context.attempt_id)
    if work_result.get("status") == "success":
        session.release(idempotency_key=keys.release)
        state = "RELEASED"
    else:
        reason = str(work_result.get("reason") or "provider work failed")[:300]
        session.fail(reason=reason, idempotency_key=keys.fail)
        state = "FAILED"
    return {"state": state, "claim_id": context.claim_id, "generation": context.generation}


def _emit_outputs(values: dict[str, Any]) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if not output:
        return
    with open(output, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            if value is None:
                value = ""
            if isinstance(value, bool):
                value = "true" if value else "false"
            handle.write(f"{key}={value}\n")


def _runtime():
    from .actions_gateway import ActionsMutationGateway
    from .bootstrap_pickup import list_control_documents
    from .discovery import GitHubIssueReader
    from .github_state import GitHubStateStore

    lane_token = os.environ["GITHUB_TOKEN"]
    read_token = os.environ.get("COORDINATOR_READ_TOKEN") or lane_token
    repository = os.environ["GITHUB_REPOSITORY"]
    state = GitHubStateStore(token=lane_token, repository=repository, issue_number=3)
    reader = GitHubIssueReader(token=read_token)
    controls = list_control_documents(read_token, reader)
    gateway = ActionsMutationGateway(token=lane_token, repository=repository, state_reader=state.load_body)
    return reader, state, controls, gateway


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - workflow integration
    from .snapshot import parse_issue_body

    parser = argparse.ArgumentParser(prog="python -m execution_coordinator.actions_controller")
    parser.add_argument("--context-dir", default=_DEFAULT_CONTEXT_DIR)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("prepare-offer")
    accept = sub.add_parser("accept-and-claim")
    accept.add_argument("--preflight", required=True)
    accept.add_argument("--base-sha", required=True)
    accept.add_argument("--attempt-id", required=True)
    reconcile = sub.add_parser("reconcile-bootstrap")
    reconcile.add_argument("--bootstrap-result", required=True)
    reconcile.add_argument("--evidence-ref", required=True)
    final = sub.add_parser("finalize-work")
    final.add_argument("--work-result", required=True)
    final.add_argument("--evidence-ref", required=True)
    args = parser.parse_args(argv)

    context_dir = Path(args.context_dir)
    reader, state, controls, gateway = _runtime()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    if args.command == "prepare-offer":
        result = prepare_offer(controls, issue_reader=reader, state_reader=state, now=now, context_dir=context_dir)
    elif args.command == "accept-and-claim":
        result = accept_and_claim(
            controls,
            issue_reader=reader,
            state_reader=state,
            gateway=gateway,
            now=now,
            context_dir=context_dir,
            preflight=_read_json(Path(args.preflight)),
            base_sha=args.base_sha,
            attempt_id=args.attempt_id,
        )
    elif args.command == "reconcile-bootstrap":
        result = reconcile_bootstrap(
            context_dir=context_dir,
            bootstrap_result=_read_json(Path(args.bootstrap_result)),
            gateway=gateway,
            current_state=parse_issue_body(state.load_body()),
            now=now,
            evidence_ref=args.evidence_ref,
        )
    else:
        result = finalize_work(
            context_dir=context_dir,
            work_result=_read_json(Path(args.work_result)),
            gateway=gateway,
            current_state=parse_issue_body(state.load_body()),
            now=now,
            evidence_ref=args.evidence_ref,
        )
    _emit_outputs(result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
