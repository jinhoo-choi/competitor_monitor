"""Synthetic event/URL/history regressions; no operational or paid calls."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import event_dedup as events
with patch.dict(os.environ, {'NAVER_CLIENT_ID':'offline','NAVER_CLIENT_SECRET':'offline',
                            'GMAIL_USER':'owner@example.invalid','GMAIL_APP_PASSWORD':'offline'}), patch('anthropic.Anthropic'):
    import monitor_v2 as bot

NOW = datetime(2026, 11, 6, 1, 0, tzinfo=timezone.utc)


def article(title, description='', company='신한투자증권', key=''):
    return {'title':title,'description':description,'_body':description,'_company':company,
            'link':'https://example.invalid/'+hashlib.md5(title.encode()).hexdigest()}, {
            'company_name':company,'event_key':key or company+'_개통이력점검_202611'}


def structured(subject='주문 도우미',stage='plan',value='내년 출시할 계획',key='상태',action='서비스',novelty='new',matched=''):
    source=f'키움증권 {subject} 서비스는 {value}이라고 밝혔다.'
    art,analysis=article('키움증권 '+subject,source,'키움증권','키움증권_주문도우미_202611')
    analysis['event']={'subject':subject,'action':action,'event_date':'','stage':stage,
        'facts':[{'key':key,'value':value,'evidence_quote':source}],'matched_event_id':matched,'novelty':novelty}
    return art,analysis


class EventRegression(unittest.TestCase):
    def assess(self,art,analysis,records=None,legacy=None,at=NOW):
        return events.assess_event(art,analysis,records or [],legacy or set(),at)

    def test_actual_three_story_variants_only_first_passes(self):
        descriptions=['신한투자증권은 휴대폰 개통이력 점검 서비스 시행 1주년을 맞았다. 분기 1800건과 2.3억원을 보호했다.',
            '신한투자증권의 휴대폰 개통 이력 점검 서비스가 1주년을 맞았다. 2027년 통화패턴 AI 도입을 계획한다.',
            '신한금융투자는 휴대전화 개통 이력 점검 도입 1주년 성과를 소개했다. 분기 1800건과 2.3억원이다.']
        titles=['고객도 모르는 휴대폰 개통까지 잡는다','휴대폰 개통이력으로 보이스피싱 막는다','휴대전화 개통 이력 점검 시행 1주년']
        keys=['개통이력사기예방','보이스피싱점검','개통이력점검']
        records,decisions=[],[]
        for i in range(3):
            a,x=article(titles[i],descriptions[i],'신한금융투자' if i==2 else '신한투자증권','신한투자증권_'+keys[i]+'_202611')
            decision=self.assess(a,x,records,at=NOW+timedelta(hours=i*8));decisions.append(decision)
            if not decision['duplicate']:
                records=events.merge_event_records(records,[decision['record']],NOW+timedelta(hours=i*8))
        self.assertEqual([d['duplicate'] for d in decisions],[False,True,True])
        self.assertEqual(len({d['event_id'] for d in decisions}),1)

    def test_legacy_phone_aliases_migrate_and_match(self):
        records=events.load_event_records({'events':{'2026-11-05':['신한투자증권_개통이력사기예방_202611','신한투자증권_보이스피싱점검_202611','신한투자증권::고객보호::202611']}},NOW)
        self.assertEqual(len(records),1)
        a,x=article('휴대전화 개통 이력 점검 시행 1주년');d=self.assess(a,x,records)
        self.assertTrue(d['duplicate']);self.assertTrue(d['prior_sent_at'])

    def test_same_company_other_security_service_passes(self):
        a,x=article('휴대폰 개통이력 점검 서비스');record=self.assess(a,x)['record']
        a,x=structured('안면 인증 잠금 서비스','launched','정식 출시했다')
        x['company_name']='신한투자증권';x['event_key']='신한투자증권_안면인증출시_202611'
        a['description']+=' 기존 휴대폰 개통이력 점검 서비스와 별개다.'
        self.assertFalse(self.assess(a,x,[record])['duplicate'])

    def test_real_ai_rollout_not_blocked_by_phone_history(self):
        a,x=article('휴대폰 개통이력 점검 서비스');record=self.assess(a,x)['record']
        a,x=article('신한투자증권 통화패턴 AI 분석 서비스 정식 출시','휴대폰 개통이력 점검 서비스에 이어 통화패턴 AI 분석을 도입했다.',key='신한투자증권_AI분석출시_202611')
        self.assertFalse(self.assess(a,x,[record])['duplicate'])

    def test_same_launch_rereport_is_not_keyword_bypass(self):
        a,x=structured(stage='launched',value='정식 출시했다');r=self.assess(a,x)['record']
        a['title']='다른 매체 정식 출시 보도';self.assertTrue(self.assess(a,x,[r])['duplicate'])

    def test_plan_to_actual_launch_allowed_then_repeat_blocked(self):
        a,x=structured();r=self.assess(a,x)['record']
        a,x=structured(stage='launched',value='정식 출시했다',novelty='update',matched=r['event_id'])
        events.validate_event(a,x,[r]);d=self.assess(a,x,[r])
        self.assertFalse(d['duplicate']);self.assertEqual(d['novelty'],'update');self.assertEqual(d['event_id'],r['event_id'])
        records=events.merge_event_records([r],[d['record']],NOW)
        self.assertTrue(self.assess(a,x,records)['duplicate'])

    def test_same_run_new_label_still_checks_launch_evidence(self):
        a,x=structured();r=self.assess(a,x)['record']
        a,x=structured(stage='launched',value='서비스 개시 예정')
        with self.assertRaises(ValueError):self.assess(a,x,[r])

    def test_same_partner_different_contract_subjects_pass(self):
        a,x=structured('트레이딩뷰 해외주식 차트 계약','contracted','본계약 체결했다',action='제휴');r=self.assess(a,x)['record']
        a,x=structured('트레이딩뷰 국내주식 데이터 계약','contracted','본계약 체결했다',action='제휴')
        self.assertFalse(self.assess(a,x,[r],{'키움증권::트레이딩뷰','키움증권::MTS::202611'})['duplicate'])

    def test_month_rollover_keeps_specific_legacy_identity(self):
        a,x=article('개통이력 점검',key='신한투자증권_개통이력사기예방_202610');r=self.assess(a,x)['record']
        x['event_key']='신한투자증권_개통이력점검_202611';self.assertTrue(self.assess(a,x,[r])['duplicate'])

    def test_company_domain_and_threat_never_block_other_event(self):
        a,x=structured();self.assertFalse(self.assess(a,x,[],{'키움증권::MTS::202611','키움증권::기능강화'})['duplicate'])

    def test_new_number_with_different_unit_is_a_new_fact(self):
        a,x=structured(value='고객 100명',key='고객수');r=self.assess(a,x)['record']
        a,x=structured(value='100만원',key='한도',novelty='update',matched=r['event_id'])
        events.validate_event(a,x,[r]);self.assertEqual(self.assess(a,x,[r])['novelty'],'update')

    def test_new_numeric_schedule_preserved(self):
        a,x=structured(value='2027년',key='출시일');r=self.assess(a,x)['record']
        a,x=structured(value='2028년',key='출시일',novelty='update',matched=r['event_id'])
        events.validate_event(a,x,[r]);self.assertFalse(self.assess(a,x,[r])['duplicate'])

    def test_merge_retains_previous_facts(self):
        a,x=structured(value='100만원',key='한도');r=self.assess(a,x)['record']
        a,x=structured(value='2027년',key='시행일',novelty='update',matched=r['event_id']);d=self.assess(a,x,[r])
        merged=events.merge_event_records([r],[d['record']],NOW)
        self.assertEqual({f['key'] for f in merged[0]['facts']},{'한도','시행일'})

    def history_100_200(self):
        a,x=structured(value='100만원',key='한도');first=self.assess(a,x)['record']
        a,x=structured(value='200만원',key='한도',novelty='update',matched=first['event_id'])
        second=self.assess(a,x,[first])['record']
        return first,events.merge_event_records([first],[second],NOW)

    def test_old_fact_does_not_resurface_after_new_fact_sent(self):
        first,records=self.history_100_200()
        a,x=structured(value='100만원',key='한도',novelty='update',matched=first['event_id'])
        with self.assertRaises(ValueError):events.validate_event(a,x,records)
        self.assertTrue(self.assess(a,x,records)['duplicate'])

    def test_explicit_reversal_to_old_fact_is_allowed(self):
        first,records=self.history_100_200()
        a,x=structured(value='100만원',key='한도',novelty='update',matched=first['event_id'])
        a['description']=a['_body']='키움증권 주문 도우미 한도를 다시 100만원으로 낮췄다고 밝혔다.'
        x['event']['facts'][0]['evidence_quote']=a['description']
        events.validate_event(a,x,records);self.assertFalse(self.assess(a,x,records)['duplicate'])

    def test_negated_reversal_or_numeric_change_is_not_update(self):
        first,records=self.history_100_200()
        for value in ['100만원','300만원']:
            a,x=structured(value=value,key='한도',novelty='update',matched=first['event_id'])
            a['description']=a['_body']=f'키움증권 주문 도우미 한도를 다시 {value}으로 변경하지 않았다.'
            x['event']['facts'][0]['evidence_quote']=a['description']
            with self.subTest(value=value),self.assertRaises(ValueError):events.validate_event(a,x,records)
            self.assertTrue(self.assess(a,x,records)['duplicate'])

    def test_numeric_literal_must_not_be_substring_of_other_value(self):
        for source_value in ['15%','-5%','−5%','1.5%']:
            a,x=structured(value=source_value,key='수익률');x['event']['facts'][0]['value']='5%'
            with self.subTest(value=source_value),self.assertRaises(ValueError):events.validate_event(a,x,[])

    def test_negated_or_planned_cancellation_is_not_update(self):
        a,x=structured(stage='launched',value='정식 출시했다');r=self.assess(a,x)['record']
        for value in ['출시를 취소하지 않았다','출시 취소를 검토한다','출시 취소할 계획이다']:
            a,x=structured(stage='cancelled',value=value,novelty='update',matched=r['event_id'])
            with self.subTest(value=value),self.assertRaises(ValueError):events.validate_event(a,x,[r])
        a,x=structured(stage='cancelled',value='출시 계획을 취소했다',novelty='update',matched=r['event_id'])
        events.validate_event(a,x,[r])

    def test_signed_numeric_change_is_preserved(self):
        a,x=structured(value='−5%',key='수익률');r=self.assess(a,x)['record']
        a,x=structured(value='5%',key='수익률',novelty='update',matched=r['event_id'])
        events.validate_event(a,x,[r]);self.assertFalse(self.assess(a,x,[r])['duplicate'])

    def test_all_unexpired_event_records_retained(self):
        records=[]
        for i in range(320):
            a,x=structured(subject=f'주문도우미{i}');records.append(self.assess(a,x)['record'])
        self.assertEqual(len(events.merge_event_records([],records,NOW)),320)

    def test_exact_seven_day_record_retention(self):
        a,x=structured();r=self.assess(a,x)['record']
        self.assertEqual(len(events.load_event_records({'event_records':[r]},NOW+timedelta(days=7))),1)
        self.assertEqual(events.load_event_records({'event_records':[r]},NOW+timedelta(days=7,seconds=1)),[])


class UrlAndHistoryRegression(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'seen.json';self.patch=patch.object(bot,'SEEN_FILE',str(self.path))
        self.patch.start();self.addCleanup(self.patch.stop)

    def test_same_url_changed_title_and_tracking_blocked(self):
        seen={'ids':{'https://example.invalid/a?idxno=1&utm_source=x'},'title_norms':[]}
        self.assertTrue(bot.is_duplicate({'link':'https://example.invalid/a?idxno=1#top','title':'changed'},seen))

    def test_article_query_identity_preserved(self):
        self.assertNotEqual(events.normalize_url('https://example.invalid/a?idxno=1'),events.normalize_url('https://example.invalid/a?idxno=2'))

    def test_old_hash_formats_remain_readable(self):
        for value in ['https://example.invalid/a','https://example.invalid/aold title']:
            seen={'ids':{hashlib.md5(value.encode()).hexdigest()},'title_norms':[]}
            self.assertTrue(bot.is_duplicate({'link':'https://example.invalid/a','title':'old title'},seen))

    def test_similar_titles_new_number_and_other_service_reach_analysis(self):
        seen={'ids':set(),'title_norms':[]}
        for i,title in enumerate(['키움증권 주문 도우미 한도 100만원 확대','키움증권 주문 도우미 한도 200만원 확대','키움증권 [다른 서비스] 한도 200만원 확대']):
            self.assertFalse(bot.is_duplicate({'link':f'https://example.invalid/{i}','title':title},seen))

    def test_legacy_hour_boundary_not_shorter_than_24_hours(self):
        stamp=(datetime.now(bot.KST)-timedelta(hours=24)).strftime('%Y-%m-%d %H')
        self.path.write_text(json.dumps({stamp:{'ids':['https://example.invalid/1']}}))
        self.assertIn('https://example.invalid/1',bot.load_seen()['ids'])

    def test_legacy_list_migrates_without_losing_urls(self):
        self.path.write_text(json.dumps(['https://example.invalid/1']))
        snap=json.loads(bot.build_seen_snapshot(sent_urls={'https://example.invalid/2?utm_source=x'}))
        ids=set().union(*(set(v.get('ids',[])) for k,v in snap.items() if isinstance(v,dict) and k!='events'))
        self.assertEqual(ids,{'https://example.invalid/1','https://example.invalid/2'})

    def test_record_saved_in_immutable_snapshot(self):
        self.path.write_text('{}');a,x=structured()
        r=events.assess_event(a,x,[],set(),datetime.now(bot.KST))['record']
        snap=json.loads(bot.build_seen_snapshot(new_event_records=[r]))
        self.assertEqual(snap['event_records'][0]['event_id'],r['event_id']);self.assertEqual(self.path.read_text(),'{}')


if __name__=='__main__':unittest.main()
