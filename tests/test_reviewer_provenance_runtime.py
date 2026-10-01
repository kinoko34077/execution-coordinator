from __future__ import annotations

import json
import types
import unittest

from execution_coordinator.actions_pickup import (
    CommandRejected,
    build_observation,
    parse_command,
)
from execution_coordinator.bootstrap_pickup import GatherInputs, gather_evidence, run_pickup
from execution_coordinator.discovery import IssueDocument
from execution_coordinator.model import Role
from execution_coordinator.portfolio_metadata import (
    PortfolioMetadataError,
    parse_portfolio_metadata,
)
from execution_coordinator.query import ClaimCandidate
from execution_coordinator.ranking import candidate_fingerprint
from tests.test_bootstrap_pickup import NOW, _Reader, _Store, _digest

BEGIN = "<!-- DEVFLOW_EXECUTION_CANDIDATES_V1_BEGIN -->"
END = "<!-- DEVFLOW_EXECUTION_CANDIDATES_V1_END -->"
PBEGIN = "<!-- DEVFLOW_EXECUTION_PORTFOLIO_METADATA_V1_BEGIN -->"
PEND = "<!-- DEVFLOW_EXECUTION_PORTFOLIO_METADATA_V1_END -->"


class _Profile:
    OBSERVATION_SCHEMA = "chat-worker-observation.v1"
    PROBES = {}

    @staticmethod
    def new_session_id(system, started_at, entropy):
        return f"{system}-{started_at.strftime('%Y%m%dT%H%M%SZ')}-{entropy}"


def _review_control(*, malformed_requirement=False, metadata=True):
    repository = "owner/repo"
    task_number = 8
    task_ref = f"{repository}#{task_number}"
    task_body = "## Scope\n\nreview this exact change"
    entry_ref = f"https://github.com/{repository}/issues/{task_number}"
    candidate = ClaimCandidate(
        task=task_ref,
        role=Role.REVIEWER,
        entry_ref=entry_ref,
        conflict_keys=(),
        scope_ready=True,
        blocked=False,
        requires_user_confirmation=False,
    )
    projection = {
        "schema_version": 1,
        "source_ref": "kinoko34077/devflow#107",
        "repository": repository,
        "candidates": [{
            "task": task_ref,
            "task_body_sha256": _digest(task_body),
            "task_work_status": "AWAITING_REVIEW",
            "entry_ref": entry_ref,
            "scope_ready": True,
            "blocked": False,
            "requires_user_confirmation": False,
            "roles": [{"role": "reviewer", "next_action_tag": "REVIEW"}],
        }],
    }
    requirement = (
        {"implementer_system": "ChatGPT"}
        if malformed_requirement
        else {"implementer_system": "ChatGPT", "implementer_model": "GPT-5.6 Sol"}
    )
    portfolio = {
        "schema_version": "execution-portfolio-metadata.v1",
        "source_ref": "kinoko34077/devflow#107",
        "repository": repository,
        "entries": [{
            "task": task_ref,
            "role": "reviewer",
            "task_body_sha256": _digest(task_body),
            "candidate_fingerprint": candidate_fingerprint(candidate),
            "controller_urgency": None,
            "dependency_ready": True,
            "dependency_order": 8,
            "readiness_class": "REVIEW",
            "ready_at": None,
            "required_capabilities": [],
            "required_environment": [],
            "work_class": "formal-review",
            "different_reviewer_requirement": requirement,
            "observed_at": "2026-09-28T15:55:00Z",
            "fresh_until": "2026-09-28T16:10:00Z",
        }],
    }
    pblock = (
        f"\n{PBEGIN}\n{json.dumps(portfolio)}\n{PEND}\n"
        if metadata else ""
    )
    control_body = (
        "## Repository\n\n`owner/repo`\n\n"
        "## Work Status\n\n`AWAITING_REVIEW`\n\n"
        "## Repository State\n\n`ACTIVE`\n\n"
        "## Priority\n\n`P1`\n\n"
        "## Next Action\n\n`[REVIEW] exact head`\n\n"
        f"{BEGIN}\n{json.dumps(projection)}\n{END}\n"
        + pblock
    )
    control = IssueDocument(
        repository="kinoko34077/devflow",
        number=107,
        state="open",
        body=control_body,
        html_url="https://github.com/kinoko34077/devflow/issues/107",
        title="[REPO] repo",
        author_association="OWNER",
    )
    task = IssueDocument(
        repository=repository,
        number=task_number,
        state="open",
        body=task_body,
        html_url=entry_ref,
        title="Review task",
        author_association="OWNER",
    )
    return control, task, candidate


