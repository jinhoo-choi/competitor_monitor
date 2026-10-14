"""구독 정책 경계 회귀: 외부 호출·SMTP·운영 이력 쓰기 없음."""
import copy
import unittest
from unittest.mock import patch

from test_subscription_bridge import fixture, bridge, bot


class SubscriptionPolicyRegression(unittest.TestCase):
    def setUp(self):
        self.job, self.result = fixture()
        self.article = self.job['input']['items'][0]
        self.analysis = next(iter(self.result['analyses'].values()))

    def missing_body(self):
        self.article.update(_body_failed=True, _body='',
                            description='키움증권은 해외주식 주문 기능을 개선했다.')
        self.analysis.update(impact_score=4.2, impact_level='하',
                             summary='[본문 미수집] 키움증권이 해외주식 주문 기능을 개선했다.',
                             evidence_quote=self.article['description'])
        self.rehash()

    def rehash(self):
        self.job['input']['raw_articles'][0] = copy.deepcopy(self.article)
        self.job['input_hash'] = bridge.digest(self.job['input'])
        self.result['input_hash'] = self.job['input_hash']

    def test_missing_body_low_grade_with_marker_and_description_is_valid(self):
        self.missing_body()
        bridge.validate(self.job, self.result, 'seen')

    def test_missing_body_high_score_is_rejected(self):
        self.missing_body()
        self.analysis.update(impact_score=9, impact_level='상')
        with self.assertRaises(ValueError):
            bridge.validate(self.job, self.result, 'seen')

    def test_missing_body_marker_is_required(self):
        self.missing_body()
        self.analysis['summary'] = '키움증권이 해외주식 주문 기능을 개선했다.'
        with self.assertRaises(ValueError):
            bridge.validate(self.job, self.result, 'seen')

    def test_failed_body_cannot_supply_quote_or_summary_numbers(self):
        self.missing_body()
        self.article['_body'] = '키움증권은 999999명에게 이벤트를 제공했다.'
        self.rehash()
        for changes in ({'evidence_quote': self.article['_body']},
                        {'summary': '[본문 미수집] 키움증권이 999999명에게 주문 기능을 제공했다.'}):
            original = copy.deepcopy(self.analysis)
            self.analysis.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                bridge.validate(self.job, self.result, 'seen')
            self.analysis.clear()
            self.analysis.update(original)

    def test_action_point_enforces_eighty_character_boundary(self):
        self.analysis['action_point'] = '가' * 80
        bridge.validate(self.job, self.result, 'seen')
        self.analysis['action_point'] += '나'
        with self.assertRaises(ValueError):
            bridge.validate(self.job, self.result, 'seen')

    def test_native_missing_body_high_grade_is_always_low_without_api(self):
        self.missing_body()
        self.analysis.update(impact_score=9, impact_level='상')
        for similar in (False, True):
            self.article['_body_from_similar'] = similar
            with self.subTest(similar=similar), patch.object(bot, '_ai_create', side_effect=AssertionError('API forbidden')):
                normalized = bot.analyze_article(copy.deepcopy(self.article), copy.deepcopy(self.analysis))['analysis']
                self.assertEqual(normalized['impact_level'], '하')
                self.assertEqual(normalized['impact_score'], 4.9)

    def test_verified_competitor_license_keeps_six_point_five_floor_without_api(self):
        self.article.update(title='키움증권 발행어음 사업 인가',
                            _body='키움증권은 발행어음 사업 인가를 받았다.', _body_failed=False)
        self.analysis.update(impact_score=3.5, impact_level='하', impact_domain='발행어음')
        with patch.object(bot, '_ai_create', side_effect=AssertionError('API forbidden')):
            normalized = bot.analyze_article(copy.deepcopy(self.article), copy.deepcopy(self.analysis))['analysis']
        self.assertEqual(normalized['impact_score'], 6.5)
        self.assertEqual(normalized['impact_level'], '중')


if __name__ == '__main__':
    unittest.main()
