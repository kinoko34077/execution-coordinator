from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch, sentinel
from types import SimpleNamespace

from execution_coordinator import mutate


class SerializedChatBoundaryCLITests(unittest.TestCase):
    """No live GitHub calls; verify dispatch authentication precedes state mutation."""

    def _run(self, operation: str, payload: dict[str, object]):
        argv = ["--operation", operation, "--payload-json", json.dumps(payload), "--idempotency-key", "test-key"]
        env = {"GITHUB_ACTOR_ID": "79015263", "GITHUB_ACTOR": "kinoko34077",
               "GITHUB_TOKEN": "fake", "GITHUB_REPOSITORY": "kinoko34077/execution-coordinator",
               "STATE_ISSUE_NUMBER": "3", "DEVFLOW_PATH": "devflow"}
        with patch.dict(os.environ, env, clear=True), \
             patch("execution_coordinator.chat_authority.authorize_internal_release_dispatch") as dispatch, \
             patch("execution_coordinator.chat_authority.verify_chat_origin") as origin, \
             patch.object(mutate, "_build_store_from_env", return_value=sentinel.store) as store, \
             patch.object(mutate, "apply_mutation", return_value=SimpleNamespace(
                 claim_id="clm_test", generation=1, replayed=False)) as commit:
            dispatch.return_value = 79015263
            origin.return_value = 79015263
            result = mutate.main(argv)
            return result, dispatch, origin, store, commit

    def test_chat_release_overwrites_dispatch_actor_with_comment_actor(self):
        result, dispatch, origin, store, commit = self._run(
            "chat_release", {"claim_id": "clm_test", "generation": 1,
                             "session": "test-session", "source_comment_id": 123})
        self.assertEqual(result, 0)
        dispatch.assert_called_once()
        origin.assert_called_once()
        self.assertEqual(origin.call_args.kwargs["command"], "release")
        self.assertEqual(origin.call_args.kwargs["claim_id"], "clm_test")
        self.assertEqual(origin.call_args.kwargs["session"], "test-session")
        self.assertEqual(commit.call_args.kwargs["verified_actor_id"], 79015263)

    def test_chat_claim_requires_comment_origin(self):
        result, _, origin, _, commit = self._run(
            "chat_claim", {"task": "kinoko34077/example#1", "role": "implementer",
                           "worker_id": "w", "source_comment_id": 123})
        self.assertEqual(result, 0)
        self.assertEqual(origin.call_args.kwargs["command"], "pickup")
        self.assertEqual(commit.call_args.kwargs["verified_actor_id"], 79015263)

    def test_internal_release_does_not_accept_comment_as_actor(self):
        result, dispatch, origin, _, commit = self._run(
            "release", {"claim_id": "clm_test", "generation": 1})
        self.assertEqual(result, 0)
        dispatch.assert_called_once()
        origin.assert_not_called()
        self.assertEqual(commit.call_args.kwargs["verified_actor_id"], 79015263)

    def test_rejected_dispatch_never_builds_store_or_commits(self):
        argv = ["--operation", "chat_release",
                "--payload-json", '{"claim_id":"clm_test","generation":1,"source_comment_id":123}',
                "--idempotency-key", "test"]
        with patch("execution_coordinator.chat_authority.authorize_internal_release_dispatch",
                   side_effect=ValueError("untrusted dispatcher")), \
             patch("execution_coordinator.chat_authority.verify_chat_origin") as origin, \
             patch.object(mutate, "_build_store_from_env") as store, \
             patch.object(mutate, "apply_mutation") as commit:
            self.assertEqual(mutate.main(argv), 2)
            origin.assert_not_called()
            store.assert_not_called()
            commit.assert_not_called()

    def test_rejected_origin_never_commits(self):
        argv = ["--operation", "chat_release",
                "--payload-json", '{"claim_id":"clm_test","generation":1,"source_comment_id":123}',
                "--idempotency-key", "test"]
        with patch("execution_coordinator.chat_authority.authorize_internal_release_dispatch",
                   return_value=79015263), \
             patch("execution_coordinator.chat_authority.verify_chat_origin",
                   side_effect=ValueError("untrusted origin")), \
             patch.object(mutate, "_build_store_from_env") as store, \
             patch.object(mutate, "apply_mutation") as commit:
            self.assertEqual(mutate.main(argv), 2)
            store.assert_not_called()
            commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
