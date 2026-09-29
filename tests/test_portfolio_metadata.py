from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from execution_coordinator.discovery import IssueDocument
from execution_coordinator.model import Role
from execution_coordinator.portfolio_metadata import (
    PortfolioMetadataError,
    parse_portfolio_metadata,
)
from execution_coordinator.query import ClaimCandidate
from execution_coordinator.ranking import (
    ControlPriority,
    ReadinessClass,
    candidate_fingerprint,
    portable_rank_class_key,
)

NOW = datetime(2026, 9, 29, 1, 20, tzinfo=timezone.utc)
BEGIN = "<!-- DEVFLOW_EXECUTION_CANDIDATES_V1_BEGIN -->"
END = "<!-- DEVFLOW_EXECUTION_CANDIDATES_V1_END -->"
PBEGIN = "<!-- DEVFLOW_EXECUTION_PORTFOLIO_METADATA_V1_BEGIN -->"
PEND = "<!-- DEVFLOW_EXECUTION_PORTFOLIO_METADATA_V1_END -->"
DIGEST = "sha256:" + "1" * 64


def candidate() -> ClaimCandidate:
    return ClaimCandidate(
        task="owner/repo#8",
        role=Role.IMPLEMENTER,
        entry_ref="https://github.com/owner/repo/issues/8",
        conflict_keys=(),
        scope_ready=True,
        blocked=False,
        requires_user_confirmation=False,
    )


def control_body(*, metadata=True, mutate_entry=None, priority="P2") -> str:
    c = candidate()
    projection = {
        "schema_version": 1,
        "source_ref": "kinoko34077/devflow#107",
        "repository": "owner/repo",
        "candidates": [{
            "task": c.task,
            "task_body_sha256": DIGEST,
            "task_work_status": "READY_FOR_IMPLEMENTATION",
            "entry_ref": c.entry_ref,
            "scope_ready": True,
            "blocked": False,
            "requires_user_confirmation": False,
            "roles": [{"role": "implementer", "next_action_tag": "IMPLEMENT"}],
        }],
    }
    entry = {
        "task": c.task,
        "role": "implementer",
        "task_body_sha256": DIGEST,
        "candidate_fingerprint": candidate_fingerprint(c),
        "controller_urgency": 70,
        "dependency_ready": True,
        "dependency_order": 2,
        "readiness_class": "IMPLEMENT",
        "ready_at": None,
        "required_capabilities": ["python", "tests"],
        "required_environment": ["windows"],
        "observed_at": "2026-09-29T01:15:00Z",
        "fresh_until": "2026-09-29T01:30:00Z",
    }
    if mutate_entry:
        mutate_entry(entry)
    portfolio = {
        "schema_version": "execution-portfolio-metadata.v1",
        "source_ref": "kinoko34077/devflow#107",
        "repository": "owner/repo",
        "entries": [entry],
    }
    extra = f"\n{PBEGIN}\n{json.dumps(portfolio)}\n{PEND}\n" if metadata else ""
    return (
        "## Repository\n\n`owner/repo`\n\n"
        "## Work Status\n\n`READY_FOR_IMPLEMENTATION`\n\n"
        "## Repository State\n\n`ACTIVE`\n\n"
        f"## Priority\n\n`{priority}`\n\n"
        "## Next Action\n\n`[IMPLEMENT] task`\n\n"
        f"{BEGIN}\n{json.dumps(projection)}\n{END}\n"
        + extra
    )


def control(body: str) -> IssueDocument:
    return IssueDocument(
        repository="kinoko34077/devflow",
        number=107,
        state="open",
        body=body,
        html_url="https://github.com/kinoko34077/devflow/issues/107",
        title="[REPO] repo",
        author_association="OWNER",
    )


