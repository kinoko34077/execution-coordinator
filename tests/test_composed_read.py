from __future__ import annotations

import hashlib
import json
import unittest
from datetime import datetime, timedelta, timezone

from execution_coordinator.discovery import DurableIssueSource, IssueDocument
from execution_coordinator.engine import claim
from execution_coordinator.frontier import compose_claimability_read
from execution_coordinator.github_state import IssueBodyRead
from execution_coordinator.model import CoordinatorState, Role
from execution_coordinator.query import ClaimabilityReason
from execution_coordinator.snapshot import render_issue_body


BEGIN = "<!-- DEVFLOW_EXECUTION_CANDIDATES_V1_BEGIN -->"
END = "<!-- DEVFLOW_EXECUTION_CANDIDATES_V1_END -->"
CONTROL = DurableIssueSource("kinoko34077/devflow", 107)
NOW = datetime(2026, 9, 28, 5, 0, tzinfo=timezone.utc)


def _digest(body: str) -> str:
    normalized = body.replace("\r\n", "\n").replace("\r", "\n")
    return "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _envelope(
    *,
    task_body: str,
    roles: list[dict[str, str]] | None = None,
    status: str = "READY_FOR_IMPLEMENTATION",
) -> dict[str, object]:
    return {
        "task": "owner/repo#7",
        "task_body_sha256": _digest(task_body),
        "task_work_status": status,
        "entry_ref": "https://github.com/owner/repo/issues/7",
        "scope_ready": True,
        "blocked": False,
        "requires_user_confirmation": False,
        "roles": roles or [{"role": "implementer", "next_action_tag": "IMPLEMENT"}],
    }


def _projection(*, envelope: dict[str, object]) -> str:
    payload = {
        "schema_version": 1,
        "source_ref": "kinoko34077/devflow#107",
        "repository": "owner/repo",
        "candidates": [envelope],
    }
    return f"{BEGIN}\n{json.dumps(payload)}\n{END}"


def _control_body(*, block: str | None = None) -> str:
    suffix = f"\n\n{block}" if block else ""
    return (
        "## Repository\n\n`owner/repo`\n\n"
        "## Repository State\n\n`ACTIVE`\n\n"
        "## Work Status\n\n`AUDITED`\n\n"
        "## Next Action\n\n`[IMPLEMENT] bounded task`"
        f"{suffix}\n"
    )


def _document(repository: str, number: int, body: str, *, title: str = "Task") -> IssueDocument:
    return IssueDocument(
        repository=repository,
        number=number,
        state="open",
        body=body,
        html_url=f"https://github.com/{repository}/issues/{number}",
        title=title,
        author_association="MEMBER",
    )


class _IssueReader:
    def __init__(self, documents: dict[tuple[str, int], IssueDocument], events: list[str]) -> None:
        self.documents = documents
        self.events = events

    def read_issue(self, repository: str, issue_number: int) -> IssueDocument:
        self.events.append(f"issue:{repository}#{issue_number}")
        return self.documents[(repository, issue_number)]


class _StateReader:
    def __init__(self, body: str, events: list[str], *, updated_at: str = "2026-09-28T05:00:00Z", error: Exception | None = None) -> None:
        self.body = body
        self.events = events
        self.updated_at = updated_at
        self.error = error
        self.write_calls = 0

    def load_body_with_metadata(self) -> IssueBodyRead:
        self.events.append("state")
        if self.error is not None:
            raise self.error
        return IssueBodyRead(body=self.body, updated_at=self.updated_at)

    def save_body(self, body: str) -> None:
        self.write_calls += 1
        raise AssertionError("composed read must not write runtime state")


def _readers(*, roles: list[dict[str, str]] | None = None, block: str | None = None) -> tuple[_IssueReader, list[str]]:
    events: list[str] = []
    task_body = "## Scope\n\nBounded task."
    status = "AWAITING_REVIEW" if roles and roles[0]["role"] == "reviewer" else "READY_FOR_IMPLEMENTATION"
    control_block = block or _projection(
        envelope=_envelope(task_body=task_body, roles=roles, status=status)
    )
    reader = _IssueReader(
        {
            ("kinoko34077/devflow", 107): _document(
                "kinoko34077/devflow", 107, _control_body(block=control_block), title="[REPO] repo"
            ),
            ("owner/repo", 7): _document("owner/repo", 7, task_body),
        },
        events,
    )
    return reader, events


