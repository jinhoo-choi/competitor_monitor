"""구독 결과·전송 선기록·롤백 회귀. 네트워크와 모델 호출 없이 검수."""
import copy
import hashlib
import tempfile
from pathlib import Path
import os
import unittest
from datetime import timedelta
from unittest.mock import patch, MagicMock
from contextlib import ExitStack
from contextlib import redirect_stdout
from io import StringIO

from test_delivery_regress import bot
import subscription_bridge as bridge
import subscription_request as signal


def fixture():
    article = {'title': '키움증권, MTS 해외주식 주문 기능 강화',
               'description': '키움증권은 해외주식 주문 기능을 개선했다.',
               '_body': '키움증권은 MTS 해외주식 주문 기능을 개선했다. 고객은 앱에서 통합 주문 화면을 이용할 수 있다.',
               '_company': '키움증권', '_tier': '1군', '_source_type': 'targeted',
               '_body_failed': False, 'link': 'https://example.invalid/insight',
               'pubDate': 'Tue, 13 Oct 2026 12:00:00 +0900'}
    article['article_id'] = bridge.article_id(article)
    job = {'schema_version': 1, 'job_id': 'offline-verification', 'status': 'pending',
           'mode': 'verification', 'expires_at': (bridge.now()+timedelta(hours=2)).isoformat(),
           'input': {'policy_version': bridge.POLICY_VERSION, 'seen_hash': 'seen',
                     'raw_articles': [copy.deepcopy(article)], 'items': [article]}, 'receipts': {}}
    job['input_hash'] = bridge.digest(job['input'])
    result = {'schema_version': 1, 'job_id': job['job_id'], 'input_hash': job['input_hash'],
              'analysis_engine': 'chatgpt_subscription',
              'classifications': [{'article_id': article['article_id'], 'relevant': True, 'reason': '구체적 주문 기능 강화'}],
              'analyses': {article['article_id']: {
                  'company_name': '키움증권', 'event_key': '키움증권_해외주식주문_202610',
                  'impact_level': '중', 'impact_score': 6.0, 'impact_domain': '해외주식',
                  'threat_type': '기능강화', 'summary': '키움증권이 MTS 해외주식 주문 기능을 개선했다.',
                  'impact_tags': {'위협유형': '기능경쟁', '고객영향': '주문편의'},
                  'action_point': '해외주식 주문 화면의 편의성을 비교한다.',
                  'evidence_quote': '키움증권은 MTS 해외주식 주문 기능을 개선했다.', 'reviewed': True}}}
    return job, result


class MemoryQueue:
    def __init__(self, job, result):
        self.files = {'index.json': {'jobs': [job['job_id']]},
                      'jobs/'+job['job_id']+'.json': copy.deepcopy(job),
                      'results/'+job['job_id']+'.json': copy.deepcopy(result)}
        self.rev = {p: '1' for p in self.files}

    def preflight(self): return True

    def get(self, path): return copy.deepcopy(self.files.get(path)), self.rev.get(path)

    def put(self, path, value, sha=None):
        if sha != self.rev.get(path): raise RuntimeError('CAS mismatch')
        self.files[path] = copy.deepcopy(value)
        self.rev[path] = str(int(self.rev.get(path, '0'))+1)


