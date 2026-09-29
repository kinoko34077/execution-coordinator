from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from execution_coordinator.actions_controller import accept_and_claim, prepare_offer
from tests.test_agent import _Gateway
from tests.test_bootstrap_pickup import NOW, _Store, _portfolio_control


class _SequencedTaskReader:
    def __init__(self, control, *task_reads):
        self._control = control
        self._task_reads = list(task_reads)
        self._task_index = 0

    def read_issue(self, repository: str, number: int):
        if repository == self._control.repository and number == self._control.number:
            return self._control
        if not self._task_reads:
            raise AssertionError("no task reads configured")
        index = min(self._task_index, len(self._task_reads) - 1)
        self._task_index += 1
        return self._task_reads[index]


class ActionsControllerTaskSnapshotTests(unittest.TestCase):
    def _fixture(self):
        control, task, _ = _portfolio_control("owner/a", 201, 8, priority="P1")
        return control, task

    def _preflight(self):
        return {
            "github_app_ready": True,
            "wif_ready": True,
            "repository_checkout": True,
            "capabilities": ["python"],
            "environment": ["windows"],
        }

    def test_prompt_read_rejects_body_changed_after_current_offer_revalidation(self):
        control, task = self._fixture()
        changed = replace(task, body=task.body + "\n\npost-validation instruction swap")
        reader = _SequencedTaskReader(control, task, task, changed)
        gateway = _Gateway(now=NOW)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepare_offer(
                (control,),
                issue_reader=reader,
                state_reader=_Store(),
                now=NOW,
                context_dir=root,
            )
            result = accept_and_claim(
                (control,),
                issue_reader=reader,
                state_reader=gateway.store,
                gateway=gateway,
                now=NOW,
                context_dir=root,
                preflight=self._preflight(),
                base_sha="a" * 40,
                attempt_id="task-body-race",
            )

            self.assertEqual("DEFERRED_BUSY", result["result"])
            self.assertEqual([], gateway.calls)
            self.assertFalse((root / "work-prompt.txt").exists())

    def test_prompt_read_rejects_trust_change_after_current_offer_revalidation(self):
        control, task = self._fixture()
        untrusted = replace(task, author_association="NONE")
        reader = _SequencedTaskReader(control, task, task, untrusted)
        gateway = _Gateway(now=NOW)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepare_offer(
                (control,),
                issue_reader=reader,
                state_reader=_Store(),
                now=NOW,
                context_dir=root,
            )
            result = accept_and_claim(
                (control,),
                issue_reader=reader,
                state_reader=gateway.store,
                gateway=gateway,
                now=NOW,
                context_dir=root,
                preflight=self._preflight(),
                base_sha="b" * 40,
                attempt_id="task-trust-race",
            )

            self.assertEqual("DEFERRED_BUSY", result["result"])
            self.assertEqual([], gateway.calls)
            self.assertFalse((root / "work-prompt.txt").exists())


if __name__ == "__main__":
    unittest.main()
