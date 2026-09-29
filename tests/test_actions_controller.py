from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from uuid import UUID

from execution_coordinator.actions_controller import (
    accept_and_claim,
    finalize_work,
    prepare_offer,
    reconcile_bootstrap,
)
from execution_coordinator.model import ExecutionState
from execution_coordinator.snapshot import parse_issue_body
from tests.test_agent import _Gateway
from tests.test_bootstrap_pickup import NOW, _Reader, _Store, _doc, _portfolio_control


class ActionsControllerTests(unittest.TestCase):
    def _fixture(self):
        control, task, _ = _portfolio_control("owner/a", 201, 8, priority="P1")
        return control, task, _Reader(control, task)

    def _preflight(self, **overrides):
        value = {
            "github_app_ready": True,
            "wif_ready": True,
            "repository_checkout": True,
            "capabilities": ["python"],
            "environment": ["windows"],
        }
        value.update(overrides)
        return value

    def test_prepare_offer_is_read_only_and_derives_exact_target(self) -> None:
        control, task, reader = self._fixture()
        store = _Store()
        before = store.body
        with tempfile.TemporaryDirectory() as tmp:
            result = prepare_offer(
                (control,), issue_reader=reader, state_reader=store,
                now=NOW, context_dir=Path(tmp),
            )
            self.assertTrue(result["has_offer"])
            self.assertEqual("owner/a", result["target_repository"])
            self.assertEqual(8, result["target_issue"])
            self.assertTrue((Path(tmp) / "offer.json").exists())
        self.assertEqual(before, store.body)

    def test_no_offer_or_missing_preflight_never_claims(self) -> None:
        blocked, task, _ = _portfolio_control(
            "owner/a", 201, 8, next_action="`[HUMAN_GATE] decide`"
        )
        reader = _Reader(blocked, task)
        with tempfile.TemporaryDirectory() as tmp:
            result = prepare_offer(
                (blocked,), issue_reader=reader, state_reader=_Store(),
                now=NOW, context_dir=Path(tmp),
            )
            self.assertFalse(result["has_offer"])

        control, task, reader = self._fixture()
        gateway = _Gateway(now=NOW)
        with tempfile.TemporaryDirectory() as tmp:
            prepare_offer(
                (control,), issue_reader=reader, state_reader=gateway.store,
                now=NOW, context_dir=Path(tmp),
            )
            result = accept_and_claim(
                (control,), issue_reader=reader, state_reader=gateway.store,
                gateway=gateway, now=NOW, context_dir=Path(tmp),
                preflight=self._preflight(wif_ready=False),
                base_sha="a" * 40, attempt_id="run-123",
            )
            self.assertEqual("REQUIRES_USER_AUTHORITY", result["result"])
            self.assertEqual([], gateway.calls)

    def test_stale_offer_revalidation_fails_before_claim(self) -> None:
        control, task, reader = self._fixture()
        gateway = _Gateway(now=NOW)
        with tempfile.TemporaryDirectory() as tmp:
            prepare_offer(
                (control,), issue_reader=reader, state_reader=gateway.store,
                now=NOW, context_dir=Path(tmp),
            )
            changed_task = _doc("owner/a", 8, task.body + "\nchanged")
            result = accept_and_claim(
                (control,), issue_reader=_Reader(control, changed_task),
                state_reader=gateway.store, gateway=gateway, now=NOW,
                context_dir=Path(tmp), preflight=self._preflight(),
                base_sha="b" * 40, attempt_id="run-124",
            )
            self.assertNotEqual("ACCEPTED", result["result"])
            self.assertEqual([], gateway.calls)

    def test_accept_claim_binds_uuid_branch_request_and_secret_free_prompt(self) -> None:
        control, task, reader = self._fixture()
        gateway = _Gateway(now=NOW)
        expected_uuid = UUID("12345678-1234-5678-9234-567812345678")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepare_offer(
                (control,), issue_reader=reader, state_reader=gateway.store,
                now=NOW, context_dir=root,
            )
            result = accept_and_claim(
                (control,), issue_reader=reader, state_reader=gateway.store,
                gateway=gateway, now=NOW, context_dir=root,
                preflight=self._preflight(), base_sha="c" * 40,
                attempt_id="run-125", uuid_factory=lambda: expected_uuid,
            )
            self.assertEqual("ACCEPTED", result["result"])
            self.assertEqual(str(expected_uuid), result["session_id"])
            self.assertEqual(["claim"], [call[0] for call in gateway.calls])
            prompt = (root / "work-prompt.txt").read_text(encoding="utf-8")
            context = (root / "launch-context.json").read_text(encoding="utf-8")
            self.assertIn("owner/a#8", prompt)
            self.assertIn("claim_id", prompt)
            self.assertIn("generation", prompt)
            self.assertIn("c" * 40, prompt)
            self.assertIn("agent/controller-8-", prompt)
            self.assertIn("Do not push", prompt)
            self.assertIn("Do not create or update pull requests", prompt)
            self.assertNotIn("ghp_", prompt)
            self.assertNotIn("github_pat_", prompt)
            self.assertIn(str(expected_uuid), context)
            self.assertTrue((root / "execution-request.json").exists())

    def test_bootstrap_reconciliation_and_finalize_use_runtime_lifecycle(self) -> None:
        control, task, reader = self._fixture()
        gateway = _Gateway(now=NOW)
        expected_uuid = UUID("12345678-1234-5678-9234-567812345678")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepare_offer((control,), issue_reader=reader, state_reader=gateway.store, now=NOW, context_dir=root)
            accepted = accept_and_claim(
                (control,), issue_reader=reader, state_reader=gateway.store,
                gateway=gateway, now=NOW, context_dir=root,
                preflight=self._preflight(), base_sha="d" * 40,
                attempt_id="run-126", uuid_factory=lambda: expected_uuid,
            )
            transition = reconcile_bootstrap(
                context_dir=root,
                bootstrap_result={"status": "success", "session_id": str(expected_uuid)},
                gateway=gateway, current_state=parse_issue_body(gateway.store.body),
                now=NOW, evidence_ref="run:126:bootstrap",
            )
            self.assertEqual("RUNNING", transition["state"])
            claim_id = accepted["claim_id"]
            self.assertEqual(ExecutionState.RUNNING, parse_issue_body(gateway.store.body).claims[claim_id].state)
            final = finalize_work(
                context_dir=root,
                work_result={"status": "success", "has_diff": True, "pr_url": "https://github.com/owner/a/pull/1"},
                gateway=gateway, current_state=parse_issue_body(gateway.store.body),
                now=NOW, evidence_ref="run:126:work",
            )
            self.assertEqual("RELEASED", final["state"])
            self.assertNotIn(claim_id, parse_issue_body(gateway.store.body).claims)
            self.assertTrue((root / "task-evidence.json").exists())

    def test_bootstrap_non_success_outcomes_are_typed_and_leave_expected_authority(self) -> None:
        cases = (
            ({"status": "success", "session_id": "wrong-session"}, "WAITING:PROVIDER", True),
            ({"status": "failed", "session_id": "12345678-1234-5678-9234-567812345678", "terminal": True, "reason": "provider exited"}, "FAILED", False),
            ({"status": "unavailable", "session_started": False, "reason": "provider unavailable"}, "RELEASED", False),
        )
        for index, (bootstrap_result, expected_state, keeps_claim) in enumerate(cases):
            with self.subTest(expected_state=expected_state):
                control, task, reader = self._fixture()
                gateway = _Gateway(now=NOW)
                expected_uuid = UUID("12345678-1234-5678-9234-567812345678")
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    prepare_offer((control,), issue_reader=reader, state_reader=gateway.store, now=NOW, context_dir=root)
                    accepted = accept_and_claim(
                        (control,), issue_reader=reader, state_reader=gateway.store,
                        gateway=gateway, now=NOW, context_dir=root,
                        preflight=self._preflight(), base_sha=(hex(index + 1)[2:] * 40)[:40],
                        attempt_id=f"run-extra-{index}", uuid_factory=lambda: expected_uuid,
                    )
                    transition = reconcile_bootstrap(
                        context_dir=root, bootstrap_result=bootstrap_result,
                        gateway=gateway, current_state=parse_issue_body(gateway.store.body),
                        now=NOW, evidence_ref=f"run:extra:{index}",
                    )
                    self.assertEqual(expected_state, transition["state"])
                    claims = parse_issue_body(gateway.store.body).claims
                    self.assertEqual(keeps_claim, accepted["claim_id"] in claims)
                    if keeps_claim:
                        claim = claims[accepted["claim_id"]]
                        self.assertEqual(ExecutionState.WAITING, claim.state)

    def test_finalize_failure_fails_runtime_claim_and_records_bounded_evidence(self) -> None:
        control, task, reader = self._fixture()
        gateway = _Gateway(now=NOW)
        expected_uuid = UUID("12345678-1234-5678-9234-567812345678")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepare_offer((control,), issue_reader=reader, state_reader=gateway.store, now=NOW, context_dir=root)
            accepted = accept_and_claim(
                (control,), issue_reader=reader, state_reader=gateway.store,
                gateway=gateway, now=NOW, context_dir=root,
                preflight=self._preflight(), base_sha="e" * 40,
                attempt_id="run-fail", uuid_factory=lambda: expected_uuid,
            )
            reconcile_bootstrap(
                context_dir=root,
                bootstrap_result={"status": "success", "session_id": str(expected_uuid)},
                gateway=gateway, current_state=parse_issue_body(gateway.store.body),
                now=NOW, evidence_ref="run:fail:bootstrap",
            )
            final = finalize_work(
                context_dir=root, work_result={"status": "failed", "reason": "tests failed"},
                gateway=gateway, current_state=parse_issue_body(gateway.store.body),
                now=NOW, evidence_ref="run:fail:work",
            )
            self.assertEqual("FAILED", final["state"])
            self.assertNotIn(accepted["claim_id"], parse_issue_body(gateway.store.body).claims)
            evidence = (root / "task-evidence.json").read_text(encoding="utf-8")
            self.assertIn("tests failed", evidence)


if __name__ == "__main__":
    unittest.main()