class ActionsReviewProvenanceTests(unittest.TestCase):
    def test_omitted_transport_pair_does_not_invent_review_provenance(self):
        _, fields = parse_command("/pickup\nworker_system: chatgpt")
        observation = build_observation(fields, _Profile, NOW)
        self.assertNotIn("review_provenance", observation)

    def test_explicit_transport_pair_builds_nested_review_provenance(self):
        body = (
            "/pickup\n"
            "worker_system: chatgpt\n"
            "reviewer_system: ChatGPT\n"
            "reviewer_model: GPT-5.6 Sol"
        )
        command, fields = parse_command(body)
        self.assertEqual("pickup", command)
        observation = build_observation(fields, _Profile, NOW)
        self.assertEqual(
            {"system": "ChatGPT", "model": "GPT-5.6 Sol"},
            observation["review_provenance"],
        )

    def test_transport_requires_both_review_signature_parts(self):
        for extra in (
            "reviewer_system: ChatGPT",
            "reviewer_model: GPT-5.6 Sol",
        ):
            with self.subTest(extra=extra):
                _, fields = parse_command(f"/pickup\nworker_system: chatgpt\n{extra}")
                with self.assertRaises(CommandRejected):
                    build_observation(fields, _Profile, NOW)


class RequestNormalizationRegressionTests(unittest.TestCase):
    def test_work_class_renormalization_drops_absent_review_provenance(self):
        seen = {}

        def build_request(observation, *, target_repository, work_intent, now):
            return {
                "schema_version": "chat-worker-bootstrap-request.v1",
                "target_repository": target_repository,
                "work_intent": work_intent,
                "worker_system": "chatgpt",
                "worker_session_id": "chatgpt-s1",
                "execution_attempt_id": "chatgpt-s1:c1",
                "capabilities": [],
                "environment": [],
                "tool_surfaces": [],
                "observed_at": "2026-09-28T16:00:00Z",
            }

        def normalize_request(request):
            normalized = dict(request)
            normalized["review_provenance"] = None
            return normalized

        def classify(request, evidence):
            seen["request"] = request
            if "review_provenance" in request and request["review_provenance"] is None:
                raise AssertionError("optional review_provenance None leaked into classifier")
            return {"claim_required": False, "disposition": "NO_ELIGIBLE_WORK"}

        tools = types.SimpleNamespace(
            chat_worker_profile=types.SimpleNamespace(build_request=build_request),
            chat_worker_bootstrap=types.SimpleNamespace(
                WORK_CLASSES=frozenset({"audit", "triage", "sync-check", "quickfix", "implementation", "formal-review"}),
                normalize_request=normalize_request,
                classify=classify,
                validate_result=lambda result: None,
            ),
        )
        run_pickup(
            target_repository="owner/repo",
            observation={},
            work_intent="test",
            accepted_work_classes=("implementation",),
            devflow_tools=tools,
            issue_reader=_Reader(),
            state_reader=_Store(),
            control_documents=(),
            agents_md_read=True,
            now=NOW,
        )
        self.assertNotIn("review_provenance", seen["request"])