class ComposedReadTests(unittest.TestCase):
    def test_composes_discovery_state_and_claimability_in_order(self) -> None:
        reader, events = _readers()
        store = _StateReader(render_issue_body("", CoordinatorState.empty()), events)

        result = compose_claimability_read(
            (CONTROL,),
            issue_reader=reader,
            state_reader=store,
            now=NOW,
            worker_id="worker-a",
        )

        self.assertEqual(events[-1], "state")
        self.assertEqual(result.discovery.failures, ())
        self.assertEqual(len(result.discovery.candidates), 1)
        self.assertEqual(result.state.source_updated_at, "2026-09-28T05:00:00Z")
        self.assertEqual(result.claimable, (result.discovery.candidates[0],))
        self.assertEqual(result.claimability[0].reason, ClaimabilityReason.CLAIMABLE)
        self.assertEqual(store.write_calls, 0)

    def test_discovery_failure_is_preserved_and_cannot_become_claimable(self) -> None:
        reader, events = _readers(block=f"{BEGIN}\n{{bad json}}\n{END}")
        store = _StateReader(render_issue_body("", CoordinatorState.empty()), events)

        result = compose_claimability_read(
            (CONTROL,),
            issue_reader=reader,
            state_reader=store,
            now=NOW,
        )

        self.assertEqual(result.discovery.candidates, ())
        self.assertEqual(len(result.discovery.failures), 1)
        self.assertEqual(result.claimable, ())
        self.assertEqual(result.claimability, ())
        self.assertEqual(store.write_calls, 0)

    def test_state_read_failure_fails_closed_without_partial_result(self) -> None:
        reader, events = _readers()
        store = _StateReader("", events, error=RuntimeError("state unavailable"))

        with self.assertRaisesRegex(RuntimeError, "state unavailable"):
            compose_claimability_read(
                (CONTROL,),
                issue_reader=reader,
                state_reader=store,
                now=NOW,
            )

        self.assertEqual(store.write_calls, 0)

    def test_worker_scoped_recovery_blocker_preserves_reason_projection(self) -> None:
        reader, events = _readers(roles=[{"role": "reviewer", "next_action_tag": "REVIEW"}])
        active = claim(
            CoordinatorState.empty(),
            task="owner/repo#7",
            role=Role.RECOVERY,
            worker_id="worker-a",
            conflict_keys=(),
            now=NOW,
            idempotency_key="active-recovery",
        ).state
        store = _StateReader(render_issue_body("", active), events)

        result = compose_claimability_read(
            (CONTROL,),
            issue_reader=reader,
            state_reader=store,
            now=NOW,
            worker_id="worker-a",
        )

        self.assertEqual(result.claimable, ())
        self.assertEqual(result.claimability[0].reason, ClaimabilityReason.BLOCKED_LIVE)
        self.assertEqual(store.write_calls, 0)

    def test_expired_unswept_blocker_remains_nonclaimable_in_composed_read(self) -> None:
        reader, events = _readers()
        active = claim(
            CoordinatorState.empty(),
            task="owner/repo#7",
            role=Role.IMPLEMENTER,
            worker_id="worker-a",
            conflict_keys=(),
            now=NOW - timedelta(minutes=30),
            idempotency_key="expired-implementer",
            lease_minutes=1,
        ).state
        store = _StateReader(render_issue_body("", active), events)

        result = compose_claimability_read(
            (CONTROL,),
            issue_reader=reader,
            state_reader=store,
            now=NOW,
        )

        self.assertEqual(result.claimable, ())
        self.assertEqual(result.claimability[0].reason, ClaimabilityReason.EXPIRED_UNSWEPT)
        self.assertEqual(store.write_calls, 0)


if __name__ == "__main__":
    unittest.main()
