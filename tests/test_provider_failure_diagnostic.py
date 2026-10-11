from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from execution_coordinator.provider_failure_diagnostic import (
    ERROR_CLASSES,
    EXECUTION_FILENAME,
    MAX_EXECUTION_FILE_BYTES,
    NO_HTTP_STATUS,
    UNAVAILABLE,
    classify_execution_file,
    main,
)


class ProviderFailureDiagnosticTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.file = self.directory / EXECUTION_FILENAME

    def write(self, messages: object) -> None:
        self.file.write_text(json.dumps(messages), encoding="utf-8")

    def classify(self) -> str:
        return classify_execution_file(str(self.directory))

    def test_rate_limit_is_a_category_not_provider_text(self) -> None:
        self.write(
            [
                {"type": "system", "subtype": "init", "sensitive": "SECRET_SENTINEL"},
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": True,
                    "api_error_status": 429,
                    "result": "SECRET_SENTINEL: You've hit your session limit",
                    "session_id": "SECRET_SESSION",
                },
            ]
        )
        self.assertEqual(self.classify(), "HTTP_429")
        with patch.dict(os.environ, {"RUNNER_TEMP": str(self.directory)}):
            with redirect_stdout(io.StringIO()) as captured:
                self.assertEqual(main(), 0)
        self.assertEqual(captured.getvalue(), "claude_bootstrap_error_class=HTTP_429\n")
        self.assertNotIn("SECRET", captured.getvalue())
        self.assertNotIn("session", captured.getvalue())

    def test_other_http_error_buckets(self) -> None:
        for status, expected in (
            (401, "HTTP_401_403"),
            (403, "HTTP_401_403"),
            (404, "HTTP_OTHER"),
            (418, "HTTP_OTHER"),
            (500, "HTTP_5XX"),
            (503, "HTTP_5XX"),
        ):
            with self.subTest(status=status):
                self.write([{"type": "result", "api_error_status": status}])
                self.assertEqual(self.classify(), expected)
                self.assertIn(self.classify(), ERROR_CLASSES)

    def test_invalid_http_status_does_not_echo_payload(self) -> None:
        for status in (None, True, False, 429.0, "429", -1, 99, 600, [], {}):
            with self.subTest(value=repr(status)):
                self.write(
                    [{"type": "result", "api_error_status": status, "result": "SECRET"}]
                )
                self.assertEqual(self.classify(), NO_HTTP_STATUS)

    def test_last_result_wins_without_using_assistant_message(self) -> None:
        self.write(
            [
                {"type": "result", "api_error_status": 401},
                {"type": "assistant", "api_error_status": 429},
                {"type": "result", "api_error_status": 503},
            ]
        )
        self.assertEqual(self.classify(), "HTTP_5XX")

    def test_missing_malformed_and_untrusted_paths_fail_closed(self) -> None:
        self.assertEqual(self.classify(), UNAVAILABLE)
        self.assertEqual(classify_execution_file(None), UNAVAILABLE)
        self.assertEqual(classify_execution_file("relative/path"), UNAVAILABLE)
        self.file.write_text("{bad JSON SECRET", encoding="utf-8")
        self.assertEqual(self.classify(), UNAVAILABLE)
        self.write({"type": "result", "api_error_status": 429})
        self.assertEqual(self.classify(), UNAVAILABLE)
        self.write(["not a message", {"type": "system", "api_error_status": 429}])
        self.assertEqual(self.classify(), NO_HTTP_STATUS)

    def test_extremely_deep_json_recursion_is_unavailable(self) -> None:
        # Prevent malformed SDK content from producing a traceback, including
        # file paths or snippets, in the Actions failure diagnostics.
        self.file.write_text(
            "[" * 2500 + '"SECRET_SENTINEL"' + "]" * 2500,
            encoding="utf-8",
        )
        self.assertEqual(self.classify(), UNAVAILABLE)

    def test_oversized_execution_file_is_not_parsed(self) -> None:
        self.file.write_bytes(b"x" * (MAX_EXECUTION_FILE_BYTES + 1))
        self.assertEqual(self.classify(), UNAVAILABLE)

    def test_file_symlink_cannot_redirect_diagnostic(self) -> None:
        outside = self.directory / "outside"
        outside.write_text('[{"type":"result","api_error_status":429}]')
        try:
            self.file.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        self.assertEqual(self.classify(), UNAVAILABLE)

    def test_workflow_diagnostic_is_gated_and_nonblocking(self) -> None:
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "controller-auto-launch.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("Classify Claude bootstrap error without sensitive output", workflow)
        diagnostic = workflow.split(
            "      - name: Classify Claude bootstrap error without sensitive output", 1
        )[1].split("      - name: Normalize Claude bootstrap evidence", 1)[0]
        self.assertIn("steps.claude-bootstrap.outcome == 'failure'", diagnostic)
        self.assertIn("steps.claim.outputs.result == 'ACCEPTED'", diagnostic)
        self.assertIn("continue-on-error: true", diagnostic)
        self.assertIn("PYTHONSAFEPATH: '1'", diagnostic)
        self.assertIn(
            "python -m execution_coordinator.provider_failure_diagnostic", diagnostic
        )
        self.assertNotIn("show_full_output", diagnostic)
        self.assertNotIn("execution_file", diagnostic)


if __name__ == "__main__":
    unittest.main()
