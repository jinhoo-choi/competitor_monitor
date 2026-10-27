"""구독 정책 경계 회귀: 외부 호출·SMTP·운영 이력 쓰기 없음."""
import copy
import unittest
from contextlib import ExitStack, redirect_stdout
from io import StringIO
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

    def run_native_grades(self, scores, subscription=True):
        job, result = fixture()
        template_article = job['input']['items'][0]
        template_analysis = next(iter(result['analyses'].values()))
        companies = ['키움증권', '미래에셋증권', 'NH투자증권', '한화투자증권',
                     '삼성증권', '신한투자증권', '하나증권', 'KB증권']
        items, analyses = [], {}
        for index, score in enumerate(scores):
            company = companies[index]
            article = copy.deepcopy(template_article)
            article.update(title=company+' 화면개편', _company=company,
                           description=company+'은 주문 화면을 개선했다.',
                           _body=company+'은 주문 화면을 개선했다.',
                           link='https://example.invalid/grade/'+str(index))
            article['article_id'] = bridge.article_id(article)
            analysis = copy.deepcopy(template_analysis)
            analysis.update(company_name=company, event_key=company+'_화면개편_202610',
                            impact_score=score,
                            impact_level='상' if score >= 7 else '중' if score >= 5 else '하',
                            summary=article['_body'], evidence_quote=article['_body'])
            items.append(article)
            analyses[article['article_id']] = analysis
        job['input'].update(items=items, raw_articles=copy.deepcopy(items))
        job['input_hash'] = bridge.digest(job['input'])
        result.update(input_hash=job['input_hash'], analyses=analyses,
                      classifications=[dict(article_id=a['article_id'], relevant=True,
                                            reason='서로 다른 경쟁사의 화면 개선') for a in items])
        original = copy.deepcopy(result)
        provider = bridge.Provider(job, result, bot, 'seen') if subscription else None
        with ExitStack() as stack:
            stack.enter_context(redirect_stdout(StringIO()))
            for name, value in {
                'preflight':True,
                'load_seen':{'ids':set(), 'title_norms':[], 'desc_norms':[], 'events':set()},
                'save_seen':None, 'save_filter_log':None,
                'send_email':None, 'send_email_no_result':None, 'send_email_error':None,
            }.items():
                stack.enter_context(patch.object(bot, name, return_value=value))
            for name, value in {'TEST_MODE':False, 'DRY_RUN':False,
                                'INSIGHT_ENGINE':'subscription' if subscription else 'api'}.items():
                stack.enter_context(patch.object(bot, name, value))
            api = stack.enter_context(patch.object(bot, '_ai_create', side_effect=AssertionError('API forbidden')))
            smtp = stack.enter_context(patch.object(bot.smtplib, 'SMTP_SSL', side_effect=AssertionError('SMTP forbidden')))
            if not subscription:
                stack.enter_context(patch.object(bot, 'collect_articles', return_value=copy.deepcopy(items)))
                stack.enter_context(patch.object(bot, 'filter_relevant_articles', side_effect=lambda arts:arts))
                stack.enter_context(patch.object(bot, 'fetch_article_body', return_value='오프라인 원문'))
                stack.enter_context(patch.object(bot, 'analyze_article', side_effect=lambda art:
                    dict(art, analysis=copy.deepcopy(analyses[art['article_id']]))))
            else:
                stack.enter_context(patch.object(bot, 'fetch_article_body', side_effect=AssertionError('recollection forbidden')))
            native_render = bot.build_email_html
            render = stack.enter_context(patch.object(bot, 'build_email_html',
                **({'wraps':native_render} if subscription else {'return_value':'legacy HTML'})))
            bot.main(provider)
            rendered = copy.deepcopy(render.call_args.args[0])
            html = native_render(rendered, len(items), len(items)) if subscription else 'legacy HTML'
            api.assert_not_called()
            smtp.assert_not_called()
        self.assertEqual(result, original)
        self.assertEqual({a['article_id'] for a in rendered}, set(analyses))
        return rendered, html

    def test_subscription_five_medium_keep_scores_levels_and_native_cards(self):
        rendered, html = self.run_native_grades([6.9, 6.8, 6.7, 6.0, 5.3])
        for article in rendered:
            self.assertEqual(article['analysis']['impact_level'], '중')
            self.assertIn(article['title'], html)
        scores = {a['_company']:a['analysis']['impact_score'] for a in rendered}
        self.assertEqual(scores['한화투자증권'], 6.0)
        self.assertEqual(scores['삼성증권'], 5.3)

    def test_subscription_high_overflow_and_score_boundaries_stay_consistent(self):
        rendered, _ = self.run_native_grades([8.0, 7.5, 7.0, 6.9, 5.0, 4.9])
        self.assertEqual(sum(a['analysis']['impact_level']=='상' for a in rendered), 3)
        for article in rendered:
            score = article['analysis']['impact_score']
            self.assertEqual(article['analysis']['impact_level'],
                             '상' if score >= 7 else '중' if score >= 5 else '하')

    def test_legacy_api_grade_cap_is_unchanged(self):
        rendered, _ = self.run_native_grades([6.9, 6.8, 6.7, 6.0, 5.3], subscription=False)
        self.assertEqual(sum(a['analysis']['impact_level']=='중' for a in rendered), 3)
        self.assertEqual(sum(a['analysis']['impact_level']=='하' for a in rendered), 2)


if __name__ == '__main__':
    unittest.main()
