from __future__ import annotations

import json
import unittest
from urllib.error import URLError
from datetime import datetime, timezone

from execution_coordinator.actions_gateway import (
    ActionsMutationGateway,
    MutationOutcomeUnknown,
    MutationRunRejected,
)
from execution_coordinator.agent import AgentSession
from execution_coordinator.engine import CoordinationError
from execution_coordinator.model import CoordinatorState, Role
from execution_coordinator.mutate import (
    REJECTION_ANNOTATION_TITLE,
    apply_mutation,
    rejection_annotation,
)
from execution_coordinator.engine import ClaimConflict
from execution_coordinator.snapshot import parse_issue_body, render_issue_body


NOW = datetime(2026, 9, 28, 14, 0, tzinfo=timezone.utc)
REPO = "https://api.github.test/repos/owner/repo"


class _Response:
    def __init__(self, payload: object | None) -> None:
        self._raw = b"" if payload is None else json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self) -> bytes:
        return self._raw


class _Store:
    def __init__(self) -> None:
        self.body = render_issue_body("# state\n", CoordinatorState.empty())

    def load_body(self) -> str:
        return self.body

    def save_body(self, body: str) -> None:
        self.body = body

    def add_comment(self, _body: str) -> None:
        pass


class _FakeActions:
    """Fake dispatch API that applies the mutation like mutate-state.yml."""

    def __init__(self, *, conclusion: str = "success", pending_polls: int = 1,
                 commit: bool = True, run_id: object = 101) -> None:
        self.store = _Store()
        self.conclusion = conclusion
        self.pending_polls = pending_polls
        self.commit = commit
        self.run_id = run_id
        self.requests: list[tuple[str, str, object]] = []
        self.annotations: list[dict[str, object]] = []
        self.jobs_error = False

    def __call__(self, request, timeout):
        body = json.loads(request.data.decode("utf-8")) if request.data else None
        self.requests.append((request.get_method(), request.full_url, body))
        if request.full_url.endswith("/dispatches"):
            assert request.get_method() == "POST"
            inputs = body["inputs"]
            if self.commit and self.conclusion == "success":
                try:
                    apply_mutation(
                        self.store,
                        operation=inputs["operation"],
                        payload=json.loads(inputs["payload_json"]),
                        idempotency_key=inputs["idempotency_key"],
                        now=NOW,
                    )
                except CoordinationError:
                    self.conclusion = "failure"
            return _Response({"workflow_run_id": self.run_id})
        if request.full_url == f"{REPO}/actions/runs/{self.run_id}/jobs":
            if self.jobs_error:
                raise URLError("jobs unavailable")
            return _Response({"jobs": [{"id": 555}]})
        if request.full_url == f"{REPO}/check-runs/555/annotations":
            return _Response(self.annotations)
        assert request.full_url == f"{REPO}/actions/runs/{self.run_id}"
        if self.pending_polls > 0:
            self.pending_polls -= 1
            return _Response({"id": self.run_id, "status": "in_progress", "conclusion": None})
        return _Response({"id": self.run_id, "status": "completed", "conclusion": self.conclusion})


def _gateway(fake: _FakeActions, *, timeout: float = 300.0) -> ActionsMutationGateway:
    clock = [0.0]

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    return ActionsMutationGateway(
        token="t",
        repository="owner/repo",
        state_reader=fake.store.load_body,
        api_base_url="https://api.github.test",
        timeout_seconds=timeout,
        poll_interval_seconds=5.0,
        opener=fake,
        sleep=sleep,
        monotonic=lambda: clock[0],
    )


