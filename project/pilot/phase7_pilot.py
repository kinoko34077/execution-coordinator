"""Bounded Phase 7 pilot (execution-coordinator#72).

Drives the accepted Agent-first cycle through the REAL serialized
``mutate-state.yml`` lane with synthetic, opt-in pilot tasks that point at
Issue #72.  Every claim created here is released before the script ends.

Usage:  PYTHONPATH=src GH_TOKEN=... python project/pilot/phase7_pilot.py
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone

from execution_coordinator.actions_gateway import ActionsMutationGateway
from execution_coordinator.agent import AgentSession
from execution_coordinator.autonomous import (
    AutonomousAttempt,
    AutonomousCycleKeys,
    run_autonomous_cycle,
)
from execution_coordinator.capability import (
    CAPABILITY_SCHEMA_VERSION,
    CandidateRequirements,
    CapabilityMatch,
    CapabilityMatchResult,
)
from execution_coordinator.discovery import DurableIssueSource, GitHubIssueReader
from execution_coordinator.github_state import GitHubStateStore
from execution_coordinator.managed_frontier import enumerate_managed_frontier
from execution_coordinator.model import Role
from execution_coordinator.query import ClaimCandidate
from execution_coordinator.ranking import candidate_fingerprint
from execution_coordinator.snapshot import parse_issue_body

REPO = "kinoko34077/execution-coordinator"
PILOT_ISSUE = 72
RUN = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
TOKEN = os.environ["GH_TOKEN"]

state_store = GitHubStateStore(token=TOKEN, repository=REPO, issue_number=3)
evidence: dict[str, object] = {"run": RUN}


def log(message: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {message}", flush=True)


def claims() -> dict:
    return dict(parse_issue_body(state_store.load_body()).claims)


class TimedGateway(ActionsMutationGateway):
    """Records wall-clock latency per real mutation."""

    timings: list[dict[str, object]] = []
    lock = threading.Lock()

    def mutate(self, *, operation, payload, idempotency_key):
        started = time.monotonic()
        outcome = "ok"
        try:
            return super().mutate(
                operation=operation, payload=payload, idempotency_key=idempotency_key
            )
        except BaseException as exc:
            outcome = type(exc).__name__
            raise
        finally:
            with self.lock:
                self.timings.append(
                    {
                        "operation": operation,
                        "worker": payload.get("worker_id"),
                        "seconds": round(time.monotonic() - started, 1),
                        "outcome": outcome,
                        "run_id": self.last_run_id,
                    }
                )


def gateway() -> TimedGateway:
    return TimedGateway(token=TOKEN, repository=REPO, state_reader=state_store.load_body)


def pilot_task(n: int) -> str:
    # Synthetic opt-in identities; entry_ref points at the pilot Issue.
    return f"{REPO}#{PILOT_ISSUE}0{n}"


def match(task: str, role: Role, worker: str) -> CapabilityMatch:
    candidate = ClaimCandidate(
        task=task,
        role=role,
        entry_ref=f"https://github.com/{REPO}/issues/{PILOT_ISSUE}",
        conflict_keys=(f"component:{REPO}:pilot/{task.rsplit('#', 1)[1]}",),
    )
    now = datetime.now(timezone.utc)
    requirements = CandidateRequirements(
        schema_version=CAPABILITY_SCHEMA_VERSION,
        source_ref="kinoko34077/devflow#107",
        task=task,
        role=role,
        candidate_fingerprint=candidate_fingerprint(candidate),
        required_capabilities=frozenset({"python"}),
        required_environment=frozenset({"github-actions-lane"}),
        observed_at=now,
        fresh_until=now + timedelta(minutes=30),
    )
    return CapabilityMatch(candidate=candidate, requirements=requirements, worker_id=worker)


def frontier(worker: str, *matches: CapabilityMatch) -> CapabilityMatchResult:
    return CapabilityMatchResult(
        worker_id=worker,
        matches=matches,
        recovery_candidates=(),
        omissions=(),
        ranking_omissions=(),
        source_failures=(),
        discovery_failures=(),
    )


def keys(label: str) -> AutonomousCycleKeys:
    return AutonomousCycleKeys(
        claim_idempotency_key=f"pilot7-{RUN}-{label}-claim",
        acknowledge_idempotency_key=f"pilot7-{RUN}-{label}-ack",
        release_idempotency_key=f"pilot7-{RUN}-{label}-release",
    )


def summarize(result) -> dict[str, object]:
    return {
        "worker": result.worker_id,
        "status": result.status.value,
        "selected": None if result.selected is None else [
            result.selected.candidate.task, result.selected.candidate.role.value
        ],
        "claim_attempts": result.claim_attempts,
        "attempt_id": result.attempt_id,
        "omissions": [[o.task, o.role.value, o.reason] for o in result.omissions],
        "rejection": result.rejection_reason,
    }


def live_frontier() -> None:
    request = urllib.request.Request(
        "https://api.github.com/repos/kinoko34077/devflow/issues?state=open&per_page=100",
        headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        issues = json.load(response)
    sources = [
        DurableIssueSource("kinoko34077/devflow", issue["number"])
        for issue in issues
        if issue["title"].startswith("[REPO] ") and "pull_request" not in issue
    ]
    result = enumerate_managed_frontier(
        sources,
        issue_reader=GitHubIssueReader(token=TOKEN),
        state_reader=state_store,
        now=datetime.now(timezone.utc),
    )
    failures = result.read.discovery.failures if result.read else ()
    reasons: dict[str, int] = {}
    for failure in failures:
        reasons[failure.reason] = reasons.get(failure.reason, 0) + 1
    evidence["live_frontier"] = {
        "sources": len(result.sources),
        "source_failures": len(result.source_failures),
        "candidates": len(result.candidates),
        "fresh": len(result.fresh_candidates),
        "recovery": len(result.recovery_candidates),
        "claimable": len(result.claimable),
        "discovery_failure_reasons": reasons,
    }
    log(f"live frontier: {evidence['live_frontier']}")


def race() -> None:
    task = pilot_task(1)
    results: dict[str, object] = {}

    def worker(name: str, delay: float) -> None:
        time.sleep(delay)
        result = run_autonomous_cycle(
            frontier(name, match(task, Role.IMPLEMENTER, name)),
            gateway(),
            keys=keys(f"race-{name}"),
            attempt=AutonomousAttempt(attempt_id=f"pilot7-{RUN}-race-{name}"),
            # Hold the claim long enough for the other worker's claim to be
            # serialized while this claim is live.
            work=lambda session: time.sleep(45) or "pilot-work-done",
        )
        results[name] = summarize(result)

    threads = [
        threading.Thread(target=worker, args=("pilot-worker-a", 0.0)),
        threading.Thread(target=worker, args=("pilot-worker-b", 1.0)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    evidence["race"] = results
    log(f"race: {json.dumps(results)}")


def reviewer_independence() -> None:
    task = pilot_task(2)
    implementer = AgentSession(
        gateway(), task=task, role=Role.IMPLEMENTER, worker_id="pilot-worker-a",
        conflict_keys=(f"component:{REPO}:pilot/{PILOT_ISSUE}02",),
    )
    implementer.claim(idempotency_key=f"pilot7-{RUN}-ri-impl-claim")
    implementer.acknowledge(idempotency_key=f"pilot7-{RUN}-ri-impl-ack")
    try:
        result = run_autonomous_cycle(
            frontier("pilot-worker-a", match(task, Role.REVIEWER, "pilot-worker-a")),
            gateway(),
            keys=keys("ri-review"),
            attempt=AutonomousAttempt(attempt_id=f"pilot7-{RUN}-ri"),
            work=lambda session: "must-not-run",
        )
        evidence["reviewer_independence"] = summarize(result)
    finally:
        implementer.release(idempotency_key=f"pilot7-{RUN}-ri-impl-release")
    log(f"reviewer independence: {evidence['reviewer_independence']}")


def publication_separation() -> None:
    published, other = pilot_task(3), pilot_task(4)
    result = run_autonomous_cycle(
        frontier(
            "pilot-worker-c",
            match(published, Role.IMPLEMENTER, "pilot-worker-c"),
            match(other, Role.IMPLEMENTER, "pilot-worker-c"),
        ),
        gateway(),
        keys=keys("3c"),
        attempt=AutonomousAttempt(
            attempt_id=f"pilot7-{RUN}-3c",
            published=frozenset({(published, Role.IMPLEMENTER)}),
        ),
        work=lambda session: "pilot-work-done",
    )
    evidence["publication_separation"] = summarize(result)
    log(f"3C: {evidence['publication_separation']}")


def main() -> int:
    before = claims()
    evidence["claims_before"] = sorted(before)
    if before:
        log(f"refusing to start: live claims exist {sorted(before)}")
        return 2
    try:
        live_frontier()
        race()
        reviewer_independence()
        publication_separation()
    finally:
        evidence["mutation_timings"] = TimedGateway.timings
        after = claims()
        evidence["claims_after"] = sorted(after)
        print("EVIDENCE " + json.dumps(evidence, sort_keys=True, default=str), flush=True)
    return 0 if not after else 1


if __name__ == "__main__":
    sys.exit(main())
