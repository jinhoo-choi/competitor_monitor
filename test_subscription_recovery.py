"""Synthetic expired-input regression suite. No SMTP, AI, HTTP or private data."""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import MagicMock, patch
from contextlib import ExitStack, redirect_stdout
from io import StringIO

import subscription_bridge as bridge

POLICY = 'a' * 64


def article(number=1):
    value = {'title': 'Synthetic broker feature ' + str(number),
             'link': 'https://example.invalid/' + str(number),
             'pubDate': 'Mon, 01 Jan 2024 00:00:00 +0000',
             'description': 'Synthetic source only.', '_body': 'Preserved synthetic body ' + str(number),
             '_body_failed': False}
    value['article_id'] = bridge.article_id(value)
    return value


def source(jid='old', count=1):
    items = [article(n) for n in range(count)]
    job = {'schema_version': 1, 'job_id': jid, 'mode': 'production', 'status': 'pending',
           'expires_at': (bridge.now() - timedelta(days=40)).isoformat(), 'receipts': {},
           'input': {'policy_version': bridge.POLICY_VERSION, 'policy_hash': POLICY,
                     'seen_hash': 'historic-seen', 'raw_articles': copy.deepcopy(items), 'items': items}}
    job['input_hash'] = bridge.digest(job['input'])
    return job


class MemoryQueue:
    def __init__(self, job):
        self.files = {'jobs/' + job['job_id'] + '.json': copy.deepcopy(job),
                      'index.json': {'jobs': [job['job_id']]}}
        self.revs = {p: '1' for p in self.files}
        self.fail_batch = False

    def preflight(self): pass
    def policy_hash(self): return POLICY
    def get(self, path): return copy.deepcopy(self.files.get(path)), self.revs.get(path)
    def put(self, path, value, sha=None):
        if sha != self.revs.get(path): raise RuntimeError('CAS mismatch')
        self.files[path] = copy.deepcopy(value)
        self.revs[path] = str(int(self.revs.get(path, '0')) + 1)
        return {'content': {'sha': self.revs[path]}}
    def snapshot_jobs(self):
        return {'head': bridge.digest(self.files), 'tree': 'offline',
                'jobs': {p[5:-5]: copy.deepcopy(j) for p, j in self.files.items() if p.startswith('jobs/')},
                'index': copy.deepcopy(self.files['index.json'])}
    def publish_collection(self, snapshot, values):
        if self.fail_batch or snapshot['head'] != bridge.digest(self.files):
            raise RuntimeError('CAS mismatch')
        for path, value in values.items(): self.put(path, value, self.revs.get(path))
    def history_source(self):
        raw = Path('seen_articles.json').read_bytes()
        return raw, hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest(), 'b' * 40


