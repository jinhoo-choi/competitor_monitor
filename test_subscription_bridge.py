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
           'input': {'policy_version': bridge.POLICY_VERSION, 'policy_hash': bridge.policy_hash(), 'seen_hash': 'seen',
                     'raw_articles': [copy.deepcopy(article)], 'items': [article]}, 'receipts': {}}
    job['input_hash'] = bridge.digest(job['input'])
    result = {'schema_version': 1, 'job_id': job['job_id'], 'input_hash': job['input_hash'],
              'analysis_engine': 'chatgpt_subscription', 'policy_hash': bridge.policy_hash(),
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

    def policy_hash(self): return bridge.policy_hash()

    def get(self, path): return copy.deepcopy(self.files.get(path)), self.rev.get(path)

    def put(self, path, value, sha=None):
        if sha != self.rev.get(path): raise RuntimeError('CAS mismatch')
        self.files[path] = copy.deepcopy(value)
        self.rev[path] = str(int(self.rev.get(path, '0'))+1)
        return {'content': {'sha': self.rev[path]}}

    def history_source(self):
        raw=getattr(self,'remote_history',Path('seen_articles.json').read_bytes())
        blob=hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
        return raw,blob,'a'*40


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

    def test_same_version_policy_change_and_missing_hash_are_rejected(self):
        for value in [None, '0'*64]:
            result = copy.deepcopy(self.result)
            result['policy_hash'] = value
            with self.assertRaisesRegex(ValueError, '정책 원문 해시'):
                bridge.validate(self.job, result, 'seen')
        with patch.object(bridge, 'policy_hash', return_value='1'*64), self.assertRaisesRegex(ValueError, '정책 원문 해시'):
            bridge.validate(self.job, self.result, 'seen')

    def test_live_policy_change_blocks_consume_before_any_receipt(self):
        self.job['mode'] = 'production'
        self.queue.files['jobs/'+self.job['job_id']+'.json'] = copy.deepcopy(self.job)
        with patch.object(bridge, 'import_bot', return_value=bot), patch.object(bridge, 'seen_hash', return_value='seen'), \
                patch.object(self.queue, 'policy_hash', return_value='0'*64), \
                patch.object(bot.smtplib, 'SMTP_SSL') as smtp, self.assertRaisesRegex(RuntimeError, '정책 원문 변경'):
            bridge.consume(self.config, self.queue, self.job['job_id'])
        smtp.assert_not_called()
        self.assertEqual(self.queue.get('jobs/'+self.job['job_id']+'.json')[0]['receipts'], {})

    def test_policy_change_between_recipients_does_not_reserve_next_recipient(self):
        guard = bridge.DeliveryGuard(self.queue, self.job['job_id'])
        self.assertTrue(guard.before('first@example.invalid'))
        with patch.object(self.queue, 'policy_hash', return_value='0'*64), self.assertRaises(RuntimeError):
            guard.before('second@example.invalid')
        receipts = self.queue.get(guard.path)[0]['receipts']
        self.assertEqual(len(receipts), 1)

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

    def test_body_extraction_prefers_nested_article_over_menu_and_subheading(self):
        # 10/29 Asiae, EZ, MK/Naver, Hankyung structures, with synthetic text.
        text = ('키움증권은 일반모드 메인화면을 지난 8월 개편했다. '
                '간편모드는 지난해 10월 출시됐다. 고객은 자산 현황을 확인할 수 있다.')
        pages = [
            '<nav><p>' + '무관한 실시간 뉴스 ' * 80 + '</p></nav>'
            '<div class="article-body"><div><div id="txt_area" itemprop="articleBody">'
            '<figure>사진 설명</figure><p>' + text + '</p></div></div></div>',
            '<div class="article-body"><h2>' + '증권업 인수 관련 부제 ' * 10 + '</h2>'
            '<article id="article-view-content-div" itemprop="articleBody">'
            '<div><figure>사진</figure></div><p>' + text + '</p></article></div>',
            '<nav><p>' + '무관한 추천 기사 ' * 80 + '</p></nav>'
            '<article id="dic_area"><div><figure>사진</figure></div>' + text + '</article>',
            '<div class="article-body" id="articletxt" itemprop="articleBody">'
            '<div class="box-cont">구독 안내</div><figure>사진</figure>' + text + '</div>',
        ]
        for page in pages:
            response = MagicMock()
            response.__enter__.return_value = response
            response.headers = {}
            response.read.return_value = page.encode()
            with self.subTest(page=page[:70]), patch.object(bot.urllib.request, 'urlopen', return_value=response):
                body = bot.fetch_article_body('https://www.hankyung.com/article/offline')
                self.assertEqual(body, text)
                self.assertNotIn('무관한', body)

    def test_body_extraction_preserves_late_dates_and_company(self):
        lead = '일반모드 화면의 주식 정보와 자산 조회를 설명한다. ' * 40
        tail = '키움증권은 지난 8월 개편했다. 간편모드는 지난해 10월 출시됐다.'
        page = '<div id="article-body"><p>' + lead + '</p><div>중간 문단</div><p>' + tail + '</p></div>'
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers = {}
        response.read.return_value = page.encode()
        with patch.object(bot.urllib.request, 'urlopen', return_value=response):
            body = bot.fetch_article_body('https://news.example.invalid/story')
        self.assertIn(tail, body)
        self.assertGreater(body.index(tail), 800)

    def test_menu_or_subscription_notice_is_not_a_collected_body(self):
        for page in [
            '<nav><p>' + '메뉴와 다른 기사 ' * 80 + '</p></nav>',
            '<div class="article-body" id="articletxt" itemprop="articleBody"><div class="box-cont">' + '구독 안내 ' * 30 + '</div></div>',
        ]:
            response = MagicMock()
            response.__enter__.return_value = response
            response.headers = {}
            response.read.return_value = page.encode()
            with self.subTest(page=page[:70]), patch.object(bot.urllib.request, 'urlopen', return_value=response):
                self.assertEqual(bot.fetch_article_body('https://www.hankyung.com/article/offline'), '')

    def test_collection_keeps_late_evidence_and_marks_empty_body_failed(self):
        article = copy.deepcopy(self.job['input']['items'][0])
        body = '화면 구성과 주문 편의를 설명한다. ' * 65 + '키움증권은 지난 8월 개편했다.'
        for fetched in (body, ''):
            queue = MemoryQueue(self.job, self.result)
            queue.files['jobs/'+self.job['job_id']+'.json']['status'] = 'completed'
            with self.subTest(fetched=bool(fetched)), ExitStack() as stack:
                stack.enter_context(patch.dict(os.environ, {'GITHUB_RUN_ID': 'offline-body-collection'}))
                stack.enter_context(patch.object(bridge, 'import_bot', return_value=bot))
                stack.enter_context(patch.object(bridge, 'seen_hash', return_value='seen'))
                stack.enter_context(patch.object(bot, 'load_seen', return_value={}))
                stack.enter_context(patch.object(bot, 'collect_articles', return_value=[copy.deepcopy(article)]))
                stack.enter_context(patch.object(bot, 'hard_filter', side_effect=lambda raw: (raw, [])))
                stack.enter_context(patch.object(bot, 'fetch_article_body', return_value=fetched))
                api = stack.enter_context(patch.object(bot, '_ai_create', side_effect=AssertionError('paid API forbidden')))
                smtp = stack.enter_context(patch.object(bot.smtplib, 'SMTP_SSL', side_effect=AssertionError('SMTP forbidden')))
                bridge.collect({'engine': 'subscription', 'job_ttl_minutes': 120}, queue)
                stored = queue.files['jobs/offline-body-collection.json']['input']['items'][0]
                self.assertEqual(stored['_body'], fetched or article['description'])
                self.assertEqual(stored['_body_failed'], not bool(fetched))
                api.assert_not_called()
                smtp.assert_not_called()

    def test_attribution_failure_has_specific_reason_without_relaxing_gate(self):
        article = copy.deepcopy(self.job['input']['items'][0])
        article.update(title='초보용 화면 소개', _body='무관한 메뉴 텍스트', description='키움증권 서비스 소개')
        got = bot.analyze_article(article, self.analysis())
        self.assertIsNone(got['analysis'])
        self.assertEqual(bot._withhold_reason(got), '회사 귀속 확인 실패: 본문 미등장(키움증권)')

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
            observed_hash=stack.enter_context(patch.object(bridge,'seen_hash',return_value='seen'))
            def saved_exact_plan(*args,**kwargs):
                observed_hash.return_value=hashlib.sha256(bot.DELIVERY_GUARD.history_plan.encode()).hexdigest()
            stack.enter_context(patch.object(bot,'save_seen',side_effect=saved_exact_plan))
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

    def test_remote_history_uses_immutable_commit_and_checks_git_blob(self):
        import base64
        raw=b'{}\n';commit='b'*40
        blob=hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
        with patch.dict(os.environ,{'INSIGHT_QUEUE_TOKEN':'offline'}):
            queue=bridge.Queue({'queue_repository':'jinhoo-choi/insight-bot-data',
                                'queue_branch':'main','queue_prefix':'subscription/insight'})
        obj={'type':'file','content':base64.b64encode(raw).decode(),'sha':blob}
        with patch.object(queue,'api_request',side_effect=[{'object':{'sha':commit}},obj]) as request:
            self.assertEqual(queue.history_source(),(raw,blob,commit))
            self.assertTrue(request.call_args.args[0].endswith('?ref='+commit))
        with patch.object(queue,'api_request',side_effect=[{'object':{'sha':commit}},{**obj,'sha':'0'*40}]):
            with self.assertRaisesRegex(RuntimeError,'blob'):queue.history_source()

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
        after=hashlib.sha256(b'after').hexdigest()
        job.update(status='delivery_complete',seen_after_hash=after,receipts={'recipient':'accepted'})
        self.queue.remote_history=b'after'
        with patch.object(bridge,'seen_hash',return_value='before'), self.assertRaises(RuntimeError):
            bridge.acknowledge(self.queue,self.job['job_id'])
        with patch.object(bridge,'seen_hash',return_value=after):
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


class DeliveryIntegrityRegression(unittest.TestCase):
    """Real native main, temporary history, memory CAS and fake SMTP only."""
    def setUp(self):
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(redirect_stdout(StringIO()))
        root=Path(__file__).resolve().parent
        tmp=Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for name in ('subscription_policy.json','subscription_config.json'):
            (tmp/name).write_bytes((root/name).read_bytes())
        (tmp/'seen_articles.json').write_text('{}\n')
        previous=os.getcwd();os.chdir(tmp);self.addCleanup(os.chdir,previous)
        self.job,self.result=fixture();self.job['mode']='production'
        self.job['input']['seen_hash']=bridge.seen_hash()
        self.job['input_hash']=bridge.digest(self.job['input']);self.result['input_hash']=self.job['input_hash']
        self.queue=MemoryQueue(self.job,self.result);self.path='jobs/'+self.job['job_id']+'.json'
        self.server=MagicMock();self.server.__enter__.return_value=self.server;self.server.sendmail.return_value={}
        for name,value in {'INSIGHT_ENGINE':'subscription','TEST_MODE':False,'DRY_RUN':False,
                           'GMAIL_USER':'owner@example.invalid','RECIPIENTS_ALL':['group@example.invalid'],
                           # Keep existing two-target failure scenarios explicit.
                           'RECIPIENTS_CC':['owner@example.invalid'],'NO_RESULT_RECEIVER':'owner@example.invalid'}.items():
            self.stack.enter_context(patch.object(bot,name,value))
        self.stack.enter_context(patch.object(bot,'preflight',return_value=True))
        self.stack.enter_context(patch.object(bridge,'import_bot',return_value=bot))
        self.stack.enter_context(patch.object(bot,'_ai_create',side_effect=AssertionError('API forbidden')))
        self.stack.enter_context(patch.object(bot,'fetch_article_body',side_effect=AssertionError('recollection forbidden')))
        self.stack.enter_context(patch.object(bot.smtplib,'SMTP_SSL',return_value=self.server))

    def consume(self):return bridge.consume({'engine':'subscription'},self.queue,self.job['job_id'])

    def test_report_keeps_groups_and_executive_without_implicit_sender_then_ack_no_resend(self):
        from email import message_from_string
        from email.utils import parseaddr
        targets = ['general@example.invalid', 'pb@example.invalid', 'executive@example.invalid']
        with patch.object(bot, 'RECIPIENTS_ALL', targets[:2]), \
             patch.object(bot, 'RECIPIENTS_CC', [targets[2], targets[0]]):
            self.consume()
            calls = self.server.sendmail.call_args_list
            self.assertEqual([c.args[1] for c in calls], [[a] for a in targets])
            for call in calls:
                msg = message_from_string(call.args[2])
                self.assertEqual(parseaddr(msg['To'])[1], call.args[1][0])
                self.assertIsNone(msg['Cc'])
                self.assertIsNone(msg['Bcc'])
            state = self.queue.get(self.path)[0]
            self.assertEqual(len(state['receipts']), len(targets))
            self.assertNotIn(hashlib.sha256(bot.GMAIL_USER.encode()).hexdigest(), state['receipts'])
            self.queue.remote_history = Path('seen_articles.json').read_bytes()
            bridge.acknowledge(self.queue, self.job['job_id'])
            completed = self.queue.get(self.path)[0]
            self.consume()
            self.assertEqual(self.queue.get(self.path)[0], completed)
            self.assertEqual(self.server.sendmail.call_count, len(targets))
            self.assertEqual(completed['status'], 'completed')

    def test_report_preserves_explicit_sender_and_rejects_empty_config(self):
        with patch.object(bot, 'RECIPIENTS_ALL', ['general@example.invalid']), \
             patch.object(bot, 'RECIPIENTS_CC', [bot.GMAIL_USER, bot.GMAIL_USER]):
            bot.send_email('<p>report</p>', [], 0)
        self.assertEqual([c.args[1] for c in self.server.sendmail.call_args_list],
                         [['general@example.invalid'], [bot.GMAIL_USER]])
        self.server.sendmail.reset_mock()
        with patch.object(bot, 'RECIPIENTS_ALL', []), patch.object(bot, 'RECIPIENTS_CC', []):
            with self.assertRaisesRegex(RuntimeError, '수신자 미설정'):
                bot.send_email('<p>report</p>', [], 0)
        self.server.sendmail.assert_not_called()

    def test_report_group_rejection_has_no_sender_fallback_or_history_ack(self):
        before = Path('seen_articles.json').read_bytes()
        self.server.sendmail.return_value = {'group@example.invalid': (550, b'refused')}
        with patch.object(bot, 'RECIPIENTS_CC', []), self.assertRaises(RuntimeError):
            self.consume()
        self.assertEqual([c.args[1] for c in self.server.sendmail.call_args_list], [['group@example.invalid']])
        self.assertEqual(Path('seen_articles.json').read_bytes(), before)
        self.assertNotEqual(self.queue.get(self.path)[0]['status'], 'completed')


    def test_stale_read_after_reservation_does_not_lose_accepted_write(self):
        real_get=self.queue.get;real_put=self.queue.put;stale={}
        def put(path,value,sha=None):
            if path==self.path and 'sending' in value.get('receipts',{}).values():
                stale['row']=real_get(path)
            return real_put(path,value,sha)
        def get(path):
            row,sha=real_get(path)
            if path==self.path and 'sending' in row.get('receipts',{}).values() and stale:
                return copy.deepcopy(stale['row'])
            return row,sha
        with patch.object(self.queue,'get',side_effect=get),patch.object(self.queue,'put',side_effect=put):
            self.consume()
        state=real_get(self.path)[0]
        self.assertEqual(state['status'],'delivery_complete')
        self.assertEqual(list(state['receipts'].values()),['accepted','accepted'])
        self.assertEqual(self.server.sendmail.call_count,2)

    def test_accepted_data_survives_quit_failure_without_resend(self):
        guard=bridge.DeliveryGuard(self.queue,self.job['job_id'])
        self.server.__exit__.side_effect=OSError('QUIT failed after DATA')
        with patch.object(bot,'DELIVERY_GUARD',guard):
            self.assertEqual(bot._send_one('test','html','owner@example.invalid'),{})
            self.assertEqual(bot._send_one('test','html','owner@example.invalid'),{})
        self.assertEqual(self.server.sendmail.call_count,1)
        self.assertEqual(list(self.queue.get(self.path)[0]['receipts'].values()),['accepted'])

    def test_filter_and_runtime_log_failure_are_optional_after_accepted_data(self):
        for prefix in ('filter-logs/','logs/'):
            with self.subTest(prefix=prefix):
                self.queue=MemoryQueue(self.job,self.result);Path('seen_articles.json').write_text('{}\n')
                real_put=self.queue.put
                def put(path,value,sha=None):
                    if path.startswith(prefix):raise OSError('optional telemetry unavailable')
                    return real_put(path,value,sha)
                with patch.object(self.queue,'put',side_effect=put):self.consume()
                state=self.queue.get(self.path)[0]
                self.assertEqual(state['status'],'delivery_complete')
                self.assertEqual(hashlib.sha256(state['seen_after_snapshot'].encode()).hexdigest(),state['seen_after_hash'])
                self.assertEqual(bridge.seen_hash(),state['seen_after_hash'])
                self.assertTrue(state['diagnostic_errors'])

    def test_exact_plan_and_manifest_are_durable_before_first_data(self):
        def send(sender,targets,payload):
            state=self.queue.get(self.path)[0]
            self.assertEqual(hashlib.sha256(state['planned_seen_snapshot'].encode()).hexdigest(),state['planned_seen_hash'])
            self.assertEqual(len(state['expected_receipts']),2)
            return {}
        self.server.sendmail.side_effect=send
        self.consume()
        self.assertEqual(self.server.sendmail.call_count,2)

    def test_history_write_failure_keeps_exact_recoverable_accepted_state(self):
        with patch.object(bot.os,'replace',side_effect=OSError('history disk failed')):
            with self.assertRaisesRegex(OSError,'history disk failed'):self.consume()
        state=self.queue.get(self.path)[0]
        self.assertEqual(state['status'],'delivery_complete')
        self.assertEqual(list(state['receipts'].values()),['accepted','accepted'])
        self.assertEqual(hashlib.sha256(state['seen_after_snapshot'].encode()).hexdigest(),state['seen_after_hash'])
        calls=self.server.sendmail.call_count
        with patch.object(bot.smtplib,'SMTP_SSL',side_effect=AssertionError('recovery may not send')):
            bridge.recover_state({'engine':'paused'},self.queue,self.job['job_id'])
            self.queue.remote_history=Path('seen_articles.json').read_bytes()
            bridge.acknowledge(self.queue,self.job['job_id'])
        self.assertEqual(self.server.sendmail.call_count,calls)
        self.assertEqual(self.queue.get(self.path)[0]['status'],'completed')

    def test_remote_main_tamper_blocks_ack_even_when_local_history_matches(self):
        local=Path('seen_articles.json').read_bytes()
        self.queue.files[self.path].update(status='delivery_complete',receipts={'r':'accepted'},
            seen_after_hash=hashlib.sha256(local).hexdigest(),seen_after_snapshot=local.decode())
        self.queue.remote_history=b'{"newer-main":true}\n'
        with self.assertRaisesRegex(RuntimeError,'main'):
            bridge.acknowledge(self.queue,self.job['job_id'])
        self.assertEqual(self.queue.get(self.path)[0]['status'],'delivery_complete')
        self.server.sendmail.assert_not_called()

    def test_complete_requires_every_planned_recipient(self):
        self.queue.files[self.path].update(expected_receipts=['first','second'],receipts={'first':'accepted'})
        guard=bridge.DeliveryGuard(self.queue,self.job['job_id'])
        with self.assertRaises(RuntimeError):guard.require_complete()

    def test_native_owner_verification_preserves_history_and_recipient_isolation(self):
        self.queue.files[self.path]['mode']='verification'
        before=Path('seen_articles.json').read_bytes()
        with patch.object(bot,'TEST_MODE',True),patch.object(bot,'RECIPIENTS_ALL',[]), \
             patch.object(bot,'RECIPIENTS_CC',[]),patch.object(bot,'TEST_PREFIX','[TEST] '):
            bridge.consume({'engine':'subscription'},self.queue,self.job['job_id'],self_test=True)
        self.assertEqual(self.queue.get(self.path)[0]['status'],'verified')
        self.assertEqual(Path('seen_articles.json').read_bytes(),before)
        self.assertEqual([c.args[1] for c in self.server.sendmail.call_args_list],[['owner@example.invalid']])

    def test_expiry_before_next_recipient_blocks_new_data(self):
        guard=bridge.DeliveryGuard(self.queue,self.job['job_id'])
        self.queue.files[self.path]['expires_at']=(bridge.now()-timedelta(seconds=1)).isoformat()
        with patch.object(bot,'DELIVERY_GUARD',guard),self.assertRaisesRegex(RuntimeError,'만료'):
            bot._send_one('test','html','owner@example.invalid')
        self.server.sendmail.assert_not_called()

    def test_partial_unknown_stays_locked_with_plan_and_never_retries_data(self):
        self.server.sendmail.side_effect=[{},ConnectionResetError('DATA result unknown')]
        with self.assertRaises(RuntimeError):self.consume()
        state=self.queue.get(self.path)[0]
        self.assertEqual(state['status'],'uncertain')
        self.assertEqual(list(state['receipts'].values()),['accepted','uncertain'])
        self.assertEqual(hashlib.sha256(state['planned_seen_snapshot'].encode()).hexdigest(),state['planned_seen_hash'])
        with self.assertRaisesRegex(RuntimeError,'전송 상태'):self.consume()
        self.assertEqual(self.server.sendmail.call_count,2)
        self.assertEqual(Path('seen_articles.json').read_text(),'{}\n')

    def test_concurrent_target_change_is_not_overwritten_by_cached_transition(self):
        put=self.queue.put;changed=[]
        def concurrent(path,value,sha=None):
            response=put(path,value,sha)
            if path==self.path and list(value.get('receipts',{}).values())==['accepted'] and not changed:
                changed.append(True)
                self.queue.files[path].update(status='uncertain',other_writer_hold=True)
                self.queue.rev[path]=str(int(self.queue.rev[path])+1)
            return response
        with patch.object(self.queue,'put',side_effect=concurrent),self.assertRaises(RuntimeError):self.consume()
        state=self.queue.get(self.path)[0]
        self.assertEqual(state['status'],'uncertain');self.assertTrue(state['other_writer_hold'])
        self.assertEqual(list(state['receipts'].values()),['accepted'])
        self.assertEqual(self.server.sendmail.call_count,1)

    def test_missing_reservation_write_proof_never_starts_data(self):
        real_put=self.queue.put
        def put(path,value,sha=None):
            response=real_put(path,value,sha)
            return {} if path==self.path and value.get('status')=='sending' else response
        with patch.object(self.queue,'put',side_effect=put),self.assertRaisesRegex(RuntimeError,'쓰기 확인 SHA'):
            self.consume()
        self.server.sendmail.assert_not_called()
        self.assertEqual(Path('seen_articles.json').read_text(),'{}\n')

    def test_known_refusal_is_preserved_across_quit_failure_and_not_retried(self):
        guard=bridge.DeliveryGuard(self.queue,self.job['job_id'])
        refused={'owner@example.invalid':(550,b'refused')}
        self.server.sendmail.return_value=refused
        self.server.__exit__.side_effect=OSError('QUIT failed')
        with patch.object(bot,'DELIVERY_GUARD',guard):
            self.assertEqual(bot._send_one('test','html','owner@example.invalid'),refused)
            with self.assertRaisesRegex(RuntimeError,'거부 기록'):
                bot._send_one('test','html','owner@example.invalid')
        self.assertEqual(self.server.sendmail.call_count,1)
        self.assertEqual(list(self.queue.get(self.path)[0]['receipts'].values()),['rejected'])


class AnalysisLimitLoggingRegression(unittest.TestCase):
    """Real provider/main selection; all external I/O is mocked, not the cap/dedup."""
    def run_selection(self, count, withheld=False):
        job, result = fixture()
        template = job['input']['items'][0]
        analysis = next(iter(result['analyses'].values()))
        items = []
        for i in range(count + 2):
            a = copy.deepcopy(template)
            a.update(title=f'키움증권 주문 개선 기사 {i}', link=f'https://example.invalid/{i}',
                     _tier='1군' if i % 2 else '2군',
                     _source_type='targeted' if i % 3 else 'broad')
            a['article_id'] = bridge.article_id(a)
            items.append(a)
        job['input'].update(items=items, raw_articles=copy.deepcopy(items))
        job['input_hash'] = bridge.digest(job['input'])
        result.update(input_hash=job['input_hash'], classifications=[
            {'article_id': a['article_id'], 'relevant': i < count, 'reason': 'offline classification'}
            for i, a in enumerate(items)])
        priority = lambda a: (a['_tier'] != '1군', a['_source_type'] != 'targeted')
        selected = sorted(items[:count], key=priority)[:15]
        result['analyses'] = {a['article_id']: copy.deepcopy(analysis) for a in selected}
        provider = bridge.Provider(job, result, bot, 'seen')
        guard = MagicMock()
        with ExitStack() as stack:
            stack.enter_context(redirect_stdout(StringIO()))
            for name, value in {'DELIVERY_GUARD': guard, 'DRY_RUN': False, 'TEST_MODE': False}.items():
                stack.enter_context(patch.object(bot, name, value))
            mocks = {}
            for name, value in {'preflight': True, 'load_seen': {}, 'is_duplicate': False,
                                'save_seen': None, 'send_email': None, 'send_email_no_result': None,
                                'send_email_error': None, 'build_email_html': 'unchanged html'}.items():
                mocks[name] = stack.enter_context(patch.object(bot, name, return_value=value))
            stack.enter_context(patch.object(bot, 'hard_filter', side_effect=lambda raw: (raw, [])))
            # Analysis uses validated external data; no paid API or article fetch allowed.
            stack.enter_context(patch.object(bot, 'fetch_article_body', side_effect=AssertionError('network')))
            stack.enter_context(patch.object(bot, 'filter_relevant_articles', side_effect=AssertionError('AI')))
            analyze = stack.enter_context(patch.object(bot, 'analyze_article', wraps=bot.analyze_article))
            if withheld:
                stack.enter_context(patch.object(bot, '_withhold_reason', return_value='원문 확인 미완료'))
            bot.main(provider)
        actual_ids = [c.args[0]['article_id'] for c in analyze.call_args_list]
        expected = selected if count > 15 else items[:count]
        self.assertEqual(actual_ids, [a['article_id'] for a in expected])
        return guard.save_log.call_args.args[0], mocks, items, selected

    def test_real_27_relevant_15_selected_12_limited_with_dedup_after_cap(self):
        log, mocks, items, selected = self.run_selection(27)
        selected_titles = {a['title'] for a in selected}
        limited = {a['title'] for a in items[:27]} - selected_titles
        decisions = {e['title']: e['decision'] for e in log['entries']}
        self.assertEqual({t for t, d in decisions.items() if d == 'ANALYSIS_LIMIT_EXCLUDED'}, limited)
        self.assertEqual({k: log[k] for k in ('ai_relevant', 'analysis_selected', 'analysis_limit_excl', 'ai_excl')},
                         dict(ai_relevant=27, analysis_selected=15, analysis_limit_excl=12, ai_excl=2))
        # All selected articles describe one event. Existing dedup keeps one and does NOT backfill the cap.
        self.assertEqual(sum(d == 'SENT' for d in decisions.values()), 1)
        self.assertEqual(sum(d == 'DEDUP_EXCLUDED' for d in decisions.values()), 14)
        mocks['send_email'].assert_called_once()
        self.assertEqual(mocks['build_email_html'].call_args.args[1:], (29, 15))
        mocks['save_seen'].assert_called_once()

    def test_cap_boundaries_and_no_relevant(self):
        for count in (0, 14, 15, 16):
            with self.subTest(relevant=count):
                log, mocks, _, _ = self.run_selection(count)
                self.assertEqual(log['ai_relevant'], count)
                self.assertEqual(log['analysis_selected'], min(count, 15))
                self.assertEqual(log['analysis_limit_excl'], max(count - 15, 0))
                self.assertEqual(log['ai_excl'], 2)
                self.assertEqual(sum(e['decision'] == 'ANALYSIS_LIMIT_EXCLUDED' for e in log['entries']), max(count-15, 0))
                if not count:
                    mocks['send_email'].assert_not_called()
                    mocks['send_email_no_result'].assert_called_once()

    def test_withheld_coverage_keeps_limit_separate_and_does_not_save_seen(self):
        log, mocks, _, _ = self.run_selection(27, withheld=True)
        self.assertEqual(sum(e['decision'] == 'WITHHELD' for e in log['entries']), 15)
        self.assertEqual(sum(e['decision'] == 'ANALYSIS_LIMIT_EXCLUDED' for e in log['entries']), 12)
        self.assertEqual(log['ai_excl'], 2)
        mocks['send_email'].assert_not_called()
        mocks['send_email_no_result'].assert_not_called()
        mocks['save_seen'].assert_not_called()

    def test_private_and_local_log_match_with_same_title_different_article(self):
        import json
        selected = {'title': '동일 제목', 'link': 'https://example.invalid/selected'}
        limited = {'title': '동일 제목', 'link': 'https://example.invalid/limited'}
        irrelevant = {'title': '비관련'}
        hard = {'title': '하드 제외', '_excl_reason': '광고'}
        guard = MagicMock()
        args = ([selected, limited, irrelevant], [hard], [selected], [selected])
        with patch.object(bot, 'DELIVERY_GUARD', guard), redirect_stdout(StringIO()):
            bot.save_filter_log(*args, analysis_limited=[limited])
        private = guard.save_log.call_args.args[0]
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            try:
                os.chdir(tmp)
                with patch.object(bot, 'DELIVERY_GUARD', None), redirect_stdout(StringIO()):
                    bot.save_filter_log(*args, analysis_limited=[limited])
                local = json.loads(next(Path(tmp).glob('filter_log_*.json')).read_text())
            finally:
                os.chdir(cwd)
        for payload in (private, local):
            self.assertEqual([e['decision'] for e in payload['entries']],
                             ['SENT', 'ANALYSIS_LIMIT_EXCLUDED', 'AI_EXCLUDED', 'HARD_EXCLUDED'])
            self.assertEqual([payload[k] for k in ('ai_relevant', 'analysis_selected', 'analysis_limit_excl', 'ai_excl')], [2, 1, 1, 1])
        private.pop('time'); local.pop('time')
        self.assertEqual(private, local)

    def test_legacy_dry_run_log_does_not_claim_ai_selection_counts(self):
        guard = MagicMock()
        with patch.object(bot, 'DELIVERY_GUARD', guard), redirect_stdout(StringIO()):
            bot.save_filter_log([{'title': '하드필터만 완료'}], [], [{'title': '하드필터만 완료'}], [])
        self.assertNotIn('ai_relevant', guard.save_log.call_args.args[0])


if __name__ == '__main__': unittest.main()
