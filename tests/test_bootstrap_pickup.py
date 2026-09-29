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
from execution_coordinator.query import ClaimCandidate
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


def _portfolio_control(repository, control_number, task_number, *, priority="P2", metadata=True, next_action="`[IMPLEMENT] task`", ready_at=None):
    task_body = f"## Scope\n\n{repository} bounded task"
    role = Role.IMPLEMENTER
    task_ref = f"{repository}#{task_number}"
    entry_ref = f"https://github.com/{repository}/issues/{task_number}"
    candidate = ClaimCandidate(
        task=task_ref, role=role, entry_ref=entry_ref, conflict_keys=(),
        scope_ready=True, blocked=False, requires_user_confirmation=False,
    )
    source_ref = f"kinoko34077/devflow#{control_number}"
    envelope = {
        "task": task_ref,
        "task_body_sha256": _digest(task_body),
        "task_work_status": "READY_FOR_IMPLEMENTATION",
        "entry_ref": entry_ref,
        "scope_ready": True,
        "blocked": False,
        "requires_user_confirmation": False,
        "roles": [{"role": "implementer", "next_action_tag": "IMPLEMENT"}],
    }
    projection = {"schema_version": 1, "source_ref": source_ref, "repository": repository, "candidates": [envelope]}
    portfolio = {
        "schema_version": "execution-portfolio-metadata.v1",
        "source_ref": source_ref,
        "repository": repository,
        "entries": [{
            "task": task_ref, "role": "implementer", "task_body_sha256": _digest(task_body),
            "candidate_fingerprint": candidate_fingerprint(candidate), "controller_urgency": None,
            "dependency_ready": True, "dependency_order": task_number,
            "readiness_class": "IMPLEMENT", "ready_at": ready_at,
            "required_capabilities": ["python"], "required_environment": ["windows"],
            "observed_at": "2026-09-28T15:55:00Z", "fresh_until": "2026-09-28T16:10:00Z",
        }],
    }
    pblock = (
        "\n<!-- DEVFLOW_EXECUTION_PORTFOLIO_METADATA_V1_BEGIN -->\n"
        + json.dumps(portfolio)
        + "\n<!-- DEVFLOW_EXECUTION_PORTFOLIO_METADATA_V1_END -->\n"
    ) if metadata else ""
    body = (
        f"## Repository\n\n`{repository}`\n\n"
        "## Work Status\n\n`READY_FOR_IMPLEMENTATION`\n\n"
        "## Repository State\n\n`ACTIVE`\n\n"
        f"## Priority\n\n`{priority}`\n\n"
        f"## Next Action\n\n{next_action}\n\n"
        f"{MARK_BEGIN}\n{json.dumps(projection)}\n{MARK_END}\n"
        + pblock
    )
    name = repository.split("/", 1)[1]
    return (
        _doc("kinoko34077/devflow", control_number, body, title=f"[REPO] {name}"),
        _doc(repository, task_number, task_body),
        candidate,
    )


class PortfolioGatherEvidenceTests(unittest.TestCase):
    def test_null_target_builds_complete_two_repository_frontier(self):
        ca, ta, _ = _portfolio_control("owner/a", 201, 8, priority="P1")
        cb, tb, _ = _portfolio_control("owner/b", 202, 9, priority="P2")
        evidence, candidates = gather_evidence(
            GatherInputs(target_repository=None, worker_id="chatgpt:s1", control_documents=(ca, cb), agents_md_read=True),
            issue_reader=_Reader(ca, cb, ta, tb), state_reader=_Store(), now=NOW,
        )
        self.assertTrue(evidence["coordinator_state_read"])
        self.assertTrue(evidence["frontier"]["complete"])
        self.assertEqual(2, len(candidates))
        by_task = {item["task_ref"]: item for item in evidence["frontier"]["candidates"]}
        self.assertEqual([1, 1, 0, 8, 1, 1, ""], by_task["owner/a#8"]["rank_key"])
        self.assertEqual(["python"], by_task["owner/a#8"]["required_capabilities"])
        self.assertEqual(["windows"], by_task["owner/a#8"]["required_environment"])
        self.assertEqual([2, 1, 0, 9, 1, 1, ""], by_task["owner/b#9"]["rank_key"])

    def test_future_ready_at_is_filtered_by_existing_ranker_semantics(self):
        ca, ta, _ = _portfolio_control("owner/a", 201, 8, ready_at="2026-09-28T16:05:00Z")
        cb, tb, _ = _portfolio_control("owner/b", 202, 9)
        evidence, candidates = gather_evidence(
            GatherInputs(target_repository=None, worker_id="chatgpt:s1", control_documents=(ca, cb), agents_md_read=True),
            issue_reader=_Reader(ca, cb, ta, tb), state_reader=_Store(), now=NOW,
        )
        self.assertTrue(evidence["frontier"]["complete"])
        self.assertEqual(2, len(candidates))
        self.assertEqual(["owner/b#9"], [item["task_ref"] for item in evidence["frontier"]["candidates"]])

    def test_missing_metadata_on_one_published_candidate_makes_frontier_incomplete(self):
        ca, ta, _ = _portfolio_control("owner/a", 201, 8)
        cb, tb, _ = _portfolio_control("owner/b", 202, 9, metadata=False)
        evidence, _ = gather_evidence(
            GatherInputs(target_repository=None, worker_id="chatgpt:s1", control_documents=(ca, cb), agents_md_read=True),
            issue_reader=_Reader(ca, cb, ta, tb), state_reader=_Store(), now=NOW,
        )
        self.assertTrue(evidence["coordinator_state_read"])
        self.assertFalse(evidence["frontier"]["complete"])

    def test_human_gated_repository_is_not_enumerated_into_other_repository_work(self):
        ca, ta, _ = _portfolio_control("owner/a", 201, 8, next_action="`[HUMAN_GATE] decide`")
        cb, tb, _ = _portfolio_control("owner/b", 202, 9)
        evidence, candidates = gather_evidence(
            GatherInputs(target_repository=None, worker_id="chatgpt:s1", control_documents=(ca, cb), agents_md_read=True),
            issue_reader=_Reader(ca, cb, ta, tb), state_reader=_Store(), now=NOW,
        )
        self.assertTrue(evidence["frontier"]["complete"])
        self.assertEqual(["owner/b#9"], [item["task_ref"] for item in evidence["frontier"]["candidates"]])
        self.assertEqual(("owner/b#9",), tuple(candidate.task for candidate in candidates))


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



