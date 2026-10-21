"""메일·LLM을 mock으로 대체한 발송/상태 저장 회귀. 실발송·유료 호출 없음."""
import copy
import importlib
import os
import smtplib
import unittest
from contextlib import ExitStack, redirect_stdout
from io import StringIO
from unittest.mock import patch

with patch.dict(os.environ, {
    "NAVER_CLIENT_ID": "offline", "NAVER_CLIENT_SECRET": "offline",
    "ANTHROPIC_API_KEY": "offline", "GMAIL_USER": "owner@example.invalid",
    "GMAIL_APP_PASSWORD": "offline", "TEST_MODE": "0", "DRY_RUN": "0",
}):
    bot = importlib.import_module("monitor_v2")


class DeliveryRegression(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(redirect_stdout(StringIO()))
        for name, value in {
            "GMAIL_USER": "owner@example.invalid",
            "NO_RESULT_RECEIVER": "admin@example.invalid",
            "TEST_MODE": False, "DRY_RUN": False,
        }.items():
            self.stack.enter_context(patch.object(bot, name, value))

    def sender(self, refused=None, error=None):
        def send(subject, html, addr):
            if error:
                raise error
            return {addr: (550, "rejected")} if addr in (refused or set()) else {}
        return self.stack.enter_context(patch.object(bot, "_send_one", side_effect=send))

    def main_fixture(self, kind="low"):
        article = {
            "title": "키움증권 신규 주문 화면 개편", "_company": "키움증권",
            "link": "https://example.invalid/article", "description": "화면 개편",
            "analysis": {"company_name": "키움증권", "impact_level": "하",
                         "impact_score": 3, "impact_domain": "MTS",
                         "event_key": "키움증권::offline-fixture"},
        }
        fixtures = {
            "preflight": True, "load_seen": {},
            "collect_articles": [] if kind == "empty" else [article],
            "hard_filter": ([article], []),
            "filter_relevant_articles": [] if kind == "irrelevant" else [article],
            "fetch_article_body": "실제 본문 대신 오프라인 fixture",
            "analyze_article": copy.deepcopy(article),
            "build_empty_html": "empty", "build_email_html": "result",
            "save_filter_log": None, "save_seen": None,
            "_usage_summary": {"total_cost_usd": 0, "calls": 0, "by_route": {}},
        }
        mocks = {name: self.stack.enter_context(patch.object(bot, name, return_value=value))
                 for name, value in fixtures.items()}
        return mocks

    def test_all_admin_routes_do_not_save_after_rejection(self):
        for kind in ("empty", "irrelevant", "low"):
            with self.subTest(kind=kind):
                mocks = self.main_fixture(kind)
                self.sender({"admin@example.invalid", "owner@example.invalid"})
                with self.assertRaisesRegex(RuntimeError, "SMTP 전원"):
                    bot.main()
                mocks["save_seen"].assert_not_called()

    def test_owner_copy_does_not_mask_admin_rejection(self):
        mocks = self.main_fixture()
        self.sender({"admin@example.invalid"})
        with self.assertRaisesRegex(RuntimeError, "담당자 메일"):
            bot.main()
        mocks["save_seen"].assert_not_called()

    def test_authentication_failure_propagates_before_state_save(self):
        mocks = self.main_fixture()
        self.sender(error=smtplib.SMTPAuthenticationError(535, b"offline"))
        with self.assertRaises(smtplib.SMTPAuthenticationError):
            bot.main()
        mocks["save_seen"].assert_not_called()

    def test_successful_low_delivery_saves_seen_once(self):
        mocks = self.main_fixture()
        send = self.sender()
        bot.main()
        mocks["save_seen"].assert_called_once()
        self.assertEqual(mocks["save_seen"].call_args.kwargs["sent_urls"],
                         {"https://example.invalid/article"})
        self.assertEqual({call.args[2] for call in send.call_args_list},
                         {"admin@example.invalid", "owner@example.invalid"})

    def test_successful_admin_is_not_retried_for_owner_copy_failure(self):
        mocks = self.main_fixture()
        self.sender({"owner@example.invalid"})
        bot.main()
        mocks["save_seen"].assert_called_once()

    def test_default_admin_owner_rejection_does_not_save(self):
        self.stack.enter_context(patch.object(bot, "NO_RESULT_RECEIVER", ""))
        mocks = self.main_fixture()
        self.sender({"owner@example.invalid"})
        with self.assertRaises(RuntimeError):
            bot.main()
        mocks["save_seen"].assert_not_called()

    def test_withheld_analysis_not_marked_as_sent(self):
        mocks = self.main_fixture()
        mocks["analyze_article"].return_value["_body_failed"] = True
        send = self.sender()
        bot.main()
        mocks["save_seen"].assert_not_called()
        self.assertEqual(len(send.call_args_list), 1)
        alerts = [c for c in send.call_args_list if "검토 필요" in c.args[0]]
        self.assertEqual([c.args[2] for c in alerts], [bot.OPERATIONAL_OWNER])
        self.assertIn("원문 확인 미완료", alerts[0].args[1])
        self.assertNotIn("런타임 오류", alerts[0].args[1])
        self.assertEqual(mocks["save_filter_log"].call_args.args[4],
                         {mocks["analyze_article"].return_value["title"]: "원문 확인 미완료"})

    def test_mixed_run_sends_valid_findings_and_owner_alert(self):
        mocks = self.main_fixture()
        valid = copy.deepcopy(mocks["analyze_article"].return_value)
        valid["analysis"].update(impact_level="중", impact_score=6)
        withheld = copy.deepcopy(valid)
        withheld.update(title="다른 기사", link="https://example.invalid/withheld", _body_failed=True)
        mocks["collect_articles"].return_value = [valid, withheld]
        mocks["hard_filter"].return_value = ([valid, withheld], [])
        mocks["filter_relevant_articles"].return_value = [valid, withheld]
        mocks["analyze_article"].side_effect = copy.deepcopy([valid, withheld])
        send = self.sender()
        bot.main()
        mocks["save_seen"].assert_called_once()
        self.assertEqual(mocks["save_seen"].call_args.kwargs["sent_urls"], {valid["link"]})
        self.assertEqual([a["link"] for a in mocks["build_email_html"].call_args.args[0]], [valid["link"]])
        alerts = [c for c in send.call_args_list if "검토 필요" in c.args[0]]
        self.assertEqual([c.args[2] for c in alerts], [bot.OPERATIONAL_OWNER])
        self.assertTrue(any("[인사이트]" in c.args[0] for c in send.call_args_list))
        self.assertEqual(mocks["save_filter_log"].call_args.args[4], {"다른 기사": "원문 확인 미완료"})

    def test_group_delivery_retains_partial_failure_behavior(self):
        self.sender({"group@example.invalid"})
        refused = bot._smtp_send("subject", "html", ["group@example.invalid"])
        self.assertIn("group@example.invalid", refused)

    def test_industry_disruption_excluded_by_ai_goes_only_to_owner_review(self):
        mocks = self.main_fixture('irrelevant')
        article = mocks['collect_articles'].return_value[0]
        article.update(title='국내 증권사 주간거래 시세 장애',
                       _body='국내 주요 증권사의 미국 주식 실시간 시세 제공이 중단됐다.',
                       _body_failed=False)
        send = self.sender()
        from unittest.mock import MagicMock
        guard = MagicMock()
        self.stack.enter_context(patch.object(bot, 'DELIVERY_GUARD', guard))
        bot.main()
        mocks['save_seen'].assert_not_called()
        mocks['save_filter_log'].assert_called_once()
        self.assertEqual(len(send.call_args_list), 1)
        self.assertEqual(send.call_args.args[2], bot.OPERATIONAL_OWNER)
        self.assertIn('검토 필요', send.call_args.args[0])
        self.assertIn('업계 공통 거래 장애', send.call_args.args[1])
        self.assertNotIn('런타임 오류', send.call_args.args[1])
        state = {'receipts': {}}
        guard.change.call_args.args[0](state)
        self.assertEqual(state, {'receipts': {}, 'no_delivery_required': True, 'review_required': True})

    def test_test_mode_still_sends_only_owner(self):
        self.stack.enter_context(patch.object(bot, "TEST_MODE", True))
        send = self.sender()
        bot.send_email_no_result("subject", "html")
        self.assertEqual([call.args[2] for call in send.call_args_list],
                         ["owner@example.invalid"])




class OwnerAlertRegression(unittest.TestCase):
    def article(self):
        return {"title": "키움증권 서비스 오류 개선", "_company": "키움증권",
                "analysis": {"impact_level": "중", "impact_score": 6.0,
                             "summary": "서비스 장애 대응 기능 개선", "action_point": "투자 위험 유의사항 확인"}}

    def test_verified_findings_and_business_cautions_remain(self):
        article = self.article()
        with patch.object(bot, "INSIGHT_ENGINE", "subscription"):
            html = bot.build_email_html([article], 1, 1)
        self.assertIn("서비스 장애 대응 기능 개선", html)
        self.assertIn("투자 위험 유의사항 확인", html)

    def test_industry_review_does_not_infer_affected_company_or_accept_speculation(self):
        common = {'title': '국내 증권사 미국 주간거래 시세 먹통', '_company': '토스증권',
                  '_body': '국내 주요 증권사의 주간거래 시세 조회가 중단됐다. 다른 ATS 주문은 정상이다.'}
        self.assertTrue(bot._industry_disruption_reason(common))
        self.assertEqual(common['_company'], '토스증권')
        for change in ({'_body_failed': True}, {'_body_from_similar': True},
                       {'title': '토스증권, 시세 공급망 다변화로 정상 서비스'},
                       {'_body': '국내 주요 증권사는 미국 주식 시장 전망을 제시했다.'},
                       {'_body': '토스증권 시세 조회가 중단됐다.'}):
            with self.subTest(change=change):
                self.assertFalse(bot._industry_disruption_reason({**common, **change}))

    def test_industry_review_is_logged_separately_from_ai_exclusion(self):
        from unittest.mock import MagicMock
        article = {'title': '국내 증권사 시세 장애', '_industry_review_reason': '업계 공통 거래 장애'}
        guard = MagicMock()
        with patch.object(bot, 'DELIVERY_GUARD', guard):
            bot.save_filter_log([article], [], [], [])
        entry = guard.save_log.call_args.args[0]['entries'][0]
        self.assertEqual(entry['decision'], 'AI_EXCLUDED')
        self.assertEqual(entry['review_scope'], bot.INDUSTRY_TAG)
        self.assertEqual(entry['review_reason'], '업계 공통 거래 장애')

    def test_incomplete_cards_withheld_without_mutation(self):
        for change in ({"_body_failed": True}, {"_body_from_similar": True}, {"analysis": None}):
            article = self.article(); article.update(change)
            before = copy.deepcopy(article)
            with patch.object(bot, "INSIGHT_ENGINE", "subscription"):
                html = bot.build_email_html([article], 1, 1)
            self.assertNotIn(article["title"], html)
            self.assertEqual(article, before)
            self.assertEqual(bot._card(article), "")
            self.assertEqual(bot._card_low(article), "")
        article = self.article()
        article["analysis"]["summary"] += " [본문 미수집]"
        self.assertEqual(bot._executive_articles([article]), [])

    def test_error_only_owner_and_guard_restored(self):
        guard = object()
        with patch.object(bot, "GMAIL_USER", "owner@example.invalid"), \
             patch.object(bot, "NO_RESULT_RECEIVER", "executives@example.invalid"), \
             patch.object(bot, "DELIVERY_GUARD", guard), \
             patch.object(bot, "_send_one") as send:
            def check(*args):
                self.assertIsNone(bot.DELIVERY_GUARD)
                self.assertEqual(args[2], "myboy3743@gmail.com")
                return {}
            send.side_effect = check
            self.assertTrue(bot.send_email_error("failure", "details"))
            self.assertIs(bot.DELIVERY_GUARD, guard)

    def test_invalid_owner_fails_closed(self):
        for owner in ("", "a@example.invalid,b@example.invalid", "bad\nheader@example.invalid"):
            with patch.object(bot, "OPERATIONAL_OWNER", owner), patch.object(bot, "_send_one") as send:
                self.assertFalse(bot.send_email_error("failure", "details"))
                send.assert_not_called()

    def test_notice_and_error_labels_keep_owner_and_receipts_isolated(self):
        guard = object()
        for operational in (False, True):
            with self.subTest(operational=operational), patch.object(bot, "DELIVERY_GUARD", guard), \
                 patch.object(bot, "TEST_PREFIX", "[TEST] "), patch.object(bot, "_send_one", return_value={}) as send:
                self.assertTrue(bot.send_email_error("검증 <항목>", "본문 & 확인", operational=operational))
                subject, html, receiver = send.call_args.args
                self.assertEqual(receiver, bot.OPERATIONAL_OWNER)
                self.assertTrue(subject.startswith("[TEST] "))
                self.assertIn("검토 필요" if operational else "오류", subject)
                self.assertIn("확인 항목" if operational else "스택 트레이스", html)
                self.assertEqual("런타임 오류" in html, not operational)
                self.assertIn("검증 &lt;항목&gt;", html)
                self.assertIs(bot.DELIVERY_GUARD, guard)

    def test_private_log_distinguishes_withheld_from_duplicate(self):
        valid = {"title": "정상 기사", "analysis": {"impact_level": "중"}}
        withheld = {"title": "보류 기사"}
        duplicate = {"title": "중복 기사"}
        from unittest.mock import MagicMock
        guard = MagicMock()
        with patch.object(bot, "DELIVERY_GUARD", guard):
            bot.save_filter_log([valid, withheld, duplicate], [], [valid, withheld, duplicate],
                                [valid], {"보류 기사": "원문 확인 미완료"})
        entries = {e["title"]: e for e in guard.save_log.call_args.args[0]["entries"]}
        self.assertEqual(entries["정상 기사"]["decision"], "SENT")
        self.assertEqual(entries["보류 기사"]["decision"], "WITHHELD")
        self.assertEqual(entries["보류 기사"]["reason"], "원문 확인 미완료")
        self.assertEqual(entries["중복 기사"]["decision"], "DEDUP_EXCLUDED")

    def test_run_alerts_are_separate_and_dry_run_silent(self):
        def run(provider): bot._OPERATIONAL_NOTES.append("원문 확인 미완료")
        with patch.object(bot, "_run_main", side_effect=run), \
             patch.object(bot, "send_email_error", return_value=True) as alert, \
             patch.object(bot, "DRY_RUN", False):
            bot.main()
            alert.assert_called_once()
            self.assertEqual(alert.call_args.kwargs, {"operational": True})
            with patch.object(bot, "DRY_RUN", True): bot.main()
            alert.assert_called_once()

if __name__ == "__main__":
    unittest.main()
