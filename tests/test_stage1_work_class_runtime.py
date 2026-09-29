from __future__ import annotations

import hashlib
import json
import types
import unittest
from datetime import datetime, timezone

from execution_coordinator.bootstrap_pickup import GatherInputs, gather_evidence, run_pickup
from execution_coordinator.discovery import IssueDocument
from execution_coordinator.model import CoordinatorState, Role
from execution_coordinator.portfolio_metadata import PortfolioMetadataError, parse_portfolio_metadata
from execution_coordinator.query import ClaimCandidate
from execution_coordinator.ranking import candidate_fingerprint
from execution_coordinator.snapshot import render_issue_body

NOW = datetime(2026, 9, 29, 8, 15, tzinfo=timezone.utc)
CBEGIN = "<!-- DEVFLOW_EXECUTION_CANDIDATES_V1_BEGIN -->"
CEND = "<!-- DEVFLOW_EXECUTION_CANDIDATES_V1_END -->"
PBEGIN = "<!-- DEVFLOW_EXECUTION_PORTFOLIO_METADATA_V1_BEGIN -->"
PEND = "<!-- DEVFLOW_EXECUTION_PORTFOLIO_METADATA_V1_END -->"
TASK_BODY = "## Scope\n\nbounded maintenance task"


def _digest(body: str) -> str:
    return "sha256:" + hashlib.sha256(body.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def _doc(repository: str, number: int, body: str, *, title: str, state: str = "open") -> IssueDocument:
    return IssueDocument(
        repository=repository,
        number=number,
        state=state,
        body=body,
        html_url=f"https://github.com/{repository}/issues/{number}",
        title=title,
        author_association="OWNER",
    )


def _candidate() -> ClaimCandidate:
    return ClaimCandidate(
        task="owner/repo#8",
        role=Role.IMPLEMENTER,
        entry_ref="https://github.com/owner/repo/issues/8",
        conflict_keys=(),
        scope_ready=True,
        blocked=False,
        requires_user_confirmation=False,
    )


def _control(*, work_class: str | None = None) -> IssueDocument:
    candidate = _candidate()
    digest = _digest(TASK_BODY)
    projection = {
        "schema_version": 1,
        "source_ref": "kinoko34077/devflow#107",
        "repository": "owner/repo",
        "candidates": [
            {
                "task": candidate.task,
                "task_body_sha256": digest,
                "task_work_status": "READY_FOR_IMPLEMENTATION",
                "entry_ref": candidate.entry_ref,
                "scope_ready": True,
                "blocked": False,
                "requires_user_confirmation": False,
                "roles": [{"role": "implementer", "next_action_tag": "IMPLEMENT"}],
            }
        ],
    }
    entry = {
        "task": candidate.task,
        "role": "implementer",
        "task_body_sha256": digest,
        "candidate_fingerprint": candidate_fingerprint(candidate),
        "controller_urgency": None,
        "dependency_ready": True,
        "dependency_order": 0,
        "readiness_class": "IMPLEMENT",
        "ready_at": None,
        "required_capabilities": [],
        "required_environment": [],
        "observed_at": "2026-09-29T08:10:00Z",
        "fresh_until": "2026-09-29T08:20:00Z",
    }
    if work_class is not None:
        entry["work_class"] = work_class
    portfolio = {
        "schema_version": "execution-portfolio-metadata.v1",
        "source_ref": "kinoko34077/devflow#107",
        "repository": "owner/repo",
        "entries": [entry],
    }
    body = (
        "## Repository\n\n`owner/repo`\n\n"
        "## Work Status\n\n`READY_FOR_IMPLEMENTATION`\n\n"
        "## Repository State\n\n`ACTIVE`\n\n"
        "## Priority\n\n`P2`\n\n"
        "## Next Action\n\n`[IMPLEMENT] bounded maintenance`\n\n"
        f"{CBEGIN}\n{json.dumps(projection)}\n{CEND}\n\n"
        f"{PBEGIN}\n{json.dumps(portfolio)}\n{PEND}\n"
    )
    return _doc("kinoko34077/devflow", 107, body, title="[REPO] repo")


class _Reader:
    def __init__(self, *documents: IssueDocument):
        self.documents = {(d.repository, d.number): d for d in documents}

    def read_issue(self, repository: str, number: int) -> IssueDocument:
        return self.documents[(repository, number)]


class _StateReader:
    def __init__(self):
        self.body = render_issue_body("# state\n", CoordinatorState.empty())

    def load_body(self) -> str:
        return self.body


class Stage1PortfolioWorkClassTests(unittest.TestCase):
    def test_explicit_work_class_is_preserved_by_portfolio_parser(self):
        result = parse_portfolio_metadata(
            _control(work_class="quickfix"),
            candidates=(_candidate(),),
            now=NOW,
        )
        [item] = result
        self.assertEqual("quickfix", item.work_class)

    def test_omitted_work_class_remains_backward_compatible(self):
        result = parse_portfolio_metadata(
            _control(),
            candidates=(_candidate(),),
            now=NOW,
        )
        [item] = result
        self.assertIsNone(item.work_class)

    def test_unknown_work_class_fails_closed(self):
        with self.assertRaisesRegex(PortfolioMetadataError, "work_class"):
            parse_portfolio_metadata(
                _control(work_class="tiny-fix"),
                candidates=(_candidate(),),
                now=NOW,
            )

    def test_portfolio_bootstrap_evidence_emits_explicit_work_class(self):
        control = _control(work_class="quickfix")
        task = _doc("owner/repo", 8, TASK_BODY, title="Task")
        evidence, _ = gather_evidence(
            GatherInputs(
                target_repository=None,
                worker_id="chatgpt:s1",
                control_documents=(control,),
                agents_md_read=True,
            ),
            issue_reader=_Reader(control, task),
            state_reader=_StateReader(),
            now=NOW,
        )
        self.assertTrue(evidence["frontier"]["complete"])
        [item] = evidence["frontier"]["candidates"]
        self.assertEqual("quickfix", item["work_class"])

    def test_repository_scoped_legacy_evidence_does_not_invent_work_class(self):
        control = _control()
        task = _doc("owner/repo", 8, TASK_BODY, title="Task")
        evidence, _ = gather_evidence(
            GatherInputs(
                target_repository="owner/repo",
                worker_id="chatgpt:s1",
                control_documents=(control,),
                agents_md_read=True,
            ),
            issue_reader=_Reader(control, task),
            state_reader=_StateReader(),
            now=NOW,
        )
        self.assertTrue(evidence["frontier"]["complete"])
        [item] = evidence["frontier"]["candidates"]
        self.assertNotIn("work_class", item)


class Stage1AcceptedWorkClassRequestTests(unittest.TestCase):
    def _tools(self, seen: dict[str, object]):
        def build_request(observation, *, target_repository, work_intent, now):
            return {
                "schema_version": "chat-worker-bootstrap-request.v1",
                "target_repository": target_repository,
                "work_intent": work_intent,
                "worker_system": "chatgpt",
                "worker_session_id": "chatgpt-20260929T081500Z-abc123",
                "execution_attempt_id": "chatgpt-20260929T081500Z-abc123:c1",
                "capabilities": [],
                "environment": [],
                "tool_surfaces": [],
                "observed_at": "2026-09-29T08:15:00Z",
            }

        def normalize_request(request):
            normalized = dict(request)
            if "accepted_work_classes" in normalized:
                values = normalized["accepted_work_classes"]
                if not values or len(values) != len(set(values)):
                    raise ValueError("invalid accepted_work_classes")
                normalized["accepted_work_classes"] = sorted(values)
            seen["normalized"] = normalized
            return normalized

        def classify(request, evidence):
            seen["classified"] = request
            return {"claim_required": False, "disposition": "NO_ELIGIBLE_WORK"}

        return types.SimpleNamespace(
            chat_worker_profile=types.SimpleNamespace(build_request=build_request),
            chat_worker_bootstrap=types.SimpleNamespace(
                normalize_request=normalize_request,
                classify=classify,
                validate_result=lambda result: None,
            ),
        )

    def test_run_pickup_threads_explicit_accepted_work_classes_into_request(self):
        seen: dict[str, object] = {}
        control = _control()
        task = _doc("owner/repo", 8, TASK_BODY, title="Task")
        outcome = run_pickup(
            target_repository="owner/repo",
            observation={},
            work_intent="軽い保守だけ",
            accepted_work_classes=("quickfix", "sync-check"),
            devflow_tools=self._tools(seen),
            issue_reader=_Reader(control, task),
            state_reader=_StateReader(),
            control_documents=(control,),
            agents_md_read=True,
            now=NOW,
        )
        self.assertEqual(["quickfix", "sync-check"], outcome["request"]["accepted_work_classes"])
        self.assertEqual(outcome["request"], seen["classified"])

    def test_run_pickup_omits_constraint_when_not_requested(self):
        seen: dict[str, object] = {}
        control = _control()
        task = _doc("owner/repo", 8, TASK_BODY, title="Task")
        outcome = run_pickup(
            target_repository="owner/repo",
            observation={},
            work_intent="なんか作業して",
            accepted_work_classes=None,
            devflow_tools=self._tools(seen),
            issue_reader=_Reader(control, task),
            state_reader=_StateReader(),
            control_documents=(control,),
            agents_md_read=True,
            now=NOW,
        )
        self.assertNotIn("accepted_work_classes", outcome["request"])


if __name__ == "__main__":
    unittest.main()