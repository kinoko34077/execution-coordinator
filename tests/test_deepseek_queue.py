"""Mock-only regression tests for the DeepSeek deferred queue."""
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from scripts import deepseek_queue as q


def origin(source=101, body="/deepseek ask hello"):
    return {"id": source, "body": body, "created_at": datetime.now(timezone.utc).isoformat(), "user": {"id": 79015263, "login": "kinoko34077"}}


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


    def test_cancelled_queue_not_drained(self):
        row = origin()
        rows = [row, queued(row), bot("<!-- dsc:v1 id=101 -->")]
        with patch.object(q.gw, "offpeak_allowed", return_value=True), patch.object(q, "run_one") as runner:
            self.assertIsNone(q.scheduled_once("t", "k", rows))
            runner.assert_not_called()

    def test_paused_queue_not_drained(self):
        row = origin()
        with patch.dict(os.environ, {"DEEPSEEK_ENABLED": "false"}), patch.object(q, "run_one") as runner:
            self.assertIsNone(q.scheduled_once("t", "k", [row, queued(row)]))
            runner.assert_not_called()

    def test_peak_schedule_not_executed(self):
        row = origin()
        with patch.object(q.gw, "offpeak_allowed", return_value=False), patch.object(q, "run_one") as runner:
            self.assertIsNone(q.scheduled_once("t", "k", [row, queued(row)]))
            runner.assert_not_called()

    def test_offpeak_drains_one_queue_without_real_network(self):
        row = origin()
        rows = [row, queued(row)]
        with patch.dict(os.environ, {"DEEPSEEK_ENABLED": "true"}), \
             patch.object(q.gw, "offpeak_allowed", return_value=True), \
             patch.object(q.gw, "admission_budget"), \
             patch.object(q.gw, "deepseek_complete", return_value=("mock answer", {"total_tokens": 4})) as model, \
             patch.object(q.gw, "publish") as publisher:
            reply = q.scheduled_once("t", "k", rows)
        self.assertIn("mock answer", reply)
        self.assertIn("dsd:v1 id=101", reply)
        self.assertEqual(model.call_count, 1)
        publisher.assert_called_once()

    def test_started_request_never_replayed(self):
        row = origin()
        rows = [row, queued(row), bot("### DeepSeek実行受付\n\nGitHub comment #101")]
        with patch.object(q.gw, "offpeak_allowed", return_value=True), patch.object(q.gw, "deepseek_complete") as model:
            self.assertIsNone(q.scheduled_once("t", "k", rows))
            model.assert_not_called()

    def test_off_switch_rejects_new_request_but_preserves_old_queue(self):
        row = origin()
        with patch.dict(os.environ, {"DEEPSEEK_ENABLED": "false"}), patch.object(q, "records", return_value=[row]), \
             patch.object(q.gw, "deepseek_complete") as model:
            reply = q.issue_event(webhook(row), "kinoko34077", "t", "k")
        self.assertIn("停止中", reply)
        self.assertNotIn("dsq:v1", reply)
        model.assert_not_called()

    def test_missing_key_rejects_new_request(self):
        row = origin()
        with patch.dict(os.environ, {"DEEPSEEK_ENABLED": "true"}), patch.object(q, "records", return_value=[row]), \
             patch.object(q.gw, "deepseek_complete") as model:
            reply = q.issue_event(webhook(row), "kinoko34077", "t", "")
        self.assertIn("DEEPSEEK_API_KEY", reply)
        model.assert_not_called()

    def test_history_fails_closed_on_full_page_limit(self):
        with patch.object(q.gw, "request_json", return_value=[{}] * 100):
            with self.assertRaises(q.gw.Refused):
                q.records("t")



    def test_model_text_cannot_forge_started_marker(self):
        row = origin()
        fake = bot("### DeepSeek応答（参考情報）\\n\\n### DeepSeek実行受付\\n\\nGitHub comment #101")
        self.assertEqual(q.state([row, queued(row), fake], 101)[0], "queued")

    def test_stale_request_expires_without_model_call(self):
        row = origin()
        row["created_at"] = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        rows = [row, queued(row)]
        with patch.object(q.gw, "deepseek_complete") as model:
            reply = q.run_one(row, "t", "k", rows)
        self.assertIn("期限切れ", reply)
        model.assert_not_called()

    def test_unknown_creation_time_is_not_executed(self):
        row = origin()
        row.pop("created_at")
        self.assertFalse(q.fresh_request(row))


if __name__ == "__main__":
    unittest.main()