class SubscriptionRegression(unittest.TestCase):
    def setUp(self):
        self.job, self.result = fixture()
        self.queue = MemoryQueue(self.job, self.result)
        self.config = {'engine': 'subscription'}
        self.engine = patch.object(bot, 'INSIGHT_ENGINE', 'subscription')
        self.engine.start()
        self.addCleanup(self.engine.stop)
        self.quiet = redirect_stdout(StringIO())
        self.quiet.__enter__()
        self.addCleanup(self.quiet.__exit__, None, None, None)

    def analysis(self): return next(iter(self.result['analyses'].values()))

    def test_body_extraction_ignores_page_controls_scripts_and_attributes(self):
        text = ('예시증권은 모바일 주문 화면에 실시간 잔고와 ETF 정보를 통합했다고 밝혔다. '
                '이용자는 같은 화면에서 차트와 보유 수량을 확인하고 주문할 수 있다.')
        pages = [
            '<div class="article-view-content">본문크기 본문 글씨 줄이기 본문 글씨 키우기</div>'
            '<article><p>' + text + '</p></article>',
            '<div class="article-body"><p>' + text.replace('ETF ', 'ETF&nbsp;') + '</p></div>',
            '<script>const template=\'<div id="article-body">' + '광고 메뉴 문구 ' * 20
            + '</div>\';</script><div id="article-body">' + text + '</div>',
        ]
        for page in pages:
            response = MagicMock()
            response.__enter__.return_value = response
            response.headers = {}
            response.read.return_value = page.encode()
            with self.subTest(page=page[:60]), patch.object(bot.urllib.request, 'urlopen', return_value=response):
                self.assertEqual(bot.fetch_article_body('https://news.example.invalid/story'), text)

    def test_valid_result_keeps_normal_news_without_paid_api(self):
        provider = bridge.Provider(self.job, self.result, bot, 'seen')
        with patch.object(bot, '_ai_create', side_effect=AssertionError('paid API forbidden')) as api:
            article = copy.deepcopy(self.job['input']['items'][0])
            got = bot.analyze_article(article, provider.analysis_for(article))
            self.assertEqual(got['analysis']['impact_level'], '중')
            self.assertEqual(got['_company'], '키움증권')
            api.assert_not_called()

    def test_real_main_with_provider_keeps_routing_and_records_receipts(self):
        self.queue.files['jobs/'+self.job['job_id']+'.json']['mode']='production'
        server=MagicMock(); server.__enter__.return_value=server
        server.sendmail.return_value={}
        with ExitStack() as stack:
            for target,name,value in [
                (bridge,'import_bot',bot),(bridge,'seen_hash','seen'),
                (bot,'preflight',True),(bot,'load_seen',{'ids':set(),'title_norms':[],'desc_norms':[],'events':set()}),
                (bot,'save_seen',None),(bot,'save_filter_log',None)]:
                stack.enter_context(patch.object(target,name,return_value=value))
            stack.enter_context(patch.object(bot.smtplib,'SMTP_SSL',return_value=server))
            stack.enter_context(patch.object(bot,'_ai_create',side_effect=AssertionError('paid API forbidden')))
            stack.enter_context(patch.object(bot,'fetch_article_body',side_effect=AssertionError('re-fetch forbidden')))
            stack.enter_context(patch.object(bot,'TEST_MODE',False))
            stack.enter_context(patch.object(bot,'DRY_RUN',False))
            bridge.consume(self.config,self.queue,self.job['job_id'])
        state=self.queue.get('jobs/'+self.job['job_id']+'.json')[0]
        self.assertEqual(state['status'],'delivery_complete')
        self.assertEqual(set(state['receipts'].values()),{'accepted'})
        self.assertGreater(server.sendmail.call_count,0)

    def test_wrong_entity_stays_excluded_and_missing_body_downgraded(self):
        article=copy.deepcopy(self.job['input']['items'][0])
        analysis=copy.deepcopy(self.analysis())
        article.update(title='두나무 디지털자산 플랫폼 출시',_body='두나무가 디지털자산 플랫폼을 출시했다.',description='두나무 신규 플랫폼')
        analysis['company_name']='두나무'
        self.assertIsNone(bot.analyze_article(article,analysis)['analysis'])
        article=copy.deepcopy(self.job['input']['items'][0]); article['_body_failed']=True
        downgraded=bot.analyze_article(article,copy.deepcopy(self.analysis()))['analysis']
        self.assertEqual(downgraded['impact_level'],'하')
        self.assertLess(downgraded['impact_score'],5)

    def test_corrupt_results_fail_before_smtp(self):
        changes = [lambda r: r.update(input_hash='tampered'),
                   lambda r: r.update(job_id='another'),
                   lambda r: r.update(analysis_engine='api'),
                   lambda r: r.update(classifications=[]),
                   lambda r: r['classifications'][0].update(relevant='true'),
                   lambda r: r.update(analyses={})]
        for change in changes:
            result = copy.deepcopy(self.result); change(result)
            with self.subTest(change=change), self.assertRaises(ValueError):
                bridge.validate(self.job, result, 'seen')

    def test_invalid_analysis_facts_score_and_review_rejected(self):
        changes = [dict(impact_score=True), dict(impact_score=float('nan')),
                   dict(impact_level='상'), dict(evidence_quote='허구의 사실입니다'),
                   dict(summary='고객이 999999명 증가했다.'), dict(reviewed=False),
                   dict(action_point=''), dict(threat_type='unknown')]
        for change in changes:
            result=copy.deepcopy(self.result); next(iter(result['analyses'].values())).update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                bridge.validate(self.job, result, 'seen')

    def test_stale_and_changed_seen_rejected(self):
        with self.assertRaises(ValueError): bridge.validate(self.job, self.result, 'changed')
        with self.assertRaises(ValueError): bridge.validate(self.job, self.result, 'seen', bridge.now()+timedelta(hours=3))

    def test_changed_raw_or_duplicate_input_rejected(self):
        for duplicate in [False, True]:
            job=copy.deepcopy(self.job)
            if duplicate: job['input']['items'] *= 2
            else: job['input']['raw_articles'][0]['title']='조작된 제목'
            job['input_hash']=bridge.digest(job['input'])
            result=copy.deepcopy(self.result); result['input_hash']=job['input_hash']
            with self.assertRaises(ValueError): bridge.validate(job,result,'seen')

    def test_subscription_api_guard_blocks_even_with_key(self):
        with patch.object(bot,'INSIGHT_ENGINE','subscription'), patch.object(bot,'client',MagicMock()) as client:
            with self.assertRaises(RuntimeError): bot._ai_create('model','purpose')
            client.messages.create.assert_not_called()

    def test_missing_queue_token_is_explicit(self):
        with patch.dict(os.environ, {'INSIGHT_QUEUE_TOKEN':''}):
            with self.assertRaisesRegex(RuntimeError,'INSIGHT_QUEUE_TOKEN'):
                bridge.Queue({'queue_repository':'jinhoo-choi/insight-bot-data','queue_branch':'main','queue_prefix':'subscription/insight'})

    def test_guard_receipts_empty_or_uncertain_block_history(self):
        guard=bridge.DeliveryGuard(self.queue,self.job['job_id'])
        with self.assertRaises(RuntimeError): guard.require_complete()
        guard.before('owner@example.invalid')
        with self.assertRaises(RuntimeError): guard.before('owner@example.invalid')
        guard.after('owner@example.invalid','uncertain')
        with self.assertRaises(RuntimeError): guard.require_complete()
        with self.assertRaises(RuntimeError): guard.before('owner@example.invalid')

    def test_accepted_receipt_prevents_resend(self):
        guard=bridge.DeliveryGuard(self.queue,self.job['job_id'])
        self.assertTrue(guard.before('owner@example.invalid'))
        guard.after('owner@example.invalid','accepted')
        self.assertFalse(guard.before('owner@example.invalid'))
        guard.require_complete()

    def test_smtp_connection_loss_sends_once_and_holds(self):
        guard=bridge.DeliveryGuard(self.queue,self.job['job_id'])
        server=MagicMock(); server.__enter__.return_value=server
        server.sendmail.side_effect=ConnectionResetError('after DATA unknown')
        with patch.object(bot,'DELIVERY_GUARD',guard), patch.object(bot.smtplib,'SMTP_SSL',return_value=server):
            with self.assertRaises(RuntimeError): bot._send_one('test','html','owner@example.invalid')
            server.sendmail.assert_called_once()
        with self.assertRaises(RuntimeError): guard.require_complete()

    def test_preflight_failure_is_not_success(self):
        with patch.object(bot,'preflight',return_value=False), self.assertRaisesRegex(RuntimeError,'SMTP'):
            bot.main(provider=MagicMock())

    def test_dry_run_renders_private_preview_no_smtp_or_seen_save(self):
        with patch.object(bridge,'import_bot',return_value=bot), patch.object(bridge,'seen_hash',return_value='seen'), patch.object(bot,'_smtp_send') as smtp, patch.object(bot,'save_seen') as seen, patch.object(bot,'_ai_create') as api:
            bridge.consume(self.config,self.queue,self.job['job_id'],dry_run=True)
            smtp.assert_not_called(); seen.assert_not_called(); api.assert_not_called()
        self.assertEqual(self.queue.get('jobs/'+self.job['job_id']+'.json')[0]['status'],'verified')
        self.assertIn('키움증권',self.queue.get('previews/'+self.job['job_id']+'.json')[0]['html'])

    def test_verification_job_cannot_broadcast(self):
        with patch.object(bridge,'import_bot',return_value=bot), patch.object(bridge,'seen_hash',return_value='seen'), self.assertRaisesRegex(RuntimeError,'검수 자료'):
            bridge.consume(self.config,self.queue,self.job['job_id'])

    def test_production_job_cannot_be_closed_as_preview(self):
        self.queue.files['jobs/'+self.job['job_id']+'.json']['mode']='production'
        with patch.object(bridge,'import_bot',return_value=bot), self.assertRaises(RuntimeError):
            bridge.consume(self.config,self.queue,self.job['job_id'],dry_run=True)

    def test_rollback_holds_pending_then_cancel_allows(self):
        with self.assertRaises(RuntimeError): bridge.rollback_check(self.queue)
        with self.assertRaises(RuntimeError): bridge.cancel(self.config,self.queue,self.job['job_id'])
        bridge.cancel({'engine':'paused'},self.queue,self.job['job_id'])
        bridge.rollback_check(self.queue)

    def test_rollback_cannot_cancel_uncertain_or_sending(self):
        for status in ['sending','uncertain','delivery_complete']:
            self.queue.files['jobs/'+self.job['job_id']+'.json']['status']=status
            with self.assertRaises(RuntimeError): bridge.cancel({'engine':'paused'},self.queue,self.job['job_id'])
            with self.assertRaises(RuntimeError): bridge.rollback_check(self.queue)

    def test_ack_needs_committed_seen_hash(self):
        job=self.queue.files['jobs/'+self.job['job_id']+'.json']
        job.update(status='delivery_complete',seen_after_hash='after')
        with patch.object(bridge,'seen_hash',return_value='before'), self.assertRaises(RuntimeError):
            bridge.acknowledge(self.queue,self.job['job_id'])
        with patch.object(bridge,'seen_hash',return_value='after'):
            bridge.acknowledge(self.queue,self.job['job_id'])
        self.assertEqual(self.queue.get('jobs/'+self.job['job_id']+'.json')[0]['status'],'completed')

    def test_delivery_state_recovery_preserves_newer_history(self):
        job=self.queue.files['jobs/'+self.job['job_id']+'.json']
        snapshot='{"after": true}'
        job.update(status='delivery_complete',seen_after_snapshot=snapshot,
                   seen_after_hash=hashlib.sha256(snapshot.encode()).hexdigest())
        with tempfile.TemporaryDirectory() as tmp, patch.object(bridge,'seen_hash',return_value='newer'):
            cwd=os.getcwd()
            try:
                os.chdir(tmp)
                Path('seen_articles.json').write_text('{"newer": true}')
                with self.assertRaises(RuntimeError): bridge.recover_state({'engine':'paused'},self.queue,self.job['job_id'])
                self.assertEqual(Path('seen_articles.json').read_text(),'{"newer": true}')
            finally: os.chdir(cwd)
        with tempfile.TemporaryDirectory() as tmp, patch.object(bridge,'seen_hash',return_value='seen'):
            cwd=os.getcwd()
            try:
                os.chdir(tmp)
                bridge.recover_state({'engine':'paused'},self.queue,self.job['job_id'])
                self.assertEqual(Path('seen_articles.json').read_text(),snapshot)
            finally: os.chdir(cwd)

    def test_queue_initialize_creates_private_only_for_matching_owner(self):
        with patch.dict(os.environ, {'INSIGHT_QUEUE_TOKEN':'offline'}):
            q=bridge.Queue({'queue_repository':'jinhoo-choi/insight-bot-data','queue_branch':'main','queue_prefix':'subscription/insight'})
        responses=[{'login':'jinhoo-choi'},None,{'private':True,'default_branch':'main'},{'object':{'sha':'head'}}]
        with patch.object(q,'api_request',side_effect=responses) as request:
            q.initialize()
        create=request.call_args_list[2]
        self.assertEqual(create.args[0],'/user/repos')
        self.assertIs(create.args[2]['private'],True)
        self.assertIs(create.args[2]['auto_init'],True)

    def test_queue_initialize_never_changes_public_repo_or_other_owner(self):
        with patch.dict(os.environ, {'INSIGHT_QUEUE_TOKEN':'offline'}):
            q=bridge.Queue({'queue_repository':'jinhoo-choi/insight-bot-data','queue_branch':'main','queue_prefix':'subscription/insight'})
        for responses in [[{'login':'different'}],[{'login':'jinhoo-choi'},{'private':False}]]:
            with patch.object(q,'api_request',side_effect=responses) as request, self.assertRaises(RuntimeError):
                q.initialize()
            self.assertTrue(all(len(c.args)<2 or c.args[1]=='GET' for c in request.call_args_list))

    def test_signal_never_evaluates_commands_and_blocks_branch_broadcast(self):
        for request in [dict(operation='consume',job_id='$(whoami)'),dict(operation='scheduled'),dict(operation='consume',job_id='id',dry_run='false')]:
            with self.assertRaises(ValueError): signal.arguments(request)
        with patch.dict(os.environ,{'GITHUB_REF':'refs/heads/codex/insight-subscription'}), self.assertRaises(ValueError):
            signal.arguments(dict(operation='consume',job_id='id'))
        self.assertEqual(signal.arguments(dict(operation='consume',job_id='id',self_test=True)),['consume','--job-id','id','--self-test'])


if __name__ == '__main__': unittest.main()
