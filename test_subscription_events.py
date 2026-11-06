"""Offline recent-event subscription contract; no network, SMTP or model calls."""
import copy
import os
import unittest
from contextlib import ExitStack, redirect_stdout
from io import StringIO
from unittest.mock import MagicMock, patch

import subscription_bridge as bridge
with patch('anthropic.Anthropic'):
    from test_subscription_bridge import fixture, MemoryQueue


class EventContract(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('urllib.request.urlopen', side_effect=AssertionError('HTTP forbidden')))
        self.stack.enter_context(patch('smtplib.SMTP_SSL', side_effect=AssertionError('SMTP forbidden')))
        self.stack.enter_context(redirect_stdout(StringIO()))
        self.job, self.result = fixture()
        self.analysis = next(iter(self.result['analyses'].values()))
        self.article = self.job['input']['items'][0]

    def contract(self, records=None):
        self.job['input'].update(event_contract_version=1, recent_events=records or [])
        self.result['recent_events_reviewed'] = bridge.digest(self.job['input']['recent_events'])
        self.rehash()
        self.analysis['event'] = {
            'subject': 'MTS 해외주식 주문 기능', 'action': '개선', 'event_date': '',
            'stage': 'other', 'facts': [{'key': '주문화면', 'value': '통합 주문 화면',
                                       'evidence_quote': '고객은 앱에서 통합 주문 화면을 이용할 수 있다.'}],
            'matched_event_id': '', 'novelty': 'new'}

    def rehash(self):
        self.job['input_hash'] = bridge.digest(self.job['input'])
        self.result['input_hash'] = self.job['input_hash']

    def validate(self):
        return bridge.validate(self.job, self.result, 'seen')

    def test_legacy_pending_does_not_require_contract(self):
        with patch.object(bridge, 'validate_event', side_effect=AssertionError('legacy must not validate event')):
            self.validate()

    def test_new_event_contract(self):
        self.contract()
        self.validate()

    def test_empty_recent_list_still_requires_digest(self):
        self.contract()
        self.result.pop('recent_events_reviewed')
        with self.assertRaisesRegex(ValueError, '대조'):
            self.validate()

    def test_recent_review_digest_must_match_snapshot(self):
        self.contract()
        self.result['recent_events_reviewed'] = bridge.digest([{'event_id': 'unreviewed'}])
        with self.assertRaisesRegex(ValueError, '대조'):
            self.validate()

    def test_contract_version_and_list_types_fail_closed(self):
        for bad in (True, '1', 2, None):
            with self.subTest(version=bad):
                self.contract()
                self.job['input']['event_contract_version'] = bad
                self.rehash()
                with self.assertRaisesRegex(ValueError, '버전'):
                    self.validate()
        self.contract()
        self.job['input']['recent_events'] = {}
        self.rehash()
        with self.assertRaisesRegex(ValueError, '목록'):
            self.validate()

    def test_all_selected_require_structured_event_even_low_score(self):
        self.contract()
        self.analysis.update(impact_score=4, impact_level='하')
        self.analysis.pop('event')
        with self.assertRaises(ValueError):
            self.validate()

    def test_body_failed_uses_description_only_for_event(self):
        self.contract()
        self.article['_body_failed'] = True
        self.job['input']['raw_articles'] = [copy.deepcopy(self.article)]
        self.rehash()
        self.analysis.update(impact_score=4, impact_level='하',
                             summary='[본문 미수집] 키움증권은 해외주식 주문 기능을 개선했다.',
                             evidence_quote=self.article['description'])
        with self.assertRaises(ValueError):
            self.validate()
        self.analysis['event'].update(subject='해외주식 주문 기능', facts=[{
            'key': '기능', 'value': '주문 기능을 개선했다',
            'evidence_quote': self.article['description']}])
        self.validate()

    def test_validator_receives_exact_recent_snapshot(self):
        records = [{'event_id': 'prior-sent'}]
        self.contract(records)
        normalized = dict(self.analysis['event'], matched_event_id='prior-sent', novelty='same')
        with patch.object(bridge, 'validate_event', return_value=normalized) as validator:
            self.validate()
        validator.assert_called_once_with(self.article, self.analysis, records)
        self.assertEqual(self.analysis['event'], normalized)

    def test_invalid_event_cannot_be_bypassed_with_valid_review_digest(self):
        self.contract()
        self.analysis['event']['novelty'] = 'same'
        with self.assertRaises(ValueError):
            self.validate()

    def test_collection_includes_independent_recent_snapshot_all_modes(self):
        for mode, recovery in [('production', False), ('verification', False), ('production', True)]:
            with self.subTest(mode=mode, recovery=recovery), ExitStack() as stack:
                queue = MemoryQueue(self.job, self.result)
                queue.files['jobs/' + self.job['job_id'] + '.json']['status'] = 'completed'
                mock_bot = MagicMock()
                records = [{'event_id': 'sent-event', 'company': '키움증권'}]
                mock_bot.load_seen.return_value = {'event_records': records}
                mock_bot.collect_articles.return_value = []
                mock_bot.hard_filter.side_effect = lambda rows: (rows, [])
                stack.enter_context(patch.object(bridge, 'import_bot', return_value=mock_bot))
                stack.enter_context(patch.object(bridge, 'seen_hash', return_value='seen'))
                stack.enter_context(patch.object(bridge, 'notify_error'))
                stack.enter_context(patch.dict(os.environ, {'GITHUB_RUN_ID': 'fresh-events'}))
                bridge.collect({'job_ttl_minutes': 120, 'engine': 'subscription'}, queue,
                               mode=mode, recovery_only=recovery)
                new = queue.files['jobs/fresh-events.json']
                self.assertEqual(new['input']['event_contract_version'], 1)
                self.assertEqual(new['input']['recent_events'], records)
                self.assertEqual(new['input_hash'], bridge.digest(new['input']))
                records.append({'event_id': 'later'})
                self.assertEqual(len(new['input']['recent_events']), 1)
                if recovery:
                    mock_bot.collect_articles.assert_not_called()


if __name__ == '__main__':
    unittest.main()
