from __future__ import annotations

from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from execution_coordinator.liveness_projection import project_liveness
from execution_coordinator.query import main as query_main

MAILBOX = "kinoko34077/devflow#519"
OBSERVED = datetime(2026, 10, 14, 17, 58, 26, tzinfo=timezone.utc)


def evidence(**updates):
    result = {
        "schema": "gpt-liveness-observation.v1",
        "source": "devflow/kaep-v0.2",
        "mailbox_ref": MAILBOX,
        "task_ref": MAILBOX,
        "session_id": "session_pilot20261014_1755",
        "turn_id": "turn_pilot20261014_1755",
        "event": "MODEL_TOOL_RESPONSE_OBSERVED",
        "observed_at": "2026-10-14T17:58:26Z",
        "valid_until": "2026-10-14T18:12:29Z",
        "evidence_url": "https://github.com/kinoko34077/devflow/issues/519#issuecomment-6174861252",
        "reported_actor": "kinoko34077",
        "producer_validation": "UPSTREAM_REPORTED",
        "claim_authority": False,
    }
    result.update(updates)
    return result


class LivenessProjectionTests(unittest.TestCase):
    def read(self, data=None, when=None, mailbox=MAILBOX):
        return project_liveness(
            evidence() if data is None else data,
            now=when if when is not None else OBSERVED + timedelta(minutes=2),
            expected_mailbox=mailbox,
        )

    def test_real_pilot_evidence_is_recent_but_not_provider_or_claim_proof(self):
        result = self.read()
        self.assertEqual(result["classification"], "RECENT_REPORTED_MODEL_RESPONSE")
        self.assertEqual(result["provenance"], "UNATTESTED_UPSTREAM_REPORT")
        self.assertEqual(result["age_seconds"], 120)
        self.assertFalse(result["agent_identity_verified"])
        self.assertFalse(result["gpt_currently_alive_proven"])
        self.assertFalse(result["claim_authority"])
        self.assertFalse(result["runtime_state_mutated"])
        self.assertEqual(result["allowed_claim_operations"], [])

    def test_past_answer_is_historical_not_alive(self):
        result = self.read(when=datetime(2026, 10, 15, 0, tzinfo=timezone.utc))
        self.assertEqual(result["classification"], "HISTORICAL_EVIDENCE")
        self.assertFalse(result["gpt_currently_alive_proven"])

    def test_mailbox_sidecar_never_equals_model(self):
        result = self.read(evidence(event="MAILBOX_ONLY", reported_actor="github-actions[bot]"))
        self.assertEqual(result["classification"], "RECENT_MAILBOX_ONLY")
        self.assertFalse(result["agent_identity_verified"])

    def test_long_running_is_declaration_only(self):
        result = self.read(evidence(event="LONG_RUNNING_DECLARED"))
        self.assertEqual(result["classification"], "RECENT_DECLARED_LONG_OPERATION")
        self.assertFalse(result["claim_authority"])

    def test_unanswered_evidence_is_not_death(self):
        result = self.read(evidence(event="NO_RESPONSE"))
        self.assertEqual(result["classification"], "RECENT_UNANSWERED_PROBE")
        self.assertFalse(result["gpt_currently_alive_proven"])

    def test_strict_source_schema_and_no_claim_grant(self):
        bad = (
            evidence(claim_authority=True),
            evidence(producer_validation="INDEPENDENT_MODEL_VERIFIED"),
            evidence(schema="other"),
            evidence(source="unknown"),
            evidence(unknown_field="injected"),
        )
        for item in bad:
            with self.subTest(item=item), self.assertRaises(ValueError):
                self.read(item)

    def test_source_scope_mismatch_and_invalid_url_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "MAILBOX_MISMATCH"):
            self.read(mailbox="kinoko34077/devflow#500")
        with self.assertRaisesRegex(ValueError, "EVIDENCE_TARGET_INVALID"):
            self.read(evidence(evidence_url="https://github.com/kinoko34077/devflow/issues/520#issuecomment-6174861252"))
        with self.assertRaisesRegex(ValueError, "EVIDENCE_URL_INVALID"):
            self.read(evidence(evidence_url="https://example.invalid/forged"))

    def test_timestamp_invariants(self):
        bad = (
            evidence(observed_at="2026-10-14T17:58:26+09:00"),
            evidence(observed_at="2026-10-14T18:10:26Z"),
            evidence(valid_until="2026-10-14T17:58:26Z"),
            evidence(valid_until="2026-10-14T21:58:26Z"),
        )
        for item in bad:
            with self.subTest(item=item), self.assertRaises(ValueError):
                self.read(item)

    def test_invalid_mode_and_bad_actor(self):
        with self.assertRaises(ValueError):
            self.read(evidence(event="BECOME_CONTROLLER"))
        with self.assertRaisesRegex(ValueError, "OBSERVATION_EVENT_INVALID"):
            self.read(evidence(event=["MODEL_TOOL_RESPONSE_OBSERVED"]))
        with self.assertRaises(ValueError):
            self.read(evidence(reported_actor="bad[bot]"))
        with self.assertRaises(ValueError):
            self.read(evidence(turn_id="too"))

    def test_query_cli_projection_does_not_read_or_mutate_runtime_state(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "observation.json"
            f.write_text(json.dumps(evidence()), encoding="utf-8")
            stdout = io.StringIO()
            with patch("execution_coordinator.query._build_store_from_env") as build, \
                 patch("execution_coordinator.query.datetime") as clock, \
                 redirect_stdout(stdout):
                clock.now.return_value = datetime(2026, 10, 15, 0, tzinfo=timezone.utc)
                status = query_main(["get_liveness", "--evidence-json", str(f),
                                     "--expected-mailbox", MAILBOX])
            self.assertEqual(status, 0)
            build.assert_not_called()
            record = json.loads(stdout.getvalue())
            self.assertEqual(record["classification"], "HISTORICAL_EVIDENCE")
            self.assertFalse(record["claim_authority"])

    def test_query_cli_rejects_missing_scope_without_runtime_read(self):
        stderr = io.StringIO()
        with patch("execution_coordinator.query._build_store_from_env") as build, redirect_stderr(stderr):
            self.assertEqual(query_main(["get_liveness"]), 2)
        build.assert_not_called()
        self.assertIn("requires", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
