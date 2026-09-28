from __future__ import annotations

import types
import unittest
from datetime import datetime, timezone
from pathlib import Path

from execution_coordinator.actions_pickup import (
    TRANSPORT_SURFACES,
    CommandRejected,
    build_observation,
    declared_probes,
    format_reply,
    handle,
    parse_command,
)

NOW = datetime(2026, 9, 28, 17, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


class _Profile:
    OBSERVATION_SCHEMA = "chat-worker-observation.v1"
    PROBES = {
        "exec.python3": ("capabilities", "python"),
        "exec.unittest": ("capabilities", "tests"),
        "os.linux": ("environment", "linux"),
        "surface.github_read": ("tool_surfaces", "github:read"),
        "surface.github_write": ("tool_surfaces", "github:write"),
        "surface.coordinator_claim": ("tool_surfaces", "coordinator:claim"),
    }

    @staticmethod
    def new_session_id(system, started_at, entropy):
        return f"{system}-{started_at.strftime('%Y%m%dT%H%M%SZ')}-{entropy}"


TOOLS = types.SimpleNamespace(chat_worker_profile=_Profile)
PICKUP = "/pickup\ntarget: kinoko34077/execution-coordinator\nworker_system: chatgpt\ncapabilities: python\nintent: このリポ側に合わせてなんか作業して"


def event(body, association="OWNER"):
    return {"comment": {"body": body, "author_association": association}, "issue": {"number": 1}}


class ParserTests(unittest.TestCase):
    def test_non_command_is_ignored(self):
        self.assertIsNone(parse_command("hello /pickup"))
        self.assertIsNone(parse_command(""))

    def test_pickup_fields(self):
        command, fields = parse_command(PICKUP)
        self.assertEqual("pickup", command)
        self.assertEqual("chatgpt", fields["worker_system"])
        self.assertEqual("このリポ側に合わせてなんか作業して", fields["intent"])

    def test_malformed_commands_are_rejected(self):
        cases = [
            "/pickup\nworker_system: claude",  # missing target
            "/pickup\ntarget: a/b\nworker_system: claude\nbogus: 1",
            "/pickup\ntarget: a/b\ntarget: c/d\nworker_system: claude",
            "/pickup\ntarget: a/b\nworker_system: claude\nsession: ghp_abc",
            "/pickup\ntarget a/b",
            "/release\nsession: s\nclaim_id: clm_x",  # missing generation
        ]
        for body in cases:
            with self.subTest(body=body):
                with self.assertRaises(CommandRejected):
                    parse_command(body)


class ProbeTests(unittest.TestCase):
    def test_declared_tags_only_and_transport_surfaces(self):
        probes = declared_probes({"capabilities": "python", "environment": "linux"}, _Profile)
        self.assertTrue(probes["exec.python3"])
        self.assertFalse(probes["exec.unittest"])
        self.assertTrue(probes["os.linux"])
        for surface in TRANSPORT_SURFACES:
            self.assertTrue(probes[surface])

    def test_actions_adds_no_capability(self):
        probes = declared_probes({}, _Profile)
        self.assertFalse(any(probes[p] for p in ("exec.python3", "exec.unittest", "os.linux")))

    def test_unknown_tag_is_rejected(self):
        with self.assertRaises(CommandRejected):
            declared_probes({"capabilities": "rust"}, _Profile)

    def test_observation_generates_session_when_absent(self):
        obs = build_observation({"worker_system": "chatgpt"}, _Profile, NOW)
        self.assertTrue(obs["worker_session_id"].startswith("chatgpt-20260928T170000Z-"))
        self.assertEqual(1, obs["cycle"])
        with self.assertRaises(CommandRejected):
            build_observation({"worker_system": "chatgpt", "cycle": "two"}, _Profile, NOW)


class HandleTests(unittest.TestCase):
    def _run(self, body, association="OWNER", outcome=None):
        calls = {"pickup": [], "release": []}

        def pickup(**kwargs):
            calls["pickup"].append(kwargs)
            return outcome or {"result": {"disposition": "NO_ELIGIBLE_WORK"}, "claim_id": None}

        def release(claim_id, generation, key):
            calls["release"].append((claim_id, generation, key))

        reply = handle(event(body, association), now=NOW, devflow_tools=TOOLS, run_pickup_fn=pickup, release_fn=release)
        return reply, calls

    def test_untrusted_author_is_ignored_silently(self):
        for association in ("NONE", "CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", ""):
            reply, calls = self._run(PICKUP, association)
            self.assertIsNone(reply)
            self.assertEqual([], calls["pickup"])

    def test_non_command_comment_is_ignored(self):
        reply, calls = self._run("thanks!")
        self.assertIsNone(reply)
        self.assertEqual([], calls["pickup"])

    def test_malformed_trusted_command_gets_typed_rejection_without_mutation(self):
        reply, calls = self._run("/pickup\nworker_system: claude")
        self.assertEqual(("REJECTED", "COMMAND_MALFORMED"), (reply["status"], reply["reason_code"]))
        self.assertEqual([], calls["pickup"])

    def test_pickup_without_claim(self):
        reply, calls = self._run(PICKUP)
        self.assertEqual("NO_CLAIM", reply["status"])
        self.assertEqual("kinoko34077/execution-coordinator", calls["pickup"][0]["target_repository"])
        self.assertTrue(calls["pickup"][0]["observation"]["probes"]["exec.python3"])
        self.assertIsNone(reply["release_command"])

    def test_pickup_with_claim_returns_release_command(self):
        session = types.SimpleNamespace(generation=1)
        outcome = {"result": {"disposition": "CLAIM_AND_WORK"}, "claim_id": "clm_" + "a" * 32, "session": session}
        reply, _ = self._run(PICKUP, outcome=outcome)
        self.assertEqual("CLAIMED", reply["status"])
        self.assertIn("/release", reply["release_command"])
        self.assertIn("clm_" + "a" * 32, reply["release_command"])
        self.assertEqual(2, reply["next_cycle"])

    def test_release(self):
        claim = "clm_" + "b" * 32
        reply, calls = self._run(f"/release\nsession: chatgpt-x\nclaim_id: {claim}\ngeneration: 3")
        self.assertEqual("RELEASED", reply["status"])
        self.assertEqual([(claim, 3, f"chatgpt-x:release:{claim}")], calls["release"])

    def test_release_with_bad_claim_id_is_rejected(self):
        reply, calls = self._run("/release\nsession: s\nclaim_id: nope\ngeneration: 1")
        self.assertEqual("REJECTED", reply["status"])
        self.assertEqual([], calls["release"])

    def test_reply_is_fenced_json(self):
        text = format_reply({"status": "NO_CLAIM"})
        self.assertIn("```json", text)
        self.assertIn('"status": "NO_CLAIM"', text)


class WorkflowTests(unittest.TestCase):
    def test_pickup_workflow_contract(self):
        text = (ROOT / ".github/workflows/chat-pickup.yml").read_text(encoding="utf-8")
        self.assertIn("issue_comment:", text)
        self.assertIn('["OWNER","MEMBER","COLLABORATOR"]', text)
        self.assertIn("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1", text)
        self.assertIn("actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97", text)
        self.assertIn("python -m execution_coordinator.actions_pickup", text)
        # Untrusted comment text must never reach a shell.
        self.assertNotIn("${{ github.event.comment.body }}", text.split("if:")[-1].split("steps:")[1])
        self.assertIn("actions: write", text)
        self.assertNotIn("contents: write", text)


if __name__ == "__main__":
    unittest.main()
