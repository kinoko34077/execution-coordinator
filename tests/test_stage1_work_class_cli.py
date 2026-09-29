from __future__ import annotations

import io
import types
import unittest
from unittest.mock import patch

from execution_coordinator.bootstrap_pickup import main


class _ContractError(ValueError):
    pass


def _tools():
    return types.SimpleNamespace(
        chat_worker_profile=types.SimpleNamespace(OBSERVATION_SCHEMA="chat-worker-observation.v1"),
        chat_worker_bootstrap=types.SimpleNamespace(ContractError=_ContractError),
    )


class Stage1WorkClassCliTests(unittest.TestCase):
    def _patch_cli(self):
        outcome = {
            "result": {"claim_required": False, "disposition": "NO_ELIGIBLE_WORK"},
            "claim_id": None,
        }
        return (
            patch("execution_coordinator.bootstrap_pickup.load_devflow_tools", return_value=_tools()),
            patch(
                "execution_coordinator.bootstrap_pickup.load_session",
                return_value={"worker_session_id": "chatgpt-20260929T081500Z-abc123", "cycle": 1},
            ),
            patch("execution_coordinator.bootstrap_pickup.probe_environment", return_value=({}, True)),
            patch("execution_coordinator.bootstrap_pickup.list_control_documents", return_value=()),
            patch("execution_coordinator.bootstrap_pickup.run_pickup", return_value=outcome),
            patch("execution_coordinator.github_state.GitHubStateStore"),
            patch("execution_coordinator.discovery.GitHubIssueReader"),
            patch("builtins.print"),
        )

    def test_pickup_passes_repeatable_work_classes_to_run_pickup(self):
        patches = self._patch_cli()
        with patches[0], patches[1], patches[2], patches[3], patches[4] as run_pickup_mock, patches[5], patches[6], patches[7]:
            code = main(
                [
                    "pickup",
                    "--worker-system",
                    "chatgpt",
                    "--devflow",
                    "/tmp/devflow",
                    "--work-class",
                    "quickfix",
                    "--work-class",
                    "audit",
                ]
            )
        self.assertEqual(0, code)
        self.assertEqual(("quickfix", "audit"), run_pickup_mock.call_args.kwargs["accepted_work_classes"])

    def test_invalid_work_class_contract_error_is_bounded_cli_error(self):
        patches = self._patch_cli()
        with patches[0], patches[1], patches[2], patches[3], patches[4] as run_pickup_mock, patches[5], patches[6], patches[7], patch(
            "sys.stderr", new_callable=io.StringIO
        ) as stderr:
            run_pickup_mock.side_effect = _ContractError("accepted_work_classes has an unknown work class")
            with self.assertRaises(SystemExit) as raised:
                main(
                    [
                        "pickup",
                        "--worker-system",
                        "chatgpt",
                        "--devflow",
                        "/tmp/devflow",
                        "--work-class",
                        "bogus",
                    ]
                )
        self.assertEqual(2, raised.exception.code)
        self.assertIn("unknown work class", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
