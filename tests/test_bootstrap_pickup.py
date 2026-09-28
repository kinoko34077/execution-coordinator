from __future__ import annotations

import hashlib
import json
import types
import unittest
from datetime import datetime, timezone

from execution_coordinator.bootstrap_pickup import (
    GatherInputs,
    SelectionChanged,
    control_summary,
    execute_selection,
    gather_evidence,
    run_pickup,
)
from execution_coordinator.discovery import IssueDocument
from execution_coordinator.engine import CoordinationError
from execution_coordinator.model import CoordinatorState, MutationResult, Role
from execution_coordinator.mutate import apply_mutation
from execution_coordinator.ranking import candidate_fingerprint
from execution_coordinator.snapshot import render_issue_body

NOW = datetime(2026, 9, 28, 16, 0, tzinfo=timezone.utc)
MARK_BEGIN = "<!-- DEVFLOW_EXECUTION_CANDIDATES_V1_BEGIN -->"
MARK_END = "<!-- DEVFLOW_EXECUTION_CANDIDATES_V1_END -->"
TASK_BODY = "## Scope\n\nbounded pilot task"


def _digest(body: str) -> str:
    return "sha256:" + hashlib.sha256(body.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def _doc(repository, number, body, *, title="Task", association="OWNER", state="open"):
    return IssueDocument(
        repository=repository,
        number=number,
        state=state,
        body=body,
        html_url=f"https://github.com/{repository}/issues/{number}",
        title=title,
        author_association=association,
    )


def _control(*, roles=("implementer",), status="READY_FOR_IMPLEMENTATION", next_action="`[IMPLEMENT] pilot`",
             work_status="AUDITED", task_body=TASK_BODY, repository_state="ACTIVE", association="OWNER"):
    tags = {"implementer": "IMPLEMENT", "reviewer": "REVIEW"}
    envelope = {
        "task": "owner/repo#8",
        "task_body_sha256": _digest(task_body),
        "task_work_status": status,
        "entry_ref": "https://github.com/owner/repo/issues/8",
        "scope_ready": True,
        "blocked": False,
        "requires_user_confirmation": False,
        "roles": [{"role": role, "next_action_tag": tags[role]} for role in roles],
    }
    projection = {"schema_version": 1, "source_ref": "kinoko34077/devflow#107", "repository": "owner/repo", "candidates": [envelope]}
    body = (
        "## Repository\n\n`owner/repo`\n\n"
        f"## Work Status\n\n`{work_status}`\n\n"
        f"## Repository State\n\n`{repository_state}`\n\n"
        f"## Next Action\n\n{next_action}\n\n"
        f"{MARK_BEGIN}\n{json.dumps(projection)}\n{MARK_END}\n"
    )
    return _doc("kinoko34077/devflow", 107, body, title="[REPO] repo", association=association)


class _Reader:
    def __init__(self, *documents):
        self.documents = {(d.repository, d.number): d for d in documents}

    def read_issue(self, repository, number):
        return self.documents[(repository, number)]


class _Store:
    def __init__(self):
        self.body = render_issue_body("# state\n", CoordinatorState.empty())

    def load_body(self):
        return self.body

    def save_body(self, body):
        self.body = body

    def add_comment(self, _body):
        pass


class _Gateway:
    def __init__(self, store):
        self.store = store
        self.calls = []

    def mutate(self, *, operation, payload, idempotency_key):
        self.calls.append(operation)
        return apply_mutation(self.store, operation=operation, payload=payload, idempotency_key=idempotency_key, now=NOW)


def _gather(control, *, task_body=TASK_BODY, store=None, worker="claude:s1", agents=True, published=frozenset()):
    store = store or _Store()
    reader = _Reader(control, _doc("owner/repo", 8, task_body))
    return gather_evidence(
        GatherInputs(target_repository="owner/repo", worker_id=worker, control_documents=(control,),
                     agents_md_read=agents, published=published),
        issue_reader=reader, state_reader=store, now=NOW,
    )


class GatherEvidenceTests(unittest.TestCase):
    def test_valid_control_projects_claimable_candidate(self):
        evidence, candidates = _gather(_control())
        self.assertEqual("chat-worker-bootstrap-evidence.v1", evidence["schema_version"])
        self.assertTrue(evidence["coordinator_state_read"])
        self.assertTrue(evidence["frontier"]["complete"])
        [item] = evidence["frontier"]["candidates"]
        self.assertEqual(("owner/repo#8", "implementer", "IMPLEMENT", "CLAIMABLE"),
                         (item["task_ref"], item["role"], item["action"], item["claimability"]))
        self.assertEqual(candidate_fingerprint(candidates[0]), item["fingerprint"])
        self.assertEqual("2026-09-28T16:10:00Z", evidence["fresh_until"])

    def test_control_summary_flags(self):
        self.assertTrue(control_summary(_control(next_action="`[HUMAN_GATE] decide`"))["human_gate"])
        self.assertTrue(control_summary(_control(work_status="BLOCKED"))["external_blocker"])
        self.assertFalse(control_summary(_control(association="NONE"))["trusted"])
        self.assertEqual("PARKED", control_summary(_control(repository_state="PARKED"))["repository_state"])

    def test_stale_digest_leaves_frontier_incomplete(self):
        evidence, candidates = _gather(_control(task_body="old body"), task_body=TASK_BODY)
        self.assertFalse(evidence["frontier"]["complete"])
        self.assertEqual((), candidates)

    def test_live_claim_is_reported_as_conflict(self):
        store = _Store()
        _Gateway(store).mutate(operation="claim", payload={"task": "owner/repo#8", "role": "implementer", "worker_id": "codex:other"}, idempotency_key="k")
        evidence, _ = _gather(_control(), store=store)
        self.assertEqual("BLOCKED_LIVE", evidence["frontier"]["candidates"][0]["claimability"])

    def test_reviewer_independence_conflict_for_same_worker(self):
        store = _Store()
        _Gateway(store).mutate(operation="claim", payload={"task": "owner/repo#8", "role": "implementer", "worker_id": "claude:s1"}, idempotency_key="k")
        evidence, _ = _gather(_control(roles=("reviewer",), status="AWAITING_REVIEW"), store=store)
        [item] = evidence["frontier"]["candidates"]
        self.assertEqual("reviewer", item["role"])
        self.assertTrue(item["reviewer_independence_conflict"])

    def test_published_by_this_attempt_is_marked(self):
        evidence, _ = _gather(_control(), published=frozenset({("owner/repo#8", Role.IMPLEMENTER)}))
        self.assertTrue(evidence["frontier"]["candidates"][0]["published_by_this_attempt"])

    def test_no_or_duplicate_control_gives_no_frontier(self):
        store = _Store()
        evidence, candidates = gather_evidence(
            GatherInputs(target_repository="other/repo", worker_id="claude:s1", control_documents=(_control(),), agents_md_read=True),
            issue_reader=_Reader(), state_reader=store, now=NOW,
        )
        self.assertFalse(evidence["coordinator_state_read"])
        self.assertEqual((), candidates)


class ExecuteSelectionTests(unittest.TestCase):
    def _result(self, candidate, **overrides):
        result = {
            "disposition": "CLAIM_AND_WORK", "claim_required": True, "task_ref": candidate.task,
            "role": candidate.role.value, "claim_candidate_fingerprint": candidate_fingerprint(candidate),
            "coordinator_worker_id": "claude:s1",
        }
        result.update(overrides)
        return result

    def test_claims_then_acknowledges_once(self):
        _, candidates = _gather(_control())
        store = _Store()
        gateway = _Gateway(store)
        session = execute_selection(self._result(candidates[0]), candidates, gateway, claim_key="a:claim", acknowledge_key="a:ack")
        self.assertEqual(["claim", "acknowledge"], gateway.calls)
        self.assertIsNotNone(session.claim_id)

    def test_changed_candidate_is_not_claimed(self):
        _, candidates = _gather(_control())
        gateway = _Gateway(_Store())
        with self.assertRaises(SelectionChanged):
            execute_selection(self._result(candidates[0], claim_candidate_fingerprint="sha256:" + "0" * 64),
                              candidates, gateway, claim_key="a:claim", acknowledge_key="a:ack")
        self.assertEqual([], gateway.calls)

    def test_non_work_disposition_is_refused(self):
        _, candidates = _gather(_control())
        with self.assertRaises(ValueError):
            execute_selection(self._result(candidates[0], disposition="NEEDS_HUMAN"), candidates, _Gateway(_Store()),
                              claim_key="a", acknowledge_key="b")

    def test_conflicting_claim_rejection_ends_cycle(self):
        _, candidates = _gather(_control())
        store = _Store()
        _Gateway(store).mutate(operation="claim", payload={"task": "owner/repo#8", "role": "implementer", "worker_id": "codex:other"}, idempotency_key="k")
        gateway = _Gateway(store)
        with self.assertRaises(CoordinationError):
            execute_selection(self._result(candidates[0]), candidates, gateway, claim_key="a:claim", acknowledge_key="a:ack")
        self.assertEqual(["claim"], gateway.calls)


class RunPickupWiringTests(unittest.TestCase):
    def test_read_only_cycle_delegates_classification(self):
        seen = {}

        def build_request(observation, *, target_repository, work_intent, now):
            return {"worker_system": "claude", "worker_session_id": "s1", "execution_attempt_id": "s1:c1", "target_repository": target_repository}

        def classify(request, evidence):
            seen["evidence"] = evidence
            return {"claim_required": False, "disposition": "NO_ELIGIBLE_WORK"}

        tools = types.SimpleNamespace(
            chat_worker_profile=types.SimpleNamespace(build_request=build_request),
            chat_worker_bootstrap=types.SimpleNamespace(classify=classify, validate_result=lambda result: None),
        )
        control = _control()
        outcome = run_pickup(
            target_repository="owner/repo", observation={}, work_intent="なんか作業して", devflow_tools=tools,
            issue_reader=_Reader(control, _doc("owner/repo", 8, TASK_BODY)), state_reader=_Store(),
            control_documents=(control,), agents_md_read=True, now=NOW,
        )
        self.assertIsNone(outcome["claim_id"])
        self.assertEqual(1, len(seen["evidence"]["frontier"]["candidates"]))


if __name__ == "__main__":
    unittest.main()
