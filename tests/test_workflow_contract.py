from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MUTATION_WORKFLOW = ROOT / ".github" / "workflows" / "mutate-state.yml"
VERIFY_WORKFLOW = ROOT / ".github" / "workflows" / "verify.yml"

CHECKOUT_SHA = "3d3c42e5aac5ba805825da76410c181273ba90b1"
SETUP_PYTHON_SHA = "5fda3b95a4ea91299a34e894583c3862153e4b97"


class WorkflowContractTests(unittest.TestCase):
    def test_mutation_workflow_exists_and_uses_one_queued_global_lane(self) -> None:
        text = MUTATION_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("group: execution-coordinator-state-mutation", text)
        self.assertIn("queue: max", text)
        self.assertNotIn("cancel-in-progress: true", text)

    def test_mutation_workflow_has_minimum_permissions_and_dispatch_inputs(self) -> None:
        text = MUTATION_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("contents: read", text)
        self.assertIn("issues: write", text)
        self.assertIn("workflow_dispatch:", text)
        for name in ("operation", "payload_json", "idempotency_key"):
            self.assertIn(f"{name}:", text)

    def test_external_actions_are_full_sha_pinned(self) -> None:
        for path in (VERIFY_WORKFLOW, MUTATION_WORKFLOW):
            text = path.read_text(encoding="utf-8")
            self.assertIn(f"actions/checkout@{CHECKOUT_SHA}", text)
            self.assertIn(f"actions/setup-python@{SETUP_PYTHON_SHA}", text)

    def test_mutation_workflow_calls_only_the_coordinator_entrypoint(self) -> None:
        text = MUTATION_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("python -m execution_coordinator.mutate", text)
        self.assertIn("--operation", text)
        self.assertIn("--payload-json", text)
        self.assertIn("--idempotency-key", text)

    def test_authority_mutation_job_runs_only_from_main_ref(self) -> None:
        text = MUTATION_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("if: github.ref == 'refs/heads/main'", text)


if __name__ == "__main__":
    unittest.main()