class ReviewerRequirementMetadataTests(unittest.TestCase):
    def test_metadata_accepts_exact_reviewer_requirement(self):
        control, _, candidate = _review_control()
        [item] = parse_portfolio_metadata(control, candidates=(candidate,), now=NOW)
        self.assertEqual(
            ("ChatGPT", "GPT-5.6 Sol"),
            (
                item.different_reviewer_requirement.implementer_system,
                item.different_reviewer_requirement.implementer_model,
            ),
        )

    def test_malformed_reviewer_requirement_fails_closed(self):
        control, _, candidate = _review_control(malformed_requirement=True)
        with self.assertRaises(PortfolioMetadataError):
            parse_portfolio_metadata(control, candidates=(candidate,), now=NOW)

    def test_reviewer_requirement_is_reviewer_only(self):
        control, _, candidate = _review_control()
        body = control.body.replace(
            '"role": "reviewer", "task_body_sha256"',
            '"role": "implementer", "task_body_sha256"',
        )
        mutated = IssueDocument(
            repository=control.repository,
            number=control.number,
            state=control.state,
            body=body,
            html_url=control.html_url,
            title=control.title,
            author_association=control.author_association,
        )
        with self.assertRaises(PortfolioMetadataError):
            parse_portfolio_metadata(mutated, candidates=(candidate,), now=NOW)


class ReviewerRequirementEvidenceTests(unittest.TestCase):
    def test_repository_scope_without_requirement_preserves_legacy_reviewer(self):
        control, task, _ = _review_control(metadata=False)
        evidence, _ = gather_evidence(
            GatherInputs(
                target_repository="owner/repo",
                worker_id="chatgpt:s1",
                control_documents=(control,),
                agents_md_read=True,
            ),
            issue_reader=_Reader(control, task),
            state_reader=_Store(),
            now=NOW,
        )
        self.assertTrue(evidence["frontier"]["complete"])
        [item] = evidence["frontier"]["candidates"]
        self.assertNotIn("different_reviewer_requirement", item)

    def test_repository_scope_malformed_requirement_fails_closed(self):
        control, task, _ = _review_control(malformed_requirement=True)
        evidence, _ = gather_evidence(
            GatherInputs(
                target_repository="owner/repo",
                worker_id="chatgpt:s1",
                control_documents=(control,),
                agents_md_read=True,
            ),
            issue_reader=_Reader(control, task),
            state_reader=_Store(),
            now=NOW,
        )
        self.assertFalse(evidence["frontier"]["complete"])
        self.assertEqual([], evidence["frontier"]["candidates"])

    def test_repository_scope_projects_bound_reviewer_requirement(self):
        control, task, _ = _review_control()
        evidence, _ = gather_evidence(
            GatherInputs(
                target_repository="owner/repo",
                worker_id="chatgpt:s1",
                control_documents=(control,),
                agents_md_read=True,
            ),
            issue_reader=_Reader(control, task),
            state_reader=_Store(),
            now=NOW,
        )
        self.assertTrue(evidence["frontier"]["complete"])
        [item] = evidence["frontier"]["candidates"]
        self.assertEqual(
            {
                "implementer_system": "ChatGPT",
                "implementer_model": "GPT-5.6 Sol",
            },
            item["different_reviewer_requirement"],
        )

    def test_portfolio_scope_projects_bound_reviewer_requirement(self):
        control, task, _ = _review_control()
        evidence, _ = gather_evidence(
            GatherInputs(
                target_repository=None,
                worker_id="chatgpt:s1",
                control_documents=(control,),
                agents_md_read=True,
            ),
            issue_reader=_Reader(control, task),
            state_reader=_Store(),
            now=NOW,
        )
        self.assertTrue(evidence["frontier"]["complete"])
        [item] = evidence["frontier"]["candidates"]
        self.assertEqual(
            "ChatGPT",
            item["different_reviewer_requirement"]["implementer_system"],
        )


if __name__ == "__main__":
    unittest.main()
