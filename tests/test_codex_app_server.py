from __future__ import annotations

import os
import unittest
from dataclasses import replace
from unittest.mock import patch

from execution_coordinator.codex_app_server import (
    CodexAppServerAdapter,
    CodexAppServerProtocolError,
    CodexTurnStatus,
)
from execution_coordinator.execution_request import LaunchStatus
from tests.test_execution_request import _bootstrap, _request


class _FakeTransport:
    def __init__(self, incoming):
        self.incoming = list(incoming)
        self.sent = []
        self.closed = False

    def send(self, message):
        self.sent.append(message)

    def receive(self):
        if not self.incoming:
            return None
        value = self.incoming.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def close(self):
        self.closed = True


class _Factory:
    def __init__(self, incoming):
        self.incoming = incoming
        self.commands = []
        self.transport = None

    def __call__(self, command):
        self.commands.append(tuple(command))
        self.transport = _FakeTransport(self.incoming)
        return self.transport


def _codex_request(
    *,
    resume_thread_id: str | None = None,
    cwd: str | None = "/work",
    codex_sandbox: str | None = "read-only",
    codex_approval_policy: str | None = "never",
):
    request = _request()
    parameters = [("mode", "bounded"), ("model", "gpt-test")]
    if cwd is not None:
        parameters.append(("cwd", cwd))
    if codex_sandbox is not None:
        parameters.append(("codex_sandbox", codex_sandbox))
    if codex_approval_policy is not None:
        parameters.append(("codex_approval_policy", codex_approval_policy))
    if resume_thread_id is not None:
        parameters.append(("resume_thread_id", resume_thread_id))
    return replace(
        request,
        bootstrap=replace(_bootstrap(), parameters=tuple(parameters)),
    )


