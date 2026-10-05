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

    def test_auto_launch_expires_stale_runtime_before_read_only_offer(self) -> None:
        text = self._auto_launch_text()
        expire = text.index("name: Expire stale runtime claims")
        offer = text.index("name: Prepare controller offer")
        self.assertLess(expire, offer)
        self.assertIn("expire-stale", text[expire:offer])
        self.assertIn("--attempt-id", text[expire:offer])

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

    def test_auto_launch_reviewer_does_not_mint_or_use_write_app_token(self) -> None:
        text = self._auto_launch_text()
        token = text.index("name: Mint target GitHub App token")
        checkout = text.index("name: Checkout selected target")
        token_block = text[token:checkout]
        self.assertIn("steps.offer.outputs.role != 'reviewer'", token_block)

        checkout_end = text.index("name: Probe exact launcher environment")
        checkout_block = text[checkout:checkout_end]
        self.assertIn("steps.offer.outputs.role == 'reviewer'", checkout_block)
        self.assertIn("secrets.COORDINATOR_READ_TOKEN", checkout_block)
        self.assertIn("steps.app-token.outputs.token", checkout_block)

        config = text.index("name: Check human-gated provider configuration")
        config_end = text.index("name: Mint target GitHub App token")
        config_block = text[config:config_end]
        self.assertIn('target_access_ready = read_ready if role == "reviewer" else app_ready', config_block)

    def test_auto_launch_target_checkout_never_persists_provider_visible_credentials(self) -> None:
        text = self._auto_launch_text()
        self.assertIn("name: Checkout selected target", text)
        self.assertIn("repository: ${{ steps.offer.outputs.target_repository }}", text)
        self.assertIn(
            "token: ${{ steps.offer.outputs.role == 'reviewer' && secrets.COORDINATOR_READ_TOKEN || steps.app-token.outputs.token }}",
            text,
        )
        self.assertIn("persist-credentials: false", text)

    def test_auto_launch_preflight_does_not_invent_shell_or_network_capability_for_claude(self) -> None:
        text = self._auto_launch_text()
        start = text.index("name: Probe exact launcher environment")
        end = text.index("name: Accept and claim")
        block = text[start:end]
        self.assertNotIn('("python3", "python")', block)
        self.assertNotIn('("git", "git")', block)
        self.assertNotIn('("node", "node")', block)
        self.assertNotIn('environment.append("github-network")', block)
        self.assertNotIn('environment.append("github-actions-lane")', block)
        self.assertIn('capabilities.append("repo-checkout")', block)
        self.assertIn('environment.append("linux")', block)

    def test_auto_launch_claude_uses_oidc_isolated_mode_then_exact_resumed_file_tools(self) -> None:
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
        implement = text.index("id: claude-implement")
        review = text.index("id: claude-review")
        self.assertLess(bootstrap, reconcile)
        self.assertLess(reconcile, implement)
        self.assertLess(reconcile, review)

        bootstrap_block = text[bootstrap:reconcile]
        self.assertIn("--safe-mode", bootstrap_block)
        self.assertNotIn("--bare", bootstrap_block)
        self.assertIn('--tools ""', bootstrap_block)
        self.assertIn("--max-turns 1", bootstrap_block)
        self.assertIn("--session-id", bootstrap_block)

        implement_end = text.find("\n      - name:", implement)
        implement_block = text[implement:implement_end]
        self.assertIn("steps.claim.outputs.role != 'reviewer'", implement_block)
        self.assertIn("steps.reconcile.outputs.state == 'RUNNING'", implement_block)
        self.assertIn("--safe-mode", implement_block)
        self.assertNotIn("--bare", implement_block)
        self.assertIn("--resume", implement_block)
        self.assertIn('--tools "Read,Edit,Write"', implement_block)
        self.assertNotIn("Bash", implement_block)
        self.assertNotIn("GITHUB_TOKEN", implement_block)
        self.assertNotIn("steps.app-token.outputs.token", implement_block)

        review_end = text.find("\n      - name:", review)
        review_block = text[review:review_end]
        self.assertIn("steps.claim.outputs.role == 'reviewer'", review_block)
        self.assertIn("--safe-mode", review_block)
        self.assertIn("--resume", review_block)
        self.assertIn('--tools "Read"', review_block)
        self.assertNotIn("Edit", review_block)
        self.assertNotIn("Write", review_block)
        self.assertNotIn("Bash", review_block)
        self.assertNotIn("steps.app-token.outputs.token", review_block)

    def test_auto_launch_reviewer_path_is_exact_head_and_evidence_only(self) -> None:
        text = self._auto_launch_text()
        checkout = text.index("name: Checkout selected target")
        preflight = text.index("name: Probe exact launcher environment")
        self.assertIn("ref: ${{ steps.offer.outputs.checkout_ref }}", text[checkout:preflight])

        prepare = text.index("name: Prepare role-aware target context")
        bootstrap = text.index("name: Bootstrap Claude execution context")
        prepare_block = text[prepare:bootstrap]
        self.assertIn('if [ "$ROLE" = "reviewer" ]', prepare_block)
        self.assertIn('test "$ACTUAL_HEAD" = "$REVIEW_HEAD"', prepare_block)
        self.assertIn('status --porcelain', prepare_block)

        review = text.index("id: claude-review")
        integration = text.index("id: integrate")
        review_evidence = text.index("id: review-evidence")
        self.assertLess(review, review_evidence)
        integration_header = text[integration:text.find("\n        run:", integration)]
        self.assertIn("steps.claim.outputs.role != 'reviewer'", integration_header)

        evidence_end = text.find("\n      - name:", review_evidence)
        evidence_block = text[review_evidence:evidence_end]
        self.assertIn('test "$ACTUAL_HEAD" = "$REVIEW_HEAD"', evidence_block)
        self.assertIn('status --porcelain', evidence_block)
        self.assertIn('"github_review_submitted": False', evidence_block)
        for forbidden in ("git commit", "git push", "gh pr create", "gh issue comment"):
            self.assertNotIn(forbidden, evidence_block)

    def test_auto_launch_post_work_github_mutation_is_deterministic_and_finalized(self) -> None:
        text = self._auto_launch_text()
        work = text.index("id: claude-implement")
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
