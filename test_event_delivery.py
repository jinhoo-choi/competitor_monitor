"""Real main/event selection/history integration; SMTP, HTTP and LLM are offline."""
import copy
import hashlib
import json
import socket
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from test_delivery_regress import bot
from event_dedup import assess_event


def article(subject, suffix, value='100만원', score=6.0, novelty='new', matched=''):
    quote = f'키움증권 {subject}의 이용 한도를 {value}으로 확대했다.'
    return {
        'article_id': suffix, '_company': '키움증권', '_tier': '1군',
        'title': f'키움증권 {subject} 이용 한도 {value} 확대 {suffix}',
        'description': quote, '_body': quote,
        'link': f'https://example.invalid/{suffix}?utm_source=news',
        'analysis': {
            'company_name': '키움증권', 'impact_domain': '고객보호',
            'threat_type': '기능강화', 'impact_score': score,
            'impact_level': '중' if score >= 5 else '하',
            'event_key': f'키움증권_{subject}_202611',
            'event': {'subject': subject, 'action': '한도확대', 'stage': 'other',
                      'event_date': '', 'matched_event_id': matched, 'novelty': novelty,
                      'facts': [{'key': '이용한도', 'value': value, 'evidence_quote': quote}]},
        },
    }


class EventDeliveryIntegration(unittest.TestCase):
    def run_main(self, articles, history=None, fail_send=False):
        """Real main, history I/O and filter logging; mock every external edge."""
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            path = Path(tmp) / 'seen.json'
            original = json.dumps({'event_records': history or [], 'events': {}}, ensure_ascii=False)
            path.write_text(original)
            rendered, plans, logs = [], [], []

            class Guard:
                history_plan = None
                accepted = False
                current = [{'input': {'seen_hash': hashlib.sha256(original.encode()).hexdigest()}}]

                def require_complete(inner):
                    if not inner.accepted:
                        raise RuntimeError('offline receipts incomplete')

                def save_log(inner, log):
                    logs.append(copy.deepcopy(log))

            guard = Guard()

            def render(items, *args):
                rendered.extend(copy.deepcopy(items))
                return 'offline rendered mail'

            def send(*args):
                self.assertIsInstance(guard.history_plan, str)
                self.assertEqual(path.read_text(), original)
                plans.append(json.loads(guard.history_plan))
                if fail_send:
                    raise RuntimeError('offline SMTP rejected')
                guard.accepted = True

            stack.enter_context(redirect_stdout(StringIO()))
            for name, value in {'SEEN_FILE': str(path), 'DELIVERY_GUARD': guard,
                                'TEST_MODE': False, 'DRY_RUN': False}.items():
                stack.enter_context(patch.object(bot, name, value))
            for name, value in {'preflight': True, 'collect_articles': copy.deepcopy(articles)}.items():
                stack.enter_context(patch.object(bot, name, return_value=value))
            stack.enter_context(patch.object(bot, 'hard_filter', side_effect=lambda arts: (arts, [])))
            stack.enter_context(patch.object(bot, 'filter_relevant_articles', side_effect=lambda arts: arts))
            stack.enter_context(patch.object(bot, 'fetch_article_body', return_value='오프라인 원문 ' * 30))
            stack.enter_context(patch.object(bot, 'analyze_article', side_effect=copy.deepcopy))
            stack.enter_context(patch.object(bot, 'build_email_html', side_effect=render))
            stack.enter_context(patch.object(bot, 'send_email', side_effect=send))
            stack.enter_context(patch.object(bot, 'send_email_no_result', side_effect=send))
            forbidden = []
            for owner, name in ((bot, '_ai_create'), (bot, 'send_email_error'),
                                (bot.smtplib, 'SMTP_SSL'), (bot.urllib.request, 'urlopen'),
                                (socket.socket, 'connect')):
                forbidden.append(stack.enter_context(patch.object(owner, name,
                    side_effect=AssertionError('external action forbidden: ' + name))))
            if fail_send:
                with self.assertRaisesRegex(RuntimeError, 'offline SMTP rejected'):
                    bot.main()
                self.assertEqual(path.read_text(), original)
            else:
                bot.main()
                self.assertEqual(path.read_text(), guard.history_plan)
            for mock in forbidden:
                mock.assert_not_called()
            return rendered, json.loads(path.read_text()), plans, logs

    def prior(self, subject='안심계좌', value='100만원'):
        old = article(subject, 'previous', value)
        now = bot.datetime.now(bot.KST) - timedelta(hours=1)
        return assess_event(old, old['analysis'], [], set(), now)['record']

    def test_same_company_different_services_survive_all_duplicate_gates(self):
        items = [article('안심계좌', 'one'), article('보안알림', 'two')]
        rendered, snapshot, _, logs = self.run_main(items)
        self.assertEqual({a['article_id'] for a in rendered}, {'one', 'two'})
        self.assertEqual({r['subject'] for r in snapshot['event_records']}, {'안심계좌', '보안알림'})
        self.assertEqual(len(snapshot['event_records']), 2)
        self.assertTrue(all(not r.get('legacy') for r in snapshot['event_records']))
        self.assertEqual([e['decision'] for e in logs[0]['entries']], ['SENT', 'SENT'])

    def test_distinct_structured_events_sharing_legacy_key_reload_without_shadows(self):
        items = [article('안심계좌', 'shared-one'), article('보안알림', 'shared-two')]
        for item in items:
            item['analysis']['event_key'] = '키움증권_고객보호강화_202611'
        _, snapshot, _, _ = self.run_main(items)
        self.assertEqual(len(snapshot['event_records']), 2)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'seen.json'
            path.write_text(json.dumps(snapshot, ensure_ascii=False))
            with patch.object(bot, 'SEEN_FILE', str(path)):
                reloaded = bot.load_seen()
                next_snapshot = json.loads(bot.build_seen_snapshot(reloaded))
                path.write_text(json.dumps(next_snapshot, ensure_ascii=False))
                reloaded_again = bot.load_seen()
        for records in (reloaded['event_records'], next_snapshot['event_records'],
                        reloaded_again['event_records']):
            self.assertEqual(len(records), 2)
            self.assertEqual({r['subject'] for r in records}, {'안심계좌', '보안알림'})
            self.assertTrue(all(not r.get('legacy') for r in records))

    def test_same_run_explicit_duplicate_update_is_excluded_without_abort(self):
        prior = self.prior()
        items = [article('안심계좌', suffix, '200만원', novelty='update', matched=prior['event_id'])
                 for suffix in ('update-one', 'update-repeat')]
        rendered, snapshot, _, logs = self.run_main(items, [prior])
        self.assertEqual([a['article_id'] for a in rendered], ['update-one'])
        self.assertEqual(len(snapshot['event_records']), 1)
        entries = {e['article_id']: e for e in logs[0]['entries']}
        self.assertEqual(entries['update-repeat']['decision'], 'DEDUP_EXCLUDED')
        self.assertEqual(entries['update-repeat']['novelty'], 'same')
        self.assertEqual(entries['update-repeat']['matched_event_id'], prior['event_id'])
        self.assertTrue(entries['update-repeat']['prior_sent_at'])
        self.assertEqual(entries['update-repeat']['dedup_reason'], 'same_batch_event_consolidated')

    def test_historical_rereport_explanation_keeps_original_sent_time(self):
        prior = self.prior()
        item = article('안심계좌', 'rereport', novelty='same', matched=prior['event_id'])
        rendered, snapshot, _, logs = self.run_main([item], [prior])
        self.assertEqual(rendered, [])
        self.assertEqual(snapshot['event_records'], [prior])
        entry = logs[0]['entries'][0]
        self.assertEqual(entry['decision'], 'DEDUP_EXCLUDED')
        self.assertEqual(entry['matched_event_id'], prior['event_id'])
        self.assertEqual(entry['prior_sent_at'], prior['last_sent_at'])
        self.assertEqual(entry['novelty'], 'same')
        self.assertEqual(entry['dedup_reason'], 'same_event_no_new_facts')

    def test_same_run_conflicting_values_are_withheld_before_smtp(self):
        prior = self.prior()
        items = [article('안심계좌', suffix, value, novelty='update', matched=prior['event_id'])
                 for suffix, value in [('first-change', '200만원'), ('second-change', '300만원')]]
        with self.assertRaisesRegex(ValueError, '동일 회차 사건 수치 충돌'):
            self.run_main(items, [prior])

    def test_low_cap_persists_only_final_sent_records_urls_and_titles(self):
        cap = bot.GRADE_LIMITS['하']
        items = [article(f'보안서비스{index}', f'low-{index}', score=4.9-index/10)
                 for index in range(cap + 2)]
        rendered, snapshot, plans, _ = self.run_main(items)
        expected = {f'low-{index}' for index in range(cap)}
        self.assertEqual({a['article_id'] for a in rendered}, expected)
        self.assertEqual(len(plans[0]['event_records']), cap)
        self.assertEqual(snapshot, plans[0])
        self.assertEqual({r['subject'] for r in snapshot['event_records']},
                         {f'보안서비스{index}' for index in range(cap)})
        buckets = [v for k, v in snapshot.items() if k not in {'events', 'event_records'}]
        self.assertEqual({u for b in buckets for u in b['ids']},
                         {f'https://example.invalid/{key}' for key in expected})
        self.assertEqual({t for b in buckets for t in b['title_norms']},
                         {bot._normalize_title(a['title']) for a in items[:cap]})

    def test_failed_delivery_never_commits_candidate_event_records(self):
        _, snapshot, plans, logs = self.run_main([article('안심계좌', 'not-sent')], fail_send=True)
        self.assertEqual(snapshot['event_records'], [])
        self.assertEqual(len(plans[0]['event_records']), 1)
        self.assertEqual(logs, [])


if __name__ == '__main__':
    unittest.main()
