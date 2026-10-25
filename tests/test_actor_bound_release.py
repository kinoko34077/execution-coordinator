from __future__ import annotations

import io
import json
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from execution_coordinator.chat_authority import ChatOriginRejected, verify_chat_origin
from execution_coordinator.engine import InvalidTransition, StaleGeneration, claim, expire, release
from execution_coordinator.model import CoordinatorState, Role
from execution_coordinator.mutate import _apply_to_state
from execution_coordinator.snapshot import render_issue_body, parse_issue_body

T0 = datetime(2026, 10, 25, 0, tzinfo=timezone.utc)
TASK = "kinoko34077/example#1"
COMMENT_ID = 12345
ID = 79015263


def new_chat_claim(owner=ID):
    return claim(
        CoordinatorState.empty(), task=TASK, role=Role.IMPLEMENTER,
        worker_id="chatgpt:session", conflict_keys=(), now=T0,
        idempotency_key="claim", claim_source="github_comment", pickup_actor_id=owner,
    )


class ActorBoundReleaseTests(unittest.TestCase):
    def test_same_principal_releases_chat_claim(self):
        current = new_chat_claim()
        result = release(
            current.state, claim_id=current.claim_id, generation=current.generation,
            now=T0 + timedelta(minutes=1), idempotency_key="release",
            caller_actor_id=ID, chat_release=True,
        )
        self.assertNotIn(current.claim_id, result.state.claims)

    def test_other_actor_or_unverified_actor_cannot_release_known_tuple(self):
        current = new_chat_claim()
        for actor in (None, ID + 1, "79015263", True):
            with self.subTest(actor=actor), self.assertRaises(InvalidTransition):
                release(
                    current.state, claim_id=current.claim_id, generation=current.generation,
                    now=T0 + timedelta(minutes=1), idempotency_key="r",
                    caller_actor_id=actor, chat_release=True,
                )
        with self.assertRaises(InvalidTransition):
            release(
                current.state, claim_id=current.claim_id, generation=current.generation,
                now=T0 + timedelta(minutes=1), idempotency_key="internal-spoof",
            )

    def test_old_claim_is_readable_but_cannot_be_manually_released(self):
        internal = claim(
            CoordinatorState.empty(), task=TASK, role=Role.IMPLEMENTER,
            worker_id="old-worker", conflict_keys=(), now=T0, idempotency_key="c",
        )
        body = render_issue_body("state\n", internal.state)
        data = json.loads(body.split("EXECUTION_COORDINATOR_STATE_V1_BEGIN -->")[1].split(
            "<!-- EXECUTION_COORDINATOR_STATE_V1_END"
        )[0])
        legacy = data["claims"][internal.claim_id]
        legacy.pop("claim_source")
        legacy.pop("pickup_actor_id")
        body = render_issue_body("state\n", internal.state)
        from execution_coordinator.snapshot import state_from_data
        old = state_from_data(data)
        self.assertEqual("legacy", old.claims[internal.claim_id].claim_source)
        with self.assertRaises(InvalidTransition):
            release(
                old, claim_id=internal.claim_id, generation=internal.generation,
                now=T0 + timedelta(minutes=1), idempotency_key="legacy-release",
            )
        self.assertNotIn(internal.claim_id, expire(
            old, now=T0 + timedelta(minutes=16), idempotency_key="expire"
        ).state.claims)

    def test_chat_claim_snapshot_roundtrip_preserves_owner(self):
        current = new_chat_claim()
        parsed = parse_issue_body(render_issue_body("header\n", current.state))
        self.assertEqual(ID, parsed.claims[current.claim_id].pickup_actor_id)

    def test_stale_generation_fences_same_actor(self):
        current = new_chat_claim()
        with self.assertRaises(StaleGeneration):
            release(
                current.state, claim_id=current.claim_id,
                generation=current.generation + 1, now=T0,
                idempotency_key="stale", caller_actor_id=ID, chat_release=True,
            )

    def test_serialized_chat_release_does_not_trust_client_actor_id(self):
        current = new_chat_claim()
        payload = {"claim_id": current.claim_id, "generation": current.generation,
                   "source_comment_id": COMMENT_ID, "session": "forged",
                   "actor_id": ID}
        with self.assertRaises(InvalidTransition):
            _apply_to_state(
                current.state, operation="chat_release", payload=payload,
                now=T0 + timedelta(minutes=1), idempotency_key="unverified",
            )
        with self.assertRaises(InvalidTransition):
            _apply_to_state(
                current.state, operation="chat_release", payload=payload,
                now=T0 + timedelta(minutes=1), idempotency_key="different",
                verified_actor_id=ID + 1,
            )
        success = _apply_to_state(
            current.state, operation="chat_release", payload=payload,
            now=T0 + timedelta(minutes=1), idempotency_key="same",
            verified_actor_id=ID,
        )
        self.assertNotIn(current.claim_id, success.state.claims)


class _FakeResponse:
    def __init__(self, data):
        self._data = json.dumps(data).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._data


class GitHubOriginTests(unittest.TestCase):
    def comment(self, command="/release", actor=ID):
        return {
            "id": COMMENT_ID,
            "issue_url": "https://api.github.com/repos/kinoko34077/execution-coordinator/issues/83",
            "user": {"id": actor, "login": "kinoko34077"},
            "body": command + "\nsession: s\nclaim_id: clm_" + "a"*32 + "\ngeneration: 1",
        }

    def verify(self, comment, *, expected="release"):
        def importer(name):
            self.assertEqual("tools.github_issue_trust", name)
            return types.SimpleNamespace(is_trusted_principal=lambda c: (
                type(c.get("user", {}).get("id")) is int
                and c["user"]["id"] == ID
            ))
        with patch("execution_coordinator.chat_authority.importlib.import_module", importer):
            return verify_chat_origin(
                COMMENT_ID, command=expected, claim_id="clm_"+"a"*32,
                generation=1, session="s", token="test-token",
                devflow_path=".", opener=lambda req, timeout: _FakeResponse(comment),
            )

    def test_source_owner_authenticated(self):
        self.assertEqual(ID, self.verify(self.comment()))

    def test_rejected_foreign_or_missing_or_malformed_actor(self):
        for actor in (None, True, str(ID), ID + 1):
            with self.subTest(actor=actor), self.assertRaises(ChatOriginRejected):
                self.verify(self.comment(actor=actor))

    def test_wrong_repo_command_or_tuple_rejected(self):
        bad_repo = self.comment()
        bad_repo["issue_url"] = "https://api.github.com/repos/attacker/repo/issues/83"
        bad_command = self.comment(command="/pickup")
        bad_tuple = self.comment()
        bad_tuple["body"] = bad_tuple["body"].replace("generation: 1", "generation: 2")
        for item in (bad_repo, bad_command, bad_tuple):
            with self.assertRaises(ChatOriginRejected):
                self.verify(item)


if __name__ == "__main__":
    unittest.main()
