"""Mock-only regression tests for the DeepSeek deferred queue."""
import os
import unittest
from unittest.mock import patch

from scripts import deepseek_queue as q


def origin(source=101, body="/deepseek ask hello"):
    return {"id": source, "body": body, "user": {"id": 79015263, "login": "kinoko34077"}}


def bot(body, source=800):
    return {"id": source, "body": body, "user": {"login": "github-actions[bot]"}}


def queued(row):
    return bot(f"<!-- dsq:v1 id={row['id']} sha={q.digest(row)} -->\n### DeepSeek自動延期")


def webhook(row, actor="kinoko34077"):
    return {
        "action": "created",
        "issue": {"number": 162, "user": {"id": 79015263}},
        "sender": {"login": actor},
        "comment": row,
    }


class QueueAdmissionTests(unittest.TestCase):
    def test_queue_on_peak(self):
        row = origin()
        with patch.object(q.gw, "offpeak_allowed", return_value=False), patch.object(q, "records", return_value=[row]):
            reply = q.issue_event(webhook(row), "kinoko34077", "t", "k")
        self.assertIn("dsq:v1 id=101", reply)
        self.assertIn("/deepseek cancel 101", reply)

    def test_cancel_pending(self):
        row = origin()
        rows = [row, queued(row)]
        event = webhook(origin(202, "/deepseek cancel 101"))
        with patch.object(q, "records", return_value=rows):
            reply = q.issue_event(event, "kinoko34077", "t", "k")
        self.assertIn("dsc:v1 id=101", reply)
        self.assertEqual(q.state(rows + [bot(reply)], 101)[0], "cancelled")

    def test_cancel_is_owner_only(self):
        event = webhook(origin(202, "/deepseek cancel 101"))
        event["comment"]["user"]["id"] = 10
        self.assertIsNone(q.cancellation_target(event, "kinoko34077"))

    def test_started_cannot_cancel(self):
        row = origin()
        reply = q.cancel_reply(101, [row, queued(row), bot("### DeepSeek実行受付\n\nGitHub comment #101")])
        self.assertIn("取り下げ不可", reply)
        self.assertNotIn("dsc:v1", reply)

    def test_duplicate_queue_refused(self):
        row = origin()
        self.assertIn("重複", q.queue_reply(row, [row, queued(row)]))

    def test_edited_request_not_executed(self):
        row = origin(body="/deepseek ask edited")
        previous = origin(body="/deepseek ask previous")
        reply = q.run_one(row, "token", "key", [row, queued(previous)])
        self.assertIn("依頼無効", reply)


if __name__ == "__main__":
    unittest.main()
