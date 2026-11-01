"""No-network tests for DeepSeek Issue gateway and paid-request admission."""
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from scripts import deepseek_issue_gateway as gw


def event(command="/deepseek status", actor="kinoko34077"):
    return {
        "action": "created",
        "issue": {"number": 162, "user": {"id": 79015263}},
        "comment": {"id": 42, "body": command, "user": {"login": actor, "id": 79015263 if actor == "kinoko34077" else 0}},
        "sender": {"login": actor},
    }


class DeepSeekPolicyTests(unittest.TestCase):
    def clock(self, day, hour, minute=0):
        # 2026-11-02 is Monday in UTC.
        return datetime(2026, 11, 2 + day, hour, minute, tzinfo=timezone.utc)

    def test_peak_monday_utc(self):
        for hour, minute in [(0,58),(1,0),(3,59),(5,58),(6,0),(9,59)]:
            with self.subTest(hour=hour, minute=minute):
                self.assertFalse(gw.offpeak_allowed(self.clock(0,hour,minute)))

    def test_allowed_weekday_utc(self):
        for hour, minute in [(0,57),(4,0),(5,57),(10,0),(23,59)]:
            with self.subTest(hour=hour,minute=minute):
                self.assertTrue(gw.offpeak_allowed(self.clock(0,hour,minute)))

    def test_offpeak_weekends(self):
        self.assertTrue(gw.offpeak_allowed(self.clock(5,2)))
        self.assertTrue(gw.offpeak_allowed(self.clock(6,7)))

    def test_utc_conversion(self):
        self.assertFalse(gw.offpeak_allowed(self.clock(0,1).astimezone(timezone(timedelta(hours=9)))))
        self.assertTrue(gw.offpeak_allowed(self.clock(0,10).astimezone(timezone(timedelta(hours=9)))))

    def test_enabled_flag_default_controls(self):
        self.assertTrue(gw.gate_enabled("true"))
        self.assertTrue(gw.gate_enabled(" TRUE "))
        for flag in ("false","off","0","","unexpected"):
            self.assertFalse(gw.gate_enabled(flag))

    def test_disabled_gate_never_reaches_model(self):
        with patch.dict(os.environ, {"DEEPSEEK_ENABLED":"false"}), patch.object(gw, "request_json") as remote:
            with self.assertRaises(gw.Refused):
                gw.deepseek_complete("test", "fake-key", review=False)
            remote.assert_not_called()

    def test_peak_gate_never_reaches_model(self):
        with patch.dict(os.environ, {"DEEPSEEK_ENABLED":"true"}), patch.object(gw,"offpeak_allowed",return_value=False), patch.object(gw, "request_json") as remote:
            with self.assertRaises(gw.Refused):
                gw.deepseek_complete("test", "fake-key", review=False)
            remote.assert_not_called()

    def test_missing_secret_never_reaches_model(self):
        with patch.dict(os.environ, {"DEEPSEEK_ENABLED":"true"}), patch.object(gw,"offpeak_allowed",return_value=True), patch.object(gw, "request_json") as remote:
            with self.assertRaises(gw.Refused):
                gw.deepseek_complete("test", "", review=True)
            remote.assert_not_called()

    def test_valid_request_is_bounded_and_tools_disabled(self):
        response={"choices":[{"finish_reason":"stop","message":{"content":"done @mention"}}],"usage":{"total_tokens":12,"prompt_tokens":10,"completion_tokens":2}}
        with patch.dict(os.environ, {"DEEPSEEK_ENABLED":"true"}), patch.object(gw,"offpeak_allowed",return_value=True), patch.object(gw,"request_json",return_value=response) as remote:
            answer, usage = gw.deepseek_complete("test", "fake-key", review=False)
            self.assertIn("@\u200bmention",answer)
            self.assertEqual(usage["total_tokens"],12)
            payload=remote.call_args.kwargs["data"]
            self.assertEqual(payload["model"],"deepseek-flash")
            self.assertEqual(payload["reasoning_effort"],"none")
            self.assertNotIn("tools",payload)
            self.assertLessEqual(payload["max_tokens"],1100)

    def test_nonterminal_response_is_never_passed_off_as_review(self):
        with patch.dict(os.environ, {"DEEPSEEK_ENABLED":"true"}), patch.object(gw,"offpeak_allowed",return_value=True), patch.object(gw,"request_json",return_value={"choices":[{"finish_reason":"length","message":{"content":"partial"}}]}):
            with self.assertRaises(gw.Refused):
                gw.deepseek_complete("test", "fake-key", review=True)