class CodexAppServerAdapterTests(unittest.TestCase):
    def test_missing_access_token_is_unavailable_without_starting_process(self) -> None:
        factory = _Factory([])
        adapter = CodexAppServerAdapter(transport_factory=factory)

        with patch.dict(os.environ, {}, clear=True):
            outcome = adapter.start(_codex_request())

        self.assertEqual(LaunchStatus.UNAVAILABLE, outcome.launch_status)
        self.assertEqual([], factory.commands)

    def test_start_establishes_thread_context_without_starting_work_turn(self) -> None:
        factory = _Factory([
            {"id": 1, "result": {"userAgent": "test"}},
            {"id": 2, "result": {"thread": {"id": "thread-1"}}},
        ])
        adapter = CodexAppServerAdapter(transport_factory=factory)

        with patch.dict(os.environ, {"ACCESS_TOKEN": "secret-token"}, clear=True):
            outcome = adapter.start(_codex_request())

        self.assertEqual(LaunchStatus.ACCEPTED, outcome.launch_status)
        self.assertEqual("worker-a", outcome.worker_id)
        self.assertEqual("thread-1", outcome.session_id)
        self.assertEqual(
            ["initialize", "initialized", "thread/start"],
            [message.get("method") for message in factory.transport.sent],
        )
        self.assertNotIn("turn/start", [message.get("method") for message in factory.transport.sent])
        thread_start = factory.transport.sent[-1]
        self.assertEqual("/work", thread_start["params"]["cwd"])
        self.assertEqual("read-only", thread_start["params"]["sandbox"])
        self.assertEqual("never", thread_start["params"]["approvalPolicy"])
        command_text = " ".join(factory.commands[0])
        self.assertIn('env_key="ACCESS_TOKEN"', command_text)
        self.assertIn(
            "shell_environment_policy.ignore_default_excludes=false",
            command_text,
        )
        self.assertNotIn("secret-token", command_text)
        self.assertNotIn("secret-token", repr(adapter))

    def test_resume_uses_explicit_saved_thread_id_and_does_not_refresh_tokens(self) -> None:
        factory = _Factory([
            {"id": 1, "result": {}},
            {"id": 2, "result": {"thread": {"id": "thread-saved"}}},
        ])
        adapter = CodexAppServerAdapter(transport_factory=factory)

        with patch.dict(os.environ, {"ACCESS_TOKEN": "token-2"}, clear=True):
            outcome = adapter.start(_codex_request(resume_thread_id="thread-saved"))

        self.assertEqual(LaunchStatus.ACCEPTED, outcome.launch_status)
        methods = [message.get("method") for message in factory.transport.sent]
        self.assertEqual(["initialize", "initialized", "thread/resume"], methods)
        resume = factory.transport.sent[-1]
        self.assertEqual("thread-saved", resume["params"]["threadId"])
        self.assertEqual("/work", resume["params"]["cwd"])
        self.assertEqual("read-only", resume["params"]["sandbox"])
        self.assertEqual("never", resume["params"]["approvalPolicy"])

    def test_runtime_policy_is_explicit_and_fails_closed_before_process_start(self) -> None:
        for request, reason in (
            (_codex_request(cwd=None), "cwd"),
            (_codex_request(codex_sandbox=None), "codex_sandbox"),
            (_codex_request(codex_approval_policy=None), "codex_approval_policy"),
            (_codex_request(codex_sandbox="danger-full-access"), "codex_sandbox"),
            (_codex_request(codex_approval_policy="on-request"), "codex_approval_policy"),
        ):
            with self.subTest(reason=reason):
                factory = _Factory([])
                adapter = CodexAppServerAdapter(transport_factory=factory)
                with patch.dict(os.environ, {"ACCESS_TOKEN": "token"}, clear=True):
                    outcome = adapter.start(request)
                self.assertEqual(LaunchStatus.FAILED, outcome.launch_status)
                self.assertIn(reason, outcome.reason)
                self.assertEqual([], factory.commands)

    def test_thread_start_eof_is_ambiguous_and_never_retried(self) -> None:
        factory = _Factory([
            {"id": 1, "result": {}},
            None,
        ])
        adapter = CodexAppServerAdapter(transport_factory=factory)

        with patch.dict(os.environ, {"ACCESS_TOKEN": "token"}, clear=True):
            outcome = adapter.start(_codex_request())

        self.assertEqual(LaunchStatus.AMBIGUOUS, outcome.launch_status)
        self.assertEqual(
            1,
            [message.get("method") for message in factory.transport.sent].count("thread/start"),
        )

    def test_completed_turn_requires_matching_terminal_event(self) -> None:
        factory = _Factory([
            {"id": 1, "result": {}},
            {"id": 2, "result": {"thread": {"id": "thread-1"}}},
            {"id": 3, "result": {"turn": {"id": "turn-1"}}},
            {"method": "item/agentMessage/delta", "params": {"delta": "hello"}},
            {
                "method": "turn/completed",
                "params": {"turn": {"id": "turn-1", "status": "completed"}},
            },
        ])
        adapter = CodexAppServerAdapter(transport_factory=factory)
        with patch.dict(os.environ, {"ACCESS_TOKEN": "token"}, clear=True):
            outcome = adapter.start(_codex_request())
            result = adapter.run_turn(outcome.session_id, "Do bounded work")

        self.assertEqual(CodexTurnStatus.COMPLETED, result.status)
        self.assertEqual("thread-1", result.thread_id)
        self.assertEqual("turn-1", result.turn_id)
        self.assertEqual(1, [m.get("method") for m in factory.transport.sent].count("turn/start"))

    def test_failed_and_interrupted_terminal_statuses_are_not_success(self) -> None:
        for provider_status, expected in (
            ("failed", CodexTurnStatus.FAILED),
            ("interrupted", CodexTurnStatus.INTERRUPTED),
        ):
            with self.subTest(provider_status=provider_status):
                factory = _Factory([
                    {"id": 1, "result": {}},
                    {"id": 2, "result": {"thread": {"id": "thread-1"}}},
                    {"id": 3, "result": {"turn": {"id": "turn-1"}}},
                    {
                        "method": "turn/completed",
                        "params": {"turn": {"id": "turn-1", "status": provider_status}},
                    },
                ])
                adapter = CodexAppServerAdapter(transport_factory=factory)
                with patch.dict(os.environ, {"ACCESS_TOKEN": "token"}, clear=True):
                    outcome = adapter.start(_codex_request())
                    result = adapter.run_turn(outcome.session_id, "Do work")
                self.assertEqual(expected, result.status)

    def test_eof_after_turn_start_is_ambiguous_and_not_retried(self) -> None:
        factory = _Factory([
            {"id": 1, "result": {}},
            {"id": 2, "result": {"thread": {"id": "thread-1"}}},
            {"id": 3, "result": {"turn": {"id": "turn-1"}}},
            None,
        ])
        adapter = CodexAppServerAdapter(transport_factory=factory)
        with patch.dict(os.environ, {"ACCESS_TOKEN": "token"}, clear=True):
            outcome = adapter.start(_codex_request())
            result = adapter.run_turn(outcome.session_id, "Do work")

        self.assertEqual(CodexTurnStatus.AMBIGUOUS, result.status)
        self.assertEqual(1, [m.get("method") for m in factory.transport.sent].count("turn/start"))

    def test_transport_loss_after_turn_start_is_ambiguous(self) -> None:
        factory = _Factory([
            {"id": 1, "result": {}},
            {"id": 2, "result": {"thread": {"id": "thread-1"}}},
            {"id": 3, "result": {"turn": {"id": "turn-1"}}},
            OSError("transport lost"),
        ])
        adapter = CodexAppServerAdapter(transport_factory=factory)
        with patch.dict(os.environ, {"ACCESS_TOKEN": "token"}, clear=True):
            outcome = adapter.start(_codex_request())
            result = adapter.run_turn(outcome.session_id, "Do work")

        self.assertEqual(CodexTurnStatus.AMBIGUOUS, result.status)
        self.assertIn("transport lost", result.reason)
        self.assertEqual(1, [m.get("method") for m in factory.transport.sent].count("turn/start"))

    def test_terminal_notification_before_turn_start_response_is_preserved(self) -> None:
        factory = _Factory([
            {"id": 1, "result": {}},
            {"id": 2, "result": {"thread": {"id": "thread-1"}}},
            {
                "method": "turn/completed",
                "params": {"turn": {"id": "turn-1", "status": "completed"}},
            },
            {"id": 3, "result": {"turn": {"id": "turn-1"}}},
        ])
        adapter = CodexAppServerAdapter(transport_factory=factory)
        with patch.dict(os.environ, {"ACCESS_TOKEN": "token"}, clear=True):
            outcome = adapter.start(_codex_request())
            result = adapter.run_turn(outcome.session_id, "Do work")

        self.assertEqual(CodexTurnStatus.COMPLETED, result.status)
        self.assertEqual("turn-1", result.turn_id)

    def test_ambiguous_turn_poison_session_until_reconciliation(self) -> None:
        factory = _Factory([
            {"id": 1, "result": {}},
            {"id": 2, "result": {"thread": {"id": "thread-1"}}},
            {"id": 3, "result": {"turn": {"id": "turn-1"}}},
            None,
        ])
        adapter = CodexAppServerAdapter(transport_factory=factory)
        with patch.dict(os.environ, {"ACCESS_TOKEN": "token"}, clear=True):
            outcome = adapter.start(_codex_request())
            result = adapter.run_turn(outcome.session_id, "Do work")
            with self.assertRaisesRegex(RuntimeError, "reconciliation"):
                adapter.run_turn(outcome.session_id, "Do work again")

        self.assertEqual(CodexTurnStatus.AMBIGUOUS, result.status)
        self.assertEqual(
            1,
            [m.get("method") for m in factory.transport.sent].count("turn/start"),
        )

    def test_malformed_terminal_event_fails_closed(self) -> None:
        factory = _Factory([
            {"id": 1, "result": {}},
            {"id": 2, "result": {"thread": {"id": "thread-1"}}},
            {"id": 3, "result": {"turn": {"id": "turn-1"}}},
            {"method": "turn/completed", "params": {"turn": {"status": "completed"}}},
        ])
        adapter = CodexAppServerAdapter(transport_factory=factory)
        with patch.dict(os.environ, {"ACCESS_TOKEN": "token"}, clear=True):
            outcome = adapter.start(_codex_request())
            with self.assertRaises(CodexAppServerProtocolError):
                adapter.run_turn(outcome.session_id, "Do work")


if __name__ == "__main__":
    unittest.main()
