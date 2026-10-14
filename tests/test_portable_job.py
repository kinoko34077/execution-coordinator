from __future__ import annotations

import unittest
from dataclasses import replace

from execution_coordinator.portable_job import (
    JobRequest,
    RoutingEvidence,
    TrustedRecipe,
    plan_portable_job,
)


class PortableJobContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.recipe = TrustedRecipe(
            recipe_id="audit.control",
            digest_sha256="a" * 64,
            input_schema={"mode": "str", "limit": "int", "dry_run": "bool"},
            backends=frozenset({"github_actions", "external_isolated"}),
            read_only=True,
            requires_secrets=False,
            max_seconds=240,
        )
        self.request = JobRequest(
            repository="kinoko34077/devflow",
            task_ref="kinoko34077/devflow#541",
            source_sha="b" * 40,
            recipe_id="audit.control",
            recipe_sha256="a" * 64,
            attempt_id="attempt-1",
            inputs={"mode": "audit", "limit": 5, "dry_run": True},
        )
        self.evidence = RoutingEvidence(
            actions="READY",
            external="READY",
            github_api="VERIFIED",
            coordinator_claim="VERIFIED",
            caller_authorized=True,
            external_isolated=True,
            private_source_authorized=True,
        )

    def test_healthy_actions_preferred_to_external(self) -> None:
        plan = plan_portable_job(self.recipe, self.request, self.evidence)
        self.assertEqual((plan.disposition, plan.backend), ("CANDIDATE_ONLY", "github_actions"))
        self.assertEqual(len(plan.request_digest or ""), 64)

    def test_fallback_on_verified_capacity_or_control_outage(self) -> None:
        for reason in ("CAPACITY_BLOCKED", "CONTROL_OUTAGE"):
            with self.subTest(reason=reason):
                result = plan_portable_job(self.recipe, self.request, replace(self.evidence, actions=reason))
                self.assertEqual((result.disposition, result.backend), ("CANDIDATE_ONLY", "external_isolated"))

    def test_native_unknown_or_policy_disabled_never_falls_back(self) -> None:
        for actions, expected in (("UNKNOWN", "NATIVE_STATUS_UNKNOWN"), ("POLICY_DISABLED", "POLICY_DISABLED")):
            with self.subTest(actions=actions):
                result = plan_portable_job(self.recipe, self.request, replace(self.evidence, actions=actions))
                self.assertEqual((result.disposition, result.reason), ("HOLD", expected))

    def test_missing_coordinator_or_github_api_fail_closed(self) -> None:
        for key in ("coordinator_claim", "github_api"):
            for value in ("UNKNOWN", "UNAVAILABLE"):
                with self.subTest(key=key, value=value):
                    self.assertEqual(
                        plan_portable_job(self.recipe, self.request, replace(self.evidence, **{key: value})).reason,
                        "AUTHORITY_UNAVAILABLE",
                    )

    def test_unapproved_private_data_or_caller_denied(self) -> None:
        for key, reason in (("caller_authorized", "CALLER_UNAUTHORIZED"), ("private_source_authorized", "SOURCE_ACCESS_UNVERIFIED")):
            with self.subTest(key=key):
                self.assertEqual(
                    plan_portable_job(self.recipe, self.request, replace(self.evidence, **{key: False})).reason,
                    reason,
                )

    def test_nonisolated_or_unavailable_external_denied(self) -> None:
        for env in (
            replace(self.evidence, external_isolated=False),
            replace(self.evidence, external="UNAVAILABLE"),
            replace(self.evidence, external="UNKNOWN"),
        ):
            result = plan_portable_job(self.recipe, self.request, replace(env, actions="CONTROL_OUTAGE"))
            self.assertEqual(result.reason, "NO_VERIFIED_EXECUTION_BACKEND")

    def test_only_explicit_backends_eligible(self) -> None:
        only_native = replace(self.recipe, backends=frozenset({"github_actions"}))
        self.assertEqual(
            plan_portable_job(only_native, self.request, replace(self.evidence, actions="CAPACITY_BLOCKED")).reason,
            "NO_VERIFIED_EXECUTION_BACKEND",
        )
        only_external = replace(self.recipe, backends=frozenset({"external_isolated"}))
        self.assertEqual(plan_portable_job(only_external, self.request, self.evidence).backend, "external_isolated")

    def test_reject_mutating_or_secret_requiring_recipes(self) -> None:
        for altered in (
            replace(self.recipe, read_only=False),
            replace(self.recipe, requires_secrets=True),
            replace(self.recipe, max_seconds=True),
            replace(self.recipe, max_seconds=721),
            replace(self.recipe, backends=frozenset({"arbitrary_shell"})),
            replace(self.recipe, backends=frozenset()),
            replace(self.recipe, input_schema={"mode": "json"}),
            replace(self.recipe, input_schema={"../../x": "str"}),
            replace(self.recipe, digest_sha256="anything"),
        ):
            with self.subTest(recipe=altered):
                self.assertEqual(plan_portable_job(altered, self.request, self.evidence).reason, "INVALID_RECIPE_OR_REQUEST")

    def test_exact_source_and_recipe_binding(self) -> None:
        for request in (
            replace(self.request, source_sha="main"),
            replace(self.request, source_sha="B" * 40),
            replace(self.request, recipe_sha256="b" * 64),
            replace(self.request, recipe_id="not.audit"),
            replace(self.request, repository="kinoko34077/devflow/evil"),
            replace(self.request, repository="kinoko34077/.."),
            replace(self.request, repository="./devflow"),
            replace(self.request, task_ref="../devflow#541"),
            replace(self.request, task_ref="bad-task"),
            replace(self.request, attempt_id="retry token"),
            replace(self.request, attempt_id="x" * 200),
        ):
            with self.subTest(request=request):
                self.assertEqual(plan_portable_job(self.recipe, request, self.evidence).reason, "INVALID_RECIPE_OR_REQUEST")

    def test_input_keys_and_exact_scalar_types(self) -> None:
        for inputs in (
            {"mode": "audit", "limit": True, "dry_run": True},
            {"mode": "audit", "limit": 5.0, "dry_run": True},
            {"mode": "audit", "limit": 5, "dry_run": 1},
            {"mode": "audit", "limit": 5, "dry_run": True, "shell": "rm -rf"},
            {"mode": "audit", "limit": 5},
            {"mode": "audit\nINJECT", "limit": 5, "dry_run": True},
            {"mode": "x" * 1025, "limit": 5, "dry_run": True},
            {"mode": "audit", "limit": 2**64, "dry_run": True},
            {"mode": {"command": "whoami"}, "limit": 5, "dry_run": True},
        ):
            with self.subTest(inputs=inputs):
                self.assertEqual(
                    plan_portable_job(self.recipe, replace(self.request, inputs=inputs), self.evidence).reason,
                    "INVALID_RECIPE_OR_REQUEST",
                )

    def test_digest_is_stable_and_binding_changes_it(self) -> None:
        p1 = plan_portable_job(self.recipe, self.request, self.evidence)
        p2 = plan_portable_job(self.recipe, replace(self.request, inputs=dict(reversed(list(self.request.inputs.items())))), self.evidence)
        p3 = plan_portable_job(self.recipe, replace(self.request, attempt_id="attempt-2"), self.evidence)
        self.assertEqual(p1.request_digest, p2.request_digest)
        self.assertNotEqual(p1.request_digest, p3.request_digest)
        self.assertEqual(p1.request_digest, plan_portable_job(self.recipe, self.request, replace(self.evidence, actions="CONTROL_OUTAGE")).request_digest)

    def test_no_transport_effects_or_secret_material_in_plan(self) -> None:
        plan = plan_portable_job(self.recipe, self.request, self.evidence)
        self.assertEqual(set(vars(plan)), {"disposition", "reason", "backend", "request_digest"})
        self.assertNotIn("audit", repr(plan))
        self.assertNotIn("kinoko", repr(plan))

    def test_invalid_health_states_not_treated_as_outage(self) -> None:
        for evidence in (
            replace(self.evidence, actions="BILLING_REJECTED_BY_SECURITY"),
            replace(self.evidence, actions=1),
            replace(self.evidence, actions=[]),
            replace(self.evidence, external={}),
            replace(self.evidence, github_api=[]),
            replace(self.evidence, coordinator_claim={}),
            replace(self.evidence, external="MAYBE"),
            replace(self.evidence, caller_authorized=1),
        ):
            with self.subTest(evidence=evidence):
                self.assertEqual(plan_portable_job(self.recipe, self.request, evidence).reason, "INVALID_EVIDENCE")


if __name__ == "__main__":
    unittest.main()