class Recovery(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(redirect_stdout(StringIO()))
        self.stack.enter_context(patch.object(bridge, 'policy_hash', return_value=POLICY))
        self.stack.enter_context(patch.dict(os.environ, {'GITHUB_RUN_ID': 'fresh'}))
        self.notice = self.stack.enter_context(patch.object(bridge, 'notify_error'))
        self.network = self.stack.enter_context(patch('urllib.request.urlopen', side_effect=AssertionError('HTTP forbidden')))
        self.smtp = self.stack.enter_context(patch('smtplib.SMTP_SSL', side_effect=AssertionError('SMTP forbidden')))
        self.temp = self.stack.enter_context(tempfile.TemporaryDirectory())
        previous = os.getcwd()
        os.chdir(self.temp)
        self.addCleanup(os.chdir, previous)
        Path('seen_articles.json').write_text('[]')
        self.bot = MagicMock()
        self.bot.TEST_MODE = self.bot.DRY_RUN = False
        self.bot.load_seen.side_effect = lambda: {'ids': set()}
        self.bot.collect_articles.return_value = []
        self.bot.hard_filter.side_effect = lambda rows: (rows, [])
        def duplicate(a, seen):
            aid = bridge.article_id(a)
            old = aid in seen['ids']
            seen['ids'].add(aid)
            return old
        self.bot.is_duplicate.side_effect = duplicate
        self.bot.fetch_article_body.side_effect = AssertionError('old body must not refetch')
        self.stack.enter_context(patch.object(bridge, 'import_bot', return_value=self.bot))
        self.queue = MemoryQueue(source())
        self.config = {'engine': 'subscription', 'job_ttl_minutes': 120}

    def collect(self, recovery_only=True):
        bridge.collect(self.config, self.queue, recovery_only=recovery_only)
        return self.queue.files['jobs/fresh.json']

    def result(self, job):
        return {'schema_version': 1, 'job_id': job['job_id'], 'input_hash': job['input_hash'],
                'policy_hash': POLICY, 'analysis_engine': 'chatgpt_subscription',
                'recovery_history_reviewed': bridge.digest(job['input']['recovery_history']),
                'classifications': [{'article_id': a['article_id'], 'relevant': False,
                                     'reason': 'Synthetic exclusion reviewed'} for a in job['input']['items']],
                'analyses': {}}

    def complete(self, job, decision='AI_EXCLUDED'):
        result = self.result(job)
        self.queue.put('results/fresh.json', result)
        def run(provider):
            guard = self.bot.DELIVERY_GUARD
            guard.prepare(['test@example.invalid'], '[]')
            self.assertTrue(guard.before('test@example.invalid'))
            guard.after('test@example.invalid', 'accepted', '<synthetic>')
            guard.save_log({'entries': [{'article_id': a['article_id'], 'decision': decision}
                                        for a in job['input']['items']]})
        self.bot.main.side_effect = run
        bridge.consume(self.config, self.queue, 'fresh')
        bridge.acknowledge(self.queue, 'fresh')

    def test_aged_input_preserved_new_ttl_policy_seen_no_network(self):
        old = copy.deepcopy(self.queue.files['jobs/old.json'])
        new = self.collect()
        self.assertEqual(new['input']['items'], old['input']['items'])
        self.assertEqual(new['input']['seen_hash'], bridge.seen_hash())
        self.assertEqual(new['input']['policy_hash'], POLICY)
        self.assertGreater(new['expires_at'], bridge.now().isoformat())
        saved = self.queue.files['jobs/old.json']
        self.assertEqual(saved['expires_at'], old['expires_at'])
        self.assertEqual(saved['input'], old['input'])
        self.assertEqual(saved['input_hash'], old['input_hash'])
        self.bot.fetch_article_body.assert_not_called()
        self.bot.collect_articles.assert_not_called()
        self.smtp.assert_not_called()
        self.network.assert_not_called()

    def test_rolling_index_truncation_does_not_lose_source(self):
        self.queue.files['index.json'] = {'jobs': []}
        self.assertEqual(len(self.collect()['input']['items']), 1)

    def test_unrelated_live_pending_and_all_uncertain_receipts_block(self):
        for state in ('pending', 'sending', 'uncertain', 'delivery_complete'):
            live = source('live')
            live.update(status=state, expires_at=(bridge.now() + timedelta(hours=1)).isoformat())
            self.queue.files['jobs/live.json'] = live
            with self.assertRaisesRegex(RuntimeError, '미종결'): self.collect()
        del self.queue.files['jobs/live.json']
        for receipt in ('sending', 'accepted', 'uncertain', 'rejected'):
            self.queue.files['jobs/old.json']['receipts'] = {'recipient': receipt}
            with self.assertRaisesRegex(RuntimeError, '미종결'): self.collect()

    def test_delivery_plan_even_empty_blocks(self):
        self.queue.files['jobs/old.json']['expected_receipts'] = []
        with self.assertRaisesRegex(RuntimeError, '미종결'): self.collect()

    def test_corrupt_body_hash_blocks_without_expiring_source(self):
        self.queue.files['jobs/old.json']['input']['items'][0]['_body'] = 'tampered'
        with self.assertRaisesRegex(RuntimeError, '해시'): self.collect()
        self.assertEqual(self.queue.files['jobs/old.json']['status'], 'pending')

    def test_valid_hash_but_bad_identity_or_body_binding_blocks(self):
        job = self.queue.files['jobs/old.json']
        job['input']['items'][0]['article_id'] = 'forged'
        job['input_hash'] = bridge.digest(job['input'])
        with self.assertRaisesRegex(RuntimeError, '불일치'): self.collect()

    def test_seen_and_fresh_duplicates_not_repeated(self):
        self.bot.collect_articles.return_value = [article(0), article(0)]
        self.assertEqual(len(self.collect(recovery_only=False)['input']['items']), 1)

    def test_current_seen_exclusion_keeps_source_unresolved(self):
        self.bot.load_seen.side_effect = lambda: {'ids': {article(0)['article_id']}}
        job = self.collect()
        self.assertEqual(job['input']['items'], [])
        self.assertTrue(job['input']['recovery_sources'])

    def test_failed_batch_leaves_old_job_and_result_unchanged(self):
        self.queue.files['results/old.json'] = {'immutable': True}
        before = copy.deepcopy(self.queue.files)
        self.queue.fail_batch = True
        with self.assertRaisesRegex(RuntimeError, 'CAS'): self.collect()
        self.assertEqual(self.queue.files, before)

    def test_concurrent_writer_cas_prevents_second_pending_job(self):
        original = self.queue.publish_collection
        def race(snapshot, values):
            self.queue.files['jobs/racer.json'] = source('racer')
            return original(snapshot, values)
        self.queue.publish_collection = race
        with self.assertRaisesRegex(RuntimeError, 'CAS'): self.collect()
        self.assertNotIn('jobs/fresh.json', self.queue.files)
        self.assertEqual(self.queue.files['jobs/old.json']['status'], 'pending')

    def test_verification_never_carries_production_inputs(self):
        bridge.collect(self.config, self.queue, mode='verification', recovery_only=True)
        self.assertEqual(self.queue.files['jobs/fresh.json']['input']['items'], [])
        self.assertEqual(self.queue.files['jobs/old.json']['input'], source()['input'])

    def test_unreviewed_history_result_cannot_consume(self):
        job = self.collect()
        result = self.result(job)
        del result['recovery_history_reviewed']
        with self.assertRaisesRegex(RuntimeError, '과거'): bridge.validate_recovery_sources(self.queue, job, result)

    def test_changed_source_after_collection_cannot_consume(self):
        job = self.collect()
        self.queue.files['jobs/old.json']['input_hash'] = '0' * 64
        with self.assertRaisesRegex(RuntimeError, '해시'): bridge.validate_recovery_sources(self.queue, job, self.result(job))

    def test_acknowledged_recovery_never_recarried_after_seen_retention(self):
        job = self.collect()
        self.complete(job)
        with patch.dict(os.environ, {'GITHUB_RUN_ID': 'next'}):
            bridge.collect(self.config, self.queue, recovery_only=True)
        self.assertEqual(self.queue.files['jobs/next.json']['input']['items'], [])
        self.smtp.assert_not_called()
        self.network.assert_not_called()

    def test_withheld_and_analysis_limit_remain_unresolved_after_ack(self):
        for decision in ('WITHHELD', 'ANALYSIS_LIMIT_EXCLUDED'):
            self.queue = MemoryQueue(source())
            job = self.collect()
            self.complete(job, decision)
            values, sources, items = bridge.prepare_carryover(self.queue.snapshot_jobs(), 'production')
            self.assertEqual(len(items), 1)

    def test_unacknowledged_recovery_blocks_next_collection(self):
        job = self.collect()
        self.queue.files['jobs/fresh.json']['status'] = 'delivery_complete'
        with self.assertRaisesRegex(RuntimeError, '미종결'):
            bridge.prepare_carryover(self.queue.snapshot_jobs(), 'production')

    def test_backlog_progress_only_resolves_reviewed_articles(self):
        self.queue = MemoryQueue(source(count=40))
        job = self.collect()
        self.complete(job, 'ANALYSIS_LIMIT_EXCLUDED')
        saved = self.queue.files['jobs/fresh.json']
        ids = [a['article_id'] for a in job['input']['items']]
        saved['recovery_decisions'].update({aid: 'SENT' for aid in ids[:15]})
        saved['recovery_decisions_hash'] = bridge.digest(saved['recovery_decisions'])
        _, _, items = bridge.prepare_carryover(self.queue.snapshot_jobs(), 'production')
        self.assertEqual(len(items), 25)
        self.assertEqual({a['article_id'] for a in items}, set(ids[15:]))

    def test_predat_failure_timestamp_alone_does_not_block_recovery(self):
        self.queue.files['jobs/old.json']['sending_at'] = '2024-01-01T00:00:00+00:00'
        self.assertEqual(len(self.collect()['input']['items']), 1)

    def test_source_safety_rechecked_at_consume(self):
        job = self.collect()
        self.queue.files['jobs/old.json']['receipts'] = {'recipient': 'uncertain'}
        with self.assertRaisesRegex(RuntimeError, '전송 상태'):
            bridge.validate_recovery_sources(self.queue, job, self.result(job))

    def test_source_full_article_including_description_is_immutable(self):
        job = self.collect()
        job['input']['raw_articles'][0]['description'] = 'changed'
        with self.assertRaisesRegex(RuntimeError, '동일성'):
            bridge.validate_recovery_sources(self.queue, job, self.result(job))

    def test_missing_raw_candidate_requires_proven_dedup(self):
        job = self.collect()
        job['input']['raw_articles'] = []
        with self.assertRaisesRegex(RuntimeError, '누락'):
            bridge.validate_recovery_sources(self.queue, job, self.result(job))
        aid = job['input']['items'][0]['article_id']
        job['input']['recovery_omissions'] = {aid: 'current_seen_or_batch_duplicate'}
        with self.assertRaisesRegex(RuntimeError, '중복 제외 재검증'):
            bridge.validate_recovery_sources(self.queue, job, self.result(job))

    def test_dedup_excluded_is_conservatively_unresolved(self):
        job = self.collect()
        self.complete(job, 'DEDUP_EXCLUDED')
        _, _, items = bridge.prepare_carryover(self.queue.snapshot_jobs(), 'production')
        self.assertEqual(len(items), 1)

    def test_explicit_batch_bypasses_starved_top_items_without_dropping_rest(self):
        self.queue = MemoryQueue(source(count=40))
        chosen = [article(39)['article_id']]
        bridge.collect(self.config, self.queue, recovery_only=True, selected_ids=chosen)
        job = self.queue.files['jobs/fresh.json']
        self.assertEqual([a['article_id'] for a in job['input']['items']], chosen)
        self.assertEqual(len(job['input']['recovery_omissions']), 39)
        self.assertEqual(set(job['input']['recovery_omissions'].values()), {'deferred_operator_selection'})
        self.complete(job)
        _, _, remaining = bridge.prepare_carryover(self.queue.snapshot_jobs(), 'production')
        self.assertEqual(len(remaining), 39)
        self.assertNotIn(chosen[0], {a['article_id'] for a in remaining})

    def test_selection_rejects_duplicate_unknown_empty_and_over_limit(self):
        self.queue = MemoryQueue(source(count=40))
        for ids in ([], ['f'*24], [article(0)['article_id']]*2,
                    [article(n)['article_id'] for n in range(16)]):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                bridge.collect(self.config, self.queue, recovery_only=True, selected_ids=ids)
        self.assertNotIn('jobs/fresh.json', self.queue.files)
        self.assertEqual(self.queue.files['jobs/old.json']['status'], 'pending')

    def test_selection_cannot_be_used_by_scheduled_or_verification(self):
        for mode, only in (('production', False), ('verification', True)):
            with self.assertRaises(ValueError):
                bridge.collect(self.config, self.queue, mode=mode, recovery_only=only,
                               selected_ids=[article(0)['article_id']])

    def test_selected_batch_source_provenance_is_revalidated(self):
        self.queue = MemoryQueue(source(count=4))
        bridge.collect(self.config, self.queue, recovery_only=True,
                       selected_ids=[article(3)['article_id']])
        job = self.queue.files['jobs/fresh.json']
        bridge.validate_recovery_sources(self.queue, job, self.result(job))
        job['input']['recovery_omissions'].pop(article(0)['article_id'])
        with self.assertRaisesRegex(RuntimeError, '누락'):
            bridge.validate_recovery_sources(self.queue, job, self.result(job))

    def test_expiry_notice_after_commit_only_and_failure_does_not_undo(self):
        self.notice.side_effect = RuntimeError('synthetic notification failure')
        self.collect()
        self.notice.assert_called_once()
        self.assertEqual(self.queue.files['jobs/old.json']['status'], 'expired')
        self.assertEqual(self.queue.files['jobs/fresh.json']['status'], 'pending')
        self.assertTrue(self.notice.call_args.kwargs['operational'])

    def test_failed_publication_never_sends_expiry_notice(self):
        self.queue.fail_batch = True
        with self.assertRaises(RuntimeError): self.collect()
        self.notice.assert_not_called()

    def test_already_expired_source_does_not_repeat_expiry_notice(self):
        self.queue.files['jobs/old.json']['status'] = 'expired'
        self.collect()
        self.notice.assert_not_called()

    def test_decision_hash_corruption_fails_closed(self):
        job = self.collect()
        self.complete(job)
        self.queue.files['jobs/fresh.json']['recovery_decisions_hash'] = 'corrupt'
        with self.assertRaisesRegex(RuntimeError, '해시'):
            bridge.prepare_carryover(self.queue.snapshot_jobs(), 'production')


class RecoveryRequest(unittest.TestCase):
    def test_recovery_request_main_only_and_no_validation_flags(self):
        import subscription_request as request
        with patch.dict(os.environ, {'GITHUB_REF': 'refs/heads/main'}):
            self.assertEqual(request.arguments({'operation': 'recover-inputs'}), ['recover-inputs'])
            with self.assertRaises(ValueError):
                request.arguments({'operation': 'recover-inputs', 'self_test': True})
        with patch.dict(os.environ, {'GITHUB_REF': 'refs/heads/feature'}):
            with self.assertRaises(ValueError): request.arguments({'operation': 'recover-inputs'})

    @patch.dict(os.environ, {'GITHUB_REF': 'refs/heads/main'})
    def test_recovery_request_passes_only_bounded_hash_ids(self):
        import subscription_request as request
        aid = 'a' * 24
        self.assertEqual(request.arguments({'operation': 'recover-inputs', 'article_ids': [aid]}),
                         ['recover-inputs', '--article-id', aid])
        for ids in ([], [aid, aid], ['not-a-hash'], ['a'*24]*16):
            with self.assertRaises(ValueError):
                request.arguments({'operation': 'recover-inputs', 'article_ids': ids})
        with self.assertRaises(ValueError):
            request.arguments({'operation': 'collect-test', 'article_ids': [aid]})

    def test_recovery_request_never_sets_history_ack_output(self):
        import runpy
        script = Path(bridge.__file__).with_name('subscription_request.py')
        with tempfile.TemporaryDirectory() as tmp:
            request_path = Path(tmp) / 'subscription_request.json'
            output = Path(tmp) / 'output'
            request_path.write_text('{"operation":"recover-inputs"}')
            real_read = Path.read_text
            def read(path, *args, **kwargs):
                return real_read(request_path if str(path) == 'subscription_request.json' else path, *args, **kwargs)
            with patch.object(Path, 'read_text', read), patch.dict(os.environ, {
                    'GITHUB_REF': 'refs/heads/main', 'GITHUB_OUTPUT': str(output)}), patch('subprocess.run') as run:
                runpy.run_path(str(script), run_name='__main__')
            self.assertIn('production=false', output.read_text())
            run.assert_called_once_with(['python', 'subscription_bridge.py', 'recover-inputs'], check=True)


class GitSnapshot(unittest.TestCase):
    def queue(self):
        with patch.dict(os.environ, {'INSIGHT_QUEUE_TOKEN': 'offline'}):
            return bridge.Queue({'queue_repository': 'synthetic/queue', 'queue_branch': 'main',
                                 'queue_prefix': 'subscription/insight'})

    def test_truncated_tree_rejected(self):
        queue = self.queue()
        queue.request = MagicMock(side_effect=[{'object': {'sha': 'a'*40}},
            {'tree': {'sha': 'b'*40}}, {'truncated': True, 'tree': []}])
        with self.assertRaisesRegex(RuntimeError, '잘린'): queue.snapshot_jobs()

    def test_git_batch_nonforce_and_single_parent(self):
        queue = self.queue()
        queue.request = MagicMock(side_effect=[{'object': {'sha': 'a'*40}},
            {'sha': 'c'*40}, {'sha': 'd'*40}, {'object': {'sha': 'd'*40}}])
        queue.publish_collection({'head': 'a'*40, 'tree': 'b'*40}, {'jobs/new.json': {'x': 1}})
        self.assertEqual(queue.request.call_args_list[2].args[2]['parents'], ['a'*40])
        self.assertEqual(queue.request.call_args_list[3].args[1:], ('PATCH', {'sha': 'd'*40, 'force': False}))

    def test_git_batch_head_change_prevents_writes(self):
        queue = self.queue()
        queue.request = MagicMock(return_value={'object': {'sha': 'c'*40}})
        with self.assertRaisesRegex(RuntimeError, 'CAS'):
            queue.publish_collection({'head': 'a'*40, 'tree': 'b'*40}, {})
        self.assertEqual(queue.request.call_count, 1)

    def test_git_batch_lost_final_response_never_retries(self):
        queue = self.queue()
        queue.request = MagicMock(side_effect=[{'object': {'sha': 'a'*40}},
            {'sha': 'c'*40}, {'sha': 'd'*40}, TimeoutError('uncertain')])
        with self.assertRaises(TimeoutError):
            queue.publish_collection({'head': 'a'*40, 'tree': 'b'*40}, {})
        self.assertEqual(queue.request.call_count, 4)


if __name__ == '__main__': unittest.main()