class PortfolioRunPickupWiringTests(unittest.TestCase):
    def test_null_target_delegates_complete_cross_repository_evidence(self):
        seen = {}

        def build_request(observation, *, target_repository, work_intent, now):
            self.assertIsNone(target_repository)
            return {
                "worker_system": "chatgpt", "worker_session_id": "s1",
                "execution_attempt_id": "s1:c1", "target_repository": None,
            }

        def classify(request, evidence):
            seen["request"] = request
            seen["evidence"] = evidence
            return {"claim_required": False, "disposition": "NO_ELIGIBLE_WORK"}

        tools = types.SimpleNamespace(
            chat_worker_profile=types.SimpleNamespace(build_request=build_request),
            chat_worker_bootstrap=types.SimpleNamespace(classify=classify, validate_result=lambda result: None),
        )
        ca, ta, _ = _portfolio_control("owner/a", 201, 8, priority="P1")
        cb, tb, _ = _portfolio_control("owner/b", 202, 9, priority="P2")
        outcome = run_pickup(
            target_repository=None, observation={}, work_intent="??????????????", devflow_tools=tools,
            issue_reader=_Reader(ca, cb, ta, tb), state_reader=_Store(),
            control_documents=(ca, cb), agents_md_read=True, now=NOW,
        )
        self.assertIsNone(outcome["claim_id"])
        self.assertTrue(seen["evidence"]["frontier"]["complete"])
        self.assertEqual(2, len(seen["evidence"]["frontier"]["candidates"]))

    def test_cli_pickup_target_is_optional_for_portfolio_scope(self):
        import inspect
        source = inspect.getsource(__import__("execution_coordinator.bootstrap_pickup", fromlist=["main"]).main)
        self.assertNotIn('pick.add_argument("--target", required=True', source)


class _Profile:
    PROBES = {name: None for name in (
        "exec.python3", "exec.git", "exec.node", "exec.unittest", "fs.repository_checkout",
        "os.linux", "os.macos", "os.windows", "net.github_api", "lane.github_actions",
        "surface.github_read", "surface.github_write", "surface.coordinator_claim",
    )}

    @staticmethod
    def new_session_id(system, started_at, entropy):
        return f"{system}-{started_at.strftime('%Y%m%dT%H%M%SZ')}-{entropy}"


class CommandTests(unittest.TestCase):
    tools = types.SimpleNamespace(chat_worker_profile=_Profile)

    def test_probes_reflect_real_checks_only(self):
        from execution_coordinator.bootstrap_pickup import probe_environment

        class _Done:
            def __init__(self, code):
                self.returncode = code

        def runner(command, **_kw):
            return _Done(0 if command[0] != "node" else 1)

        probes, agents = probe_environment(token="", devflow_tools=self.tools, repository_checkout=False,
                                           runner=runner, system=lambda: "Linux", fetch=lambda url: True)
        self.assertTrue(agents)
        self.assertTrue(probes["exec.python3"])
        self.assertFalse(probes["exec.node"])
        self.assertTrue(probes["os.linux"])
        # Without a token there is no write or claim surface, whatever the provider.
        self.assertFalse(probes["surface.github_write"])
        self.assertFalse(probes["surface.coordinator_claim"])

    def test_unreachable_github_yields_no_surfaces(self):
        from execution_coordinator.bootstrap_pickup import probe_environment

        probes, agents = probe_environment(token="t", devflow_tools=self.tools, repository_checkout=False,
                                           runner=lambda *a, **k: (_ for _ in ()).throw(OSError()),
                                           system=lambda: "Windows", fetch=lambda url: False)
        self.assertFalse(agents)
        self.assertFalse(any(probes[name] for name in probes if name.startswith(("surface.", "exec."))))
        self.assertTrue(probes["os.windows"])

    def test_session_file_is_created_once_and_cycle_advances(self):
        import tempfile
        from pathlib import Path

        from execution_coordinator.bootstrap_pickup import load_session

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.json"
            first = load_session(path, "codex", now=NOW, devflow_tools=self.tools)
            second = load_session(path, "codex", now=NOW, devflow_tools=self.tools)
            self.assertEqual(first["worker_session_id"], second["worker_session_id"])
            self.assertEqual((1, 2), (first["cycle"], second["cycle"]))
            self.assertTrue(first["worker_session_id"].startswith("codex-20260928T160000Z-"))
            with self.assertRaises(ValueError):
                load_session(path, "claude", now=NOW, devflow_tools=self.tools)


class PythonVersionGuardTests(unittest.TestCase):
    def test_old_interpreter_gets_clear_error(self):
        import subprocess
        import sys
        from pathlib import Path

        src = Path(__file__).resolve().parents[1] / "src"
        code = (
            "import sys; sys.version_info = (3, 10, 0, 'final', 0); sys.path.insert(0, %r)\n"
            "try:\n import execution_coordinator\nexcept RuntimeError as e:\n print(e)\n" % str(src)
        )
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
        self.assertIn("requires Python 3.11 or newer", out.stdout)