class DeepSeekAdmissionTests(unittest.TestCase):
    def test_exact_comment_only(self):
        self.assertEqual(gw.admission(event(), "kinoko34077"), ("status", ""))
        self.assertEqual(gw.admission(event("/deepseek ask hello"),"kinoko34077"), ("ask", "hello"))
        self.assertEqual(gw.admission(event("/deepseek review kinoko34077/devflow#12"), "kinoko34077"), ("review","kinoko34077/devflow#12"))
        via_connector=event()
        via_connector["sender"]["login"]=gw.BOT
        self.assertEqual(gw.admission(via_connector,gw.BOT),("status",""))

    def test_reject_untrusted_and_invalid_scope(self):
        for text in ("bad", "/deepseek status extra","/deepseek ask","/deepseek review other/repo#1","/deepseek review kinoko34077/devflow#0","/deepseek ask abc\nmalicious"):
            with self.subTest(text=text):
                self.assertIsNone(gw.admission(event(text), "kinoko34077"))
        self.assertIsNone(gw.admission(event(actor="attacker"), "attacker"))
        self.assertIsNone(gw.admission(event(actor="kinoko34077"), "attacker"))

    def test_no_key_status_reports_no_api_call(self):
        with patch.dict(os.environ, {"DEEPSEEK_ENABLED":"false"}),patch.object(gw,"request_json") as remote,patch.object(gw,"offpeak_allowed",return_value=False):
            answer=gw.run(event(), "kinoko34077", "fake-github", "")
            self.assertIn("OFF", answer)
            self.assertIn("PEAK",answer)
            self.assertIn("未登録",answer)
            remote.assert_not_called()

    def test_no_model_call_without_enabled_gate(self):
        with patch.dict(os.environ, {"DEEPSEEK_ENABLED":"false"}),patch.object(gw,"request_json") as remote:
            with self.assertRaises(gw.Refused):
                gw.run(event("/deepseek ask hello"),"kinoko34077","fake-github","fake-deepseek")
            remote.assert_not_called()

    def test_review_public_only_and_head_pin(self):
        ref="kinoko34077/devflow#17"
        meta={"private":False}
        pr={"state":"open","head":{"sha":"a"*40}}
        files=[{"filename":"one.py","patch":"@@ -0,0 +1 @@\n+safe"}]
        with patch.object(gw,"request_json",side_effect=[meta,pr,files]):
            prompt,head,target=gw.review_prompt(ref,"fake-gh")
            self.assertEqual(head,"a"*40)
            self.assertEqual(target,ref)
            self.assertIn("one.py",prompt)
        with patch.object(gw,"request_json",return_value={"private":True}):
            with self.assertRaises(gw.Refused):
                gw.review_prompt(ref,"fake-gh")


    def test_daily_limit_rejects_existing_20_reservations(self):
        history=[{"user":{"login":"github-actions[bot]"},"body":"### DeepSeek実行受付\\n\\nGitHub comment #"+str(i)} for i in range(20)]
        with patch.dict(os.environ, {"DEEPSEEK_DAILY_LIMIT":"20"}), patch.object(gw,"request_json",return_value=history):
            with self.assertRaisesRegex(gw.Refused,"daily"):
                gw.admission_budget("fake-gh",42)

    def test_duplicate_request_fails_before_model_call(self):
        history=[{"user":{"login":"github-actions[bot]"},"body":"### DeepSeek実行受付\\n\\nGitHub comment #42"}]
        with patch.dict(os.environ, {"DEEPSEEK_DAILY_LIMIT":"20"}), patch.object(gw,"request_json",return_value=history):
            with self.assertRaisesRegex(gw.Refused,"already answered"):
                gw.admission_budget("fake-gh",42)

    def test_missing_comment_history_fails_closed(self):
        with patch.object(gw,"request_json",return_value=[{"user":{"login":"nobody"}}]*100):
            with self.assertRaises(gw.Refused):
                gw.admission_budget("fake-gh",42)

    def test_redirects_are_forbidden(self):
        self.assertIsNone(gw._NoCrossOriginRedirect().redirect_request(None,None,302,"redirect",{},"https://evil.example/path"))

    def test_issue_owner_id_enforced_even_if_bot_is_the_actor(self):
        ev=event()
        ev["sender"]["login"]=gw.BOT
        ev["comment"]["user"]["id"]=0
        self.assertIsNone(gw.admission(ev,gw.BOT))


if __name__ == "__main__":
    unittest.main()