class PortfolioMetadataTests(unittest.TestCase):
    def test_valid_companion_binds_digest_fingerprint_ranking_and_requirements(self):
        result = parse_portfolio_metadata(
            control(control_body()),
            candidates=(candidate(),),
            now=NOW,
        )
        self.assertEqual(1, len(result))
        [item] = result
        self.assertEqual(ControlPriority.P2, item.ranking.control_priority)
        self.assertEqual(ReadinessClass.IMPLEMENT, item.ranking.readiness_class)
        self.assertEqual(frozenset({"python", "tests"}), item.requirements.required_capabilities)
        self.assertEqual(frozenset({"windows"}), item.requirements.required_environment)
        self.assertEqual((2, 0, 30, 2, 1, 1, ""), portable_rank_class_key(item.ranking))

    def test_missing_companion_is_incomplete_for_ordinary_candidate(self):
        with self.assertRaisesRegex(PortfolioMetadataError, "portfolio metadata block is missing"):
            parse_portfolio_metadata(control(control_body(metadata=False)), candidates=(candidate(),), now=NOW)

    def test_stale_or_binding_mismatch_fails_closed(self):
        stale = lambda e: e.update(fresh_until="2026-09-29T01:19:59Z")
        with self.assertRaisesRegex(PortfolioMetadataError, "stale"):
            parse_portfolio_metadata(control(control_body(mutate_entry=stale)), candidates=(candidate(),), now=NOW)
        bad_digest = lambda e: e.update(task_body_sha256="sha256:" + "2" * 64)
        with self.assertRaisesRegex(PortfolioMetadataError, "task_body_sha256"):
            parse_portfolio_metadata(control(control_body(mutate_entry=bad_digest)), candidates=(candidate(),), now=NOW)
        bad_fp = lambda e: e.update(candidate_fingerprint="sha256:" + "3" * 64)
        with self.assertRaisesRegex(PortfolioMetadataError, "fingerprint"):
            parse_portfolio_metadata(control(control_body(mutate_entry=bad_fp)), candidates=(candidate(),), now=NOW)

    def test_duplicate_or_reversed_markers_fail_closed(self):
        body = control_body()
        with self.assertRaisesRegex(PortfolioMetadataError, "exactly one"):
            parse_portfolio_metadata(control(body + "\n" + PBEGIN), candidates=(candidate(),), now=NOW)
        start = body.index(PBEGIN)
        end = body.index(PEND) + len(PEND)
        block = body[start:end]
        reversed_block = block.replace(PBEGIN, "__B__").replace(PEND, PBEGIN).replace("__B__", PEND)
        with self.assertRaisesRegex(PortfolioMetadataError, "reversed"):
            parse_portfolio_metadata(control(body[:start] + reversed_block + body[end:]), candidates=(candidate(),), now=NOW)

    def test_unknown_fields_and_dependency_pair_are_rejected(self):
        extra = lambda e: e.update(guessed_from_prose=True)
        with self.assertRaisesRegex(PortfolioMetadataError, "unknown or missing"):
            parse_portfolio_metadata(control(control_body(mutate_entry=extra)), candidates=(candidate(),), now=NOW)
        bad_pair = lambda e: e.update(dependency_ready=False, dependency_order=1)
        with self.assertRaisesRegex(PortfolioMetadataError, "dependency_order"):
            parse_portfolio_metadata(control(control_body(mutate_entry=bad_pair)), candidates=(candidate(),), now=NOW)


class PortableRankTests(unittest.TestCase):
    def test_portable_key_preserves_higher_urgency_before_missing_and_lower(self):
        from execution_coordinator.ranking import RANKING_SCHEMA_VERSION, RankingMetadata
        def meta(urgency):
            return RankingMetadata(
                schema_version=RANKING_SCHEMA_VERSION,
                source_ref="kinoko34077/devflow#107", task="owner/repo#8", role=Role.IMPLEMENTER,
                candidate_fingerprint=candidate_fingerprint(candidate()), control_priority=ControlPriority.P2,
                controller_urgency=urgency, dependency_ready=True, dependency_order=2,
                readiness_class=ReadinessClass.IMPLEMENT, ready_at=None,
                observed_at=datetime(2026,9,29,1,15,tzinfo=timezone.utc),
                fresh_until=datetime(2026,9,29,1,30,tzinfo=timezone.utc),
            )
        self.assertLess(portable_rank_class_key(meta(90)), portable_rank_class_key(meta(10)))
        self.assertLess(portable_rank_class_key(meta(10)), portable_rank_class_key(meta(None)))


if __name__ == "__main__":
    unittest.main()