class ActionsMutationGatewayTests(unittest.TestCase):
    def test_successful_run_reads_committed_idempotency_record(self) -> None:
        fake = _FakeActions()
        session = AgentSession(
            _gateway(fake), task="owner/repo#1", role=Role.IMPLEMENTER, worker_id="w1"
        )

        session.claim(idempotency_key="k-claim")
        session.acknowledge(idempotency_key="k-ack")
        session.release(idempotency_key="k-release")

        dispatches = [r for r in fake.requests if r[1].endswith("/dispatches")]
        self.assertEqual(3, len(dispatches))
        self.assertEqual("main", dispatches[0][2]["ref"])
        self.assertTrue(dispatches[0][2]["return_run_details"])
        self.assertEqual("claim", dispatches[0][2]["inputs"]["operation"])
        self.assertEqual({}, dict(parse_issue_body(fake.store.body).claims))

    def test_failed_run_is_rejection_not_success(self) -> None:
        fake = _FakeActions(conclusion="failure", commit=False)
        with self.assertRaises(MutationRunRejected) as ctx:
            _gateway(fake).mutate(
                operation="claim",
                payload={"task": "owner/repo#1", "role": "implementer", "worker_id": "w1"},
                idempotency_key="k",
            )
        self.assertIsInstance(ctx.exception, CoordinationError)

    def test_cancelled_run_is_rejection(self) -> None:
        fake = _FakeActions(conclusion="cancelled", commit=False)
        with self.assertRaisesRegex(MutationRunRejected, "cancelled"):
            _gateway(fake).mutate(operation="claim", payload={}, idempotency_key="k")

    def test_conflicting_claim_surfaces_as_coordination_rejection(self) -> None:
        fake = _FakeActions()
        winner = AgentSession(
            _gateway(fake), task="owner/repo#1", role=Role.IMPLEMENTER, worker_id="w1"
        )
        winner.claim(idempotency_key="k-winner")
        loser = AgentSession(
            _gateway(fake), task="owner/repo#1", role=Role.IMPLEMENTER, worker_id="w2"
        )
        with self.assertRaises(CoordinationError):
            loser.claim(idempotency_key="k-loser")

    def test_timeout_is_ambiguous_not_rejection(self) -> None:
        fake = _FakeActions(pending_polls=10_000)
        with self.assertRaises(MutationOutcomeUnknown) as ctx:
            _gateway(fake, timeout=30.0).mutate(
                operation="claim",
                payload={"task": "owner/repo#1", "role": "implementer", "worker_id": "w1"},
                idempotency_key="k",
            )
        self.assertNotIsInstance(ctx.exception, CoordinationError)

    def test_success_without_committed_record_is_ambiguous(self) -> None:
        fake = _FakeActions(commit=False)
        with self.assertRaisesRegex(MutationOutcomeUnknown, "no idempotency record"):
            _gateway(fake).mutate(operation="claim", payload={}, idempotency_key="k")

    def test_missing_run_id_is_ambiguous(self) -> None:
        fake = _FakeActions(run_id=None, commit=False)
        with self.assertRaisesRegex(MutationOutcomeUnknown, "run id"):
            _gateway(fake).mutate(operation="claim", payload={}, idempotency_key="k")

    def test_failed_run_surfaces_typed_rejection_from_annotation(self) -> None:
        fake = _FakeActions(conclusion="failure", commit=False)
        line = rejection_annotation(ClaimConflict("task already claimed"))
        title, _, encoded = line.removeprefix("::error ").partition("::")
        self.assertEqual(f"title={REJECTION_ANNOTATION_TITLE}", title)
        fake.annotations = [
            {"title": "other", "message": "noise"},
            {"title": REJECTION_ANNOTATION_TITLE, "message": encoded},
        ]
        with self.assertRaises(MutationRunRejected) as ctx:
            _gateway(fake).mutate(operation="claim", payload={}, idempotency_key="k")
        self.assertEqual("ClaimConflict", ctx.exception.error_class)
        self.assertIn("task already claimed", str(ctx.exception))

    def test_unreadable_rejection_reason_is_still_a_rejection(self) -> None:
        for setup in ("no-annotation", "jobs-error", "bad-json"):
            with self.subTest(setup=setup):
                fake = _FakeActions(conclusion="failure", commit=False)
                if setup == "jobs-error":
                    fake.jobs_error = True
                if setup == "bad-json":
                    fake.annotations = [
                        {"title": REJECTION_ANNOTATION_TITLE, "message": "{not json"}
                    ]
                with self.assertRaises(MutationRunRejected) as ctx:
                    _gateway(fake).mutate(operation="claim", payload={}, idempotency_key="k")
                self.assertIsNone(ctx.exception.error_class)

    def test_rejection_annotation_escapes_newlines_and_percent(self) -> None:
        line = rejection_annotation(ValueError("100%\nsecond line"))
        self.assertNotIn("\n", line)
        self.assertIn("%25", line)
        self.assertTrue(line.startswith(f"::error title={REJECTION_ANNOTATION_TITLE}::"))

    def test_constructor_validates_inputs(self) -> None:
        with self.assertRaises(ValueError):
            ActionsMutationGateway(token="", repository="o/r", state_reader=lambda: "")
        with self.assertRaises(ValueError):
            ActionsMutationGateway(token="t", repository="bad", state_reader=lambda: "")


if __name__ == "__main__":
    unittest.main()
