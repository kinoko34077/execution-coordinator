from __future__ import annotations

import io
import json
import os
import re
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
    format_http_status,
    inspect_execution_file,
    main,
)

OUTPUT_FORMAT = re.compile(
    r"claude_bootstrap_error_class=[A-Z0-9_]+\n"
    r"claude_bootstrap_http_status=(?:[1-5][0-9]{2}|UNAVAILABLE)\n"
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
        self.assertEqual(
            captured.getvalue(),
            "claude_bootstrap_error_class=HTTP_429\n"
            "claude_bootstrap_http_status=429\n",
        )
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

    def test_extremely_deep_json_never_exposes_status_or_payload(self) -> None:
        # The JSON parser's maximum depth varies by Python version. A deep
        # array may decode successfully (NO_HTTP_STATUS) or raise RecursionError
        # (UNAVAILABLE); neither outcome is valid SDK HTTP-status evidence.
        self.file.write_text(
            "[" * 2500 + '"SECRET_SENTINEL"' + "]" * 2500,
            encoding="utf-8",
        )
        category, status = inspect_execution_file(str(self.directory))
        self.assertIn(category, (NO_HTTP_STATUS, UNAVAILABLE))
        self.assertIsNone(status)
        output = self.run_main()
        self.assertIn("claude_bootstrap_http_status=UNAVAILABLE\\n", output)
        self.assertNotIn("SECRET_SENTINEL", output)
        self.assertNotIn("Traceback", output)

        # Exercise both parser outcomes deterministically, independently of
        # the interpreter hosting this test suite.
        with patch(
            "execution_coordinator.provider_failure_diagnostic.json.loads",
            return_value=[["SECRET_SENTINEL"]],
        ):
            self.assertEqual(
                inspect_execution_file(str(self.directory)),
                (NO_HTTP_STATUS, None),
            )
        with patch(
            "execution_coordinator.provider_failure_diagnostic.json.loads",
            side_effect=RecursionError,
        ):
            self.assertEqual(
                inspect_execution_file(str(self.directory)),
                (UNAVAILABLE, None),
            )

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

    def run_main(self) -> str:
        with patch.dict(os.environ, {"RUNNER_TEMP": str(self.directory)}):
            with redirect_stdout(io.StringIO()) as captured:
                self.assertEqual(main(), 0)
        output = captured.getvalue()
        self.assertRegex(output, OUTPUT_FORMAT)
        self.assertEqual(len(output.splitlines()), 2)
        return output

    def test_exact_numeric_status_is_emitted_with_the_unchanged_category(self) -> None:
        for status, expected in (
            (100, "HTTP_OTHER"),
            (400, "HTTP_OTHER"),
            (401, "HTTP_401_403"),
            (403, "HTTP_401_403"),
            (404, "HTTP_OTHER"),
            (422, "HTTP_OTHER"),
            (429, "HTTP_429"),
            (500, "HTTP_5XX"),
            (503, "HTTP_5XX"),
            (599, "HTTP_5XX"),
        ):
            with self.subTest(status=status):
                self.write(
                    [
                        {
                            "type": "result",
                            "api_error_status": status,
                            "result": "SECRET_SENTINEL",
                            "session_id": "SECRET_SESSION",
                        }
                    ]
                )
                self.assertEqual(inspect_execution_file(str(self.directory)), (expected, status))
                self.assertEqual(self.classify(), expected)
                output = self.run_main()
                self.assertEqual(
                    output,
                    f"claude_bootstrap_error_class={expected}\n"
                    f"claude_bootstrap_http_status={status:03d}\n",
                )
                self.assertNotIn("SECRET", output)

    def test_invalid_status_types_and_ranges_emit_only_the_sentinel(self) -> None:
        class IntSubclass(int):
            pass

        bad_values = (None, True, False, 429.0, "429", " 429", -1, 0, 99, 600, 2**70, [], {}, "SECRET_SENTINEL")
        for status in bad_values:
            with self.subTest(value=repr(status)):
                self.write([{"type": "result", "api_error_status": status, "result": "SECRET_SENTINEL"}])
                self.assertEqual(inspect_execution_file(str(self.directory)), (NO_HTTP_STATUS, None))
                output = self.run_main()
                self.assertEqual(
                    output,
                    "claude_bootstrap_error_class=NO_HTTP_STATUS\n"
                    "claude_bootstrap_http_status=UNAVAILABLE\n",
                )
                self.assertNotIn("SECRET", output)
        # json.loads never yields an int subclass, so exercise the formatter
        # (the last gate before the log) directly.
        for bad in (IntSubclass(429), True, False, None, 99, 600, "429", 429.0, [], {}, -5):
            with self.subTest(formatter=repr(bad)):
                self.assertEqual(format_http_status(bad), UNAVAILABLE)
        self.assertEqual(format_http_status(100), "100")
        self.assertEqual(format_http_status(599), "599")

    def test_status_comes_from_the_last_result_message_only(self) -> None:
        self.write(
            [
                {"type": "result", "api_error_status": 401},
                {"type": "assistant", "api_error_status": 429},
                {"type": "result", "api_error_status": 503},
                {"type": "system", "api_error_status": 404},
            ]
        )
        self.assertEqual(inspect_execution_file(str(self.directory)), ("HTTP_5XX", 503))
        self.assertIn("claude_bootstrap_http_status=503\n", self.run_main())
        # A later result without a valid status must not reuse an earlier one.
        self.write(
            [
                {"type": "result", "api_error_status": 429},
                {"type": "result", "api_error_status": None},
            ]
        )
        self.assertEqual(inspect_execution_file(str(self.directory)), (NO_HTTP_STATUS, None))
        self.assertIn("claude_bootstrap_http_status=UNAVAILABLE\n", self.run_main())

    def test_fail_closed_inputs_emit_unavailable_status_and_no_payload(self) -> None:
        sentinel_payload = '[{"type":"result","api_error_status":429,"result":"SECRET_SENTINEL"}'
        cases = {
            "missing": None,
            "malformed": "{bad JSON SECRET_SENTINEL",
            "not-a-list": json.dumps({"type": "result", "api_error_status": 429}),
            "oversized": "x" * (MAX_EXECUTION_FILE_BYTES + 1),
            "truncated": sentinel_payload,
            # Parser behavior on very deep input differs between Python 3.11
            # (RecursionError) and 3.12+ (parsed); both must stay fail-closed.
            "deep": "[" * 2500 + '"SECRET_SENTINEL"' + "]" * 2500,
        }
        for name, content in cases.items():
            with self.subTest(case=name):
                if self.file.exists() or self.file.is_symlink():
                    self.file.unlink()
                if content is not None:
                    self.file.write_text(content, encoding="utf-8")
                output = self.run_main()
                self.assertIn("claude_bootstrap_http_status=UNAVAILABLE\n", output)
                self.assertNotIn("SECRET", output)
                self.assertNotIn("Traceback", output)
        for runner_temp in (None, "", "relative/path"):
            with self.subTest(runner_temp=runner_temp):
                self.assertEqual(inspect_execution_file(runner_temp), (UNAVAILABLE, None))

    def test_symlinked_execution_file_never_yields_a_status(self) -> None:
        outside = self.directory / "outside"
        outside.write_text('[{"type":"result","api_error_status":429}]')
        try:
            self.file.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        self.assertEqual(inspect_execution_file(str(self.directory)), (UNAVAILABLE, None))
        self.assertIn("claude_bootstrap_http_status=UNAVAILABLE\n", self.run_main())

    def test_public_classifier_contract_is_unchanged(self) -> None:
        self.write([{"type": "result", "api_error_status": 404}])
        value = classify_execution_file(str(self.directory))
        self.assertIsInstance(value, str)
        self.assertEqual(value, "HTTP_OTHER")
        self.assertIn(value, ERROR_CLASSES)
        self.assertEqual(UNAVAILABLE, "UNAVAILABLE")

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
