from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MUTATION_WORKFLOW = ROOT / ".github" / "workflows" / "mutate-state.yml"
VERIFY_WORKFLOW = ROOT / ".github" / "workflows" / "verify.yml"
AUTO_LAUNCH_WORKFLOW = ROOT / ".github" / "workflows" / "controller-auto-launch.yml"

CHECKOUT_SHA = "3d3c42e5aac5ba805825da76410c181273ba90b1"
SETUP_PYTHON_SHA = "5fda3b95a4ea91299a34e894583c3862153e4b97"
APP_TOKEN_SHA = "bcd2ba49218906704ab6c1aa796996da409d3eb1"
CLAUDE_BASE_ACTION_SHA = "dd171826d0197ac293cd459b506790de5119f573"


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

    def _auto_launch_text(self) -> str:
        return AUTO_LAUNCH_WORKFLOW.read_text(encoding="utf-8")

    def test_auto_launch_workflow_has_only_v1_wakes_and_bounded_single_job(self) -> None:
        text = self._auto_launch_text()
        self.assertIn("schedule:", text)
        self.assertIn("workflow_dispatch:", text)
        self.assertNotIn("repository_dispatch:", text)
        self.assertNotIn("issue_comment:", text)
        self.assertNotIn("pull_request:", text)
        self.assertNotIn("push:", text)
        self.assertEqual(1, text.count("runs-on:"))
        self.assertIn("timeout-minutes: 12", text)
        self.assertIn("concurrency:", text)
        self.assertIn("cancel-in-progress: false", text)

    def test_auto_launch_workflow_has_minimum_central_permissions_and_pinned_actions(self) -> None:
        text = self._auto_launch_text()
        self.assertIn("contents: read", text)
        self.assertIn("actions: write", text)
        self.assertIn("id-token: write", text)
        self.assertIn(f"actions/checkout@{CHECKOUT_SHA}", text)
        self.assertIn(f"actions/setup-python@{SETUP_PYTHON_SHA}", text)
        self.assertIn(f"actions/create-github-app-token@{APP_TOKEN_SHA}", text)
        self.assertIn(f"anthropics/claude-code-base-action@{CLAUDE_BASE_ACTION_SHA}", text)

    def test_auto_launch_target_token_is_offer_gated_scoped_and_created_before_claim(self) -> None:
        text = self._auto_launch_text()
        offer = text.index("name: Prepare controller offer")
        token = text.index("name: Mint target GitHub App token")
        claim = text.index("name: Accept and claim")
        self.assertLess(offer, token)
        self.assertLess(token, claim)
        self.assertIn("if: steps.offer.outputs.has_offer == 'true'", text)
        self.assertIn("client-id: ${{ vars.AUTONOMOUS_GITHUB_APP_CLIENT_ID }}", text)
        self.assertIn("private-key: ${{ secrets.AUTONOMOUS_GITHUB_APP_PRIVATE_KEY }}", text)
        self.assertIn("owner: ${{ steps.target.outputs.owner }}", text)
        self.assertIn("repositories: ${{ steps.target.outputs.repository_name }}", text)
        self.assertIn("permission-contents: write", text)
        self.assertIn("permission-pull-requests: write", text)
        self.assertIn("permission-issues: write", text)

    def test_auto_launch_target_checkout_never_persists_provider_visible_credentials(self) -> None:
        text = self._auto_launch_text()
        self.assertIn("name: Checkout selected target", text)
        self.assertIn("repository: ${{ steps.offer.outputs.target_repository }}", text)
        self.assertIn("token: ${{ steps.app-token.outputs.token }}", text)
        self.assertIn("persist-credentials: false", text)

    def test_auto_launch_claude_uses_oidc_bootstrap_then_exact_resumed_file_tools(self) -> None:
        text = self._auto_launch_text()
        for name in (
            "ANTHROPIC_FEDERATION_RULE_ID",
            "ANTHROPIC_ORGANIZATION_ID",
            "ANTHROPIC_SERVICE_ACCOUNT_ID",
        ):
            self.assertIn(f"vars.{name}", text)
        self.assertNotIn("anthropic_api_key:", text)
        self.assertNotIn("claude_code_oauth_token:", text)

        bootstrap = text.index("id: claude-bootstrap")
        reconcile = text.index("reconcile-bootstrap")
        work = text.index("id: claude-work")
        self.assertLess(bootstrap, reconcile)
        self.assertLess(reconcile, work)

        bootstrap_block = text[bootstrap:reconcile]
        self.assertIn('--tools ""', bootstrap_block)
        self.assertIn("--max-turns 1", bootstrap_block)
        self.assertIn("--session-id", bootstrap_block)

        work_block_end = text.find("\n      - name:", work)
        if work_block_end == -1:
            work_block_end = len(text)
        work_block = text[work:work_block_end]
        self.assertIn("steps.reconcile.outputs.state == 'RUNNING'", work_block)
        self.assertIn("--resume", work_block)
        self.assertIn('--tools "Read,Glob,Grep,Edit,Write"', work_block)
        self.assertNotIn("Bash", work_block)
        self.assertNotIn("GITHUB_TOKEN", work_block)
        self.assertNotIn("steps.app-token.outputs.token", work_block)

    def test_auto_launch_post_work_github_mutation_is_deterministic_and_finalized(self) -> None:
        text = self._auto_launch_text()
        work = text.index("id: claude-work")
        commit = text.index("git commit")
        push = text.index("git push")
        pr = text.index("gh pr create --draft")
        checkpoint = text.index("gh issue comment")
        finalize = text.index("finalize-work")
        self.assertLess(work, commit)
        self.assertLess(commit, push)
        self.assertLess(push, pr)
        self.assertLess(pr, checkpoint)
        self.assertLess(checkpoint, finalize)
        self.assertIn("if: always()", text)
        for forbidden in ("gh pr merge", "gh release", "gh workflow run deploy"):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
