"""만료·SMTP 시도 전 실패·알림·push 재시도 검수. 실제 네트워크/발송/API 없음."""
import copy
from contextlib import ExitStack, redirect_stdout
from datetime import timedelta
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import yaml

from test_subscription_bridge import MemoryQueue, fixture, bot, bridge


class StallRegression(unittest.TestCase):
    def setUp(self):
        self.job, self.result = fixture()
        self.queue = MemoryQueue(self.job, self.result)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(redirect_stdout(StringIO()))
        self.stack.enter_context(patch.object(bot, 'INSIGHT_ENGINE', 'subscription'))
        self.stack.enter_context(patch.object(bot, 'TEST_MODE', False))
        self.stack.enter_context(patch.object(bot, 'DRY_RUN', False))
        self.api = self.stack.enter_context(patch.object(bot, '_ai_create', side_effect=AssertionError('paid API forbidden')))
        self.smtp = self.stack.enter_context(patch.object(bot.smtplib, 'SMTP_SSL', side_effect=AssertionError('real SMTP forbidden')))
        self.seen = self.stack.enter_context(patch.object(bot, 'save_seen'))
        self.job_path = 'jobs/' + self.job['job_id'] + '.json'

    def collect(self):
        collector = MagicMock()
        collector.collect_articles.return_value = []
        collector.hard_filter.return_value = ([], [])
        with patch.object(bridge, 'import_bot', return_value=collector), patch.object(bridge, 'seen_hash', return_value='seen'), patch.object(bridge, 'notify_error') as notify, patch.dict(os.environ, {'GITHUB_RUN_ID': 'new-production'}):
            bridge.collect({'engine': 'subscription', 'job_ttl_minutes': 120}, self.queue)
        return collector, notify

    def expire(self):
        self.queue.files[self.job_path]['expires_at'] = (bridge.now() - timedelta(minutes=1)).isoformat()

    def test_expired_unstarted_job_unblocks_collection_without_losing_input(self):
        self.expire()
        before = copy.deepcopy(self.queue.files[self.job_path])
        collector, notify = self.collect()
        old = self.queue.get(self.job_path)[0]
        self.assertEqual(old['status'], 'expired')
        self.assertIn('expired_at', old)
        self.assertEqual(old['input'], before['input'])
        self.assertEqual(old['input_hash'], before['input_hash'])
        self.assertEqual(self.queue.get('results/' + self.job['job_id'] + '.json')[0], self.result)
        new = self.queue.get('jobs/new-production.json')[0]
        self.assertEqual(new['status'], 'pending')
        self.assertEqual(new['input']['seen_hash'], 'seen')
        collector.save_seen.assert_not_called()
        notify.assert_called_once()
        self.assertIn('보장되지', notify.call_args.args[1])
        self.api.assert_not_called()
        self.smtp.assert_not_called()

    def test_live_pending_job_still_blocks_new_collection(self):
        with self.assertRaisesRegex(RuntimeError, '미종결'):
            self.collect()
        self.assertEqual(self.queue.get(self.job_path)[0]['status'], 'pending')
        self.assertIsNone(self.queue.get('jobs/new-production.json')[0])

    def test_expired_pending_with_any_receipt_remains_blocked(self):
        self.expire()
        for receipt in ('sending', 'accepted', 'rejected', 'uncertain'):
            with self.subTest(receipt=receipt):
                self.queue.files[self.job_path]['receipts'] = {'recipient-hash': receipt}
                with self.assertRaisesRegex(RuntimeError, '미종결'):
                    self.collect()
                self.assertEqual(self.queue.get(self.job_path)[0]['status'], 'pending')
                self.assertIsNone(self.queue.get('jobs/new-production.json')[0])

    def test_started_states_never_expire_even_without_receipts(self):
        self.expire()
        for state in ('sending', 'uncertain', 'delivery_complete'):
            with self.subTest(state=state):
                self.queue.files[self.job_path]['status'] = state
                with self.assertRaisesRegex(RuntimeError, '미종결'):
                    self.collect()
                self.assertEqual(self.queue.get(self.job_path)[0]['status'], state)

    def test_missing_receipts_or_delivery_marker_never_auto_expire(self):
        self.expire()
        original = copy.deepcopy(self.queue.files[self.job_path])
        for change in ({'receipts': None}, {'receipts': []}, {'delivered_at': 'recorded'}, {'seen_after_hash': 'after'}):
            with self.subTest(change=change):
                self.queue.files[self.job_path] = {**original, **change}
                with self.assertRaisesRegex(RuntimeError, '미종결'):
                    self.collect()
                self.assertEqual(self.queue.get(self.job_path)[0]['status'], 'pending')

    def test_expiry_cas_conflict_stops_without_overwriting_job(self):
        self.expire()
        with patch.object(self.queue, 'put', side_effect=RuntimeError('CAS mismatch')):
            with self.assertRaisesRegex(RuntimeError, 'CAS'):
                self.collect()
        self.assertEqual(self.queue.get(self.job_path)[0]['status'], 'pending')
        self.assertIsNone(self.queue.get('jobs/new-production.json')[0])

    def test_invalid_expiry_is_not_treated_as_safe_to_delete(self):
        for expiry in ('not-a-date', '2020-01-01T00:00:00'):
            with self.subTest(expiry=expiry):
                self.queue.files[self.job_path]['expires_at'] = expiry
                with self.assertRaises((ValueError, TypeError)):
                    self.collect()
                self.assertEqual(self.queue.get(self.job_path)[0]['status'], 'pending')

    def test_expired_terminal_job_is_ignored_on_next_collection(self):
        self.queue.files[self.job_path]['status'] = 'expired'
        _, notify = self.collect()
        self.assertEqual(self.queue.get('jobs/new-production.json')[0]['status'], 'pending')
        notify.assert_not_called()

    def test_preflight_failure_returns_to_pending_with_no_smtp_or_seen(self):
        self.queue.files[self.job_path]['mode'] = 'production'
        with patch.object(bridge, 'import_bot', return_value=bot), patch.object(bridge, 'seen_hash', return_value='seen'), patch.object(bot, 'preflight', return_value=False):
            with self.assertRaisesRegex(RuntimeError, 'SMTP 사전'):
                bridge.consume({'engine': 'subscription'}, self.queue, self.job['job_id'])
        state = self.queue.get(self.job_path)[0]
        self.assertEqual(state['status'], 'pending')
        self.assertEqual(state['receipts'], {})
        self.assertIsNone(bot.DELIVERY_GUARD)
        self.smtp.assert_not_called()
        self.seen.assert_not_called()
        self.api.assert_not_called()

    def test_failure_after_any_reservation_remains_uncertain(self):
        for receipt in ('sending', 'accepted', 'rejected', 'uncertain'):
            with self.subTest(receipt=receipt):
                self.queue = MemoryQueue(self.job, self.result)
                self.queue.files[self.job_path]['mode'] = 'production'
                def fail_after_reservation(provider):
                    bot.DELIVERY_GUARD.before('owner@example.invalid')
                    if receipt != 'sending':
                        bot.DELIVERY_GUARD.after('owner@example.invalid', receipt)
                    raise RuntimeError('failure after reservation')
                with patch.object(bridge, 'import_bot', return_value=bot), patch.object(bridge, 'seen_hash', return_value='seen'), patch.object(bot, 'main', side_effect=fail_after_reservation):
                    with self.assertRaisesRegex(RuntimeError, 'after reservation'):
                        bridge.consume({'engine': 'subscription'}, self.queue, self.job['job_id'])
                state = self.queue.get(self.job_path)[0]
                self.assertEqual(state['status'], 'uncertain')
                self.assertEqual(list(state['receipts'].values()), [receipt])
        self.api.assert_not_called()
        self.smtp.assert_not_called()

    def test_error_notice_uses_admin_route_without_production_receipts(self):
        existing_guard = MagicMock()
        output = StringIO()
        with patch.dict(os.environ, {'GMAIL_USER': 'owner@example.invalid', 'GMAIL_APP_PASSWORD': 'offline', 'GITHUB_RUN_ID': '123'}), patch.object(bridge, 'import_bot', return_value=bot), patch.object(bot, 'NO_RESULT_RECEIVER', 'admin@example.invalid'), patch.object(bot, 'DELIVERY_GUARD', existing_guard), patch.object(bot, '_send_one', return_value={}) as sender, redirect_stdout(output):
            bridge.notify_error(RuntimeError('offline failure'), 'safe context')
            self.assertIs(bot.DELIVERY_GUARD, existing_guard)
        self.assertEqual({c.args[2] for c in sender.call_args_list}, {'admin@example.invalid', 'owner@example.invalid'})
        self.assertNotIn('risk_aigent@googlegroups.com', {c.args[2] for c in sender.call_args_list})
        self.assertIn('/actions/runs/123', sender.call_args.args[1])
        self.assertNotIn('@example.invalid', output.getvalue())
        existing_guard.before.assert_not_called()
        existing_guard.after.assert_not_called()
        self.api.assert_not_called()

    def test_missing_smtp_settings_skips_notice_without_importing_bot(self):
        with patch.dict(os.environ, {'GMAIL_APP_PASSWORD': ''}), patch.object(bridge, 'import_bot') as imported:
            bridge.notify_error(RuntimeError('offline'))
            imported.assert_not_called()

    def test_notice_failure_keeps_original_cli_failure(self):
        with patch.dict(os.environ, {'GMAIL_USER': 'owner@example.invalid', 'GMAIL_APP_PASSWORD': 'offline'}), patch.object(bridge, 'main', side_effect=RuntimeError('original failure')), patch.object(bridge, 'import_bot', return_value=bot), patch.object(bot, 'send_email_error', side_effect=OSError('notification failed')):
            self.assertEqual(bridge.cli(), 1)
        self.api.assert_not_called()

    def test_cli_exposes_only_safe_queue_diagnostics(self):
        for message, public in (
                ('비공개 대기 저장소 PUT 실패 HTTP 403', True),
                ('비공개 저장소 쓰기 후 읽기 검증 실패', True),
                ('SMTP failure owner@example.invalid https://private.invalid/article 비공개기사', False)):
            output = StringIO()
            with self.subTest(message=message), patch.object(bridge, 'main', side_effect=RuntimeError(message)), patch.object(bridge, 'notify_error') as notice, redirect_stdout(output):
                self.assertEqual(bridge.cli(), 1)
                self.assertEqual(message in output.getvalue(), public)
                notice.assert_called_once()
                self.assertEqual(str(notice.call_args.args[0]), message)

    def test_error_notice_owner_copy_does_not_mask_admin_refusal(self):
        def send(subject, html, address):
            return {address: (550, 'refused')} if address == 'admin@example.invalid' else {}
        output = StringIO()
        with patch.dict(os.environ, {'GMAIL_USER': 'owner@example.invalid', 'GMAIL_APP_PASSWORD': 'offline'}), patch.object(bridge, 'import_bot', return_value=bot), patch.object(bot, 'NO_RESULT_RECEIVER', 'admin@example.invalid'), patch.object(bot, '_send_one', side_effect=send), redirect_stdout(output):
            bridge.notify_error(RuntimeError('offline'), 'safe context')
        self.assertIn('접수 미확인', output.getvalue())
        self.assertNotIn('알림 SMTP 접수;', output.getvalue())
        self.assertNotIn('@example.invalid', output.getvalue())


class StatePushRegression(unittest.TestCase):
    def setUp(self):
        workflow = yaml.safe_load(Path('.github/workflows/subscription_consume.yml').read_text())
        self.steps = workflow['jobs']['consume']['steps']
        self.command = next(s['run'] for s in self.steps if s.get('name') == '발송 완료 처리 이력 저장')

    def push(self, failures, conflict=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_path = root / 'bin'
            bin_path.mkdir()
            stub = bin_path / 'git'
            stub.write_text('''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
root=Path(os.environ['STUB_GIT_DIR'])
args=sys.argv[1:]
with (root/'commands.jsonl').open('a') as f:
 f.write(json.dumps(args)+'\\n')
if args[:2]==['diff','--staged']:
 sys.exit(1)
if args[0]=='push':
 count_path=root/'pushes'
 count=int(count_path.read_text())+1 if count_path.exists() else 1
 count_path.write_text(str(count))
 sys.exit(1 if count<=int(os.environ['STUB_PUSH_FAILURES']) else 0)
if args[0]=='pull' and os.environ['STUB_PULL_CONFLICT']=='1':
 sys.exit(1)
sys.exit(0)
''')
            stub.chmod(0o755)
            pause = bin_path / 'sleep'
            pause.write_text('#!/bin/sh\nexit 0\n')
            pause.chmod(0o755)
            env = {**os.environ, 'PATH': str(bin_path) + os.pathsep + os.environ['PATH'],
                   'STUB_GIT_DIR': str(root), 'STUB_PUSH_FAILURES': str(failures),
                   'STUB_PULL_CONFLICT': '1' if conflict else '0'}
            run = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', self.command], cwd=root, env=env, capture_output=True, text=True, timeout=15)
            commands = [json.loads(l) for l in (root / 'commands.jsonl').read_text().splitlines()]
        self.assertTrue(all('--force' not in c and '-f' not in c for c in commands if c[0] == 'push'))
        return run, commands

    def test_push_succeeds_without_unnecessary_retry(self):
        run, commands = self.push(0)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(sum(c[0] == 'push' for c in commands), 1)
        self.assertEqual(sum(c[0] == 'pull' for c in commands), 0)

    def test_non_fast_forward_then_rebase_and_push_succeeds(self):
        run, commands = self.push(1)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(sum(c[0] == 'push' for c in commands), 2)
        self.assertEqual(sum(c[0] == 'pull' for c in commands), 1)

    def test_three_push_failures_exit_failed_without_ack(self):
        run, commands = self.push(3)
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(sum(c[0] == 'push' for c in commands), 3)
        self.assertIn('종결 보류', run.stdout)
        self.assertFalse(any('ack' in c for c in commands))

    def test_rebase_conflict_aborts_without_pushing_conflicted_history(self):
        run, commands = self.push(3, conflict=True)
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(sum(c[0] == 'push' for c in commands), 1)
        self.assertIn(['rebase', '--abort'], commands)
        self.assertIn('처리 이력 충돌', run.stdout)

    def test_post_delivery_failure_notice_does_not_duplicate_consume_error_notice(self):
        notice = next(s for s in self.steps if s.get('name') == '발송 후 저장·종결 실패 담당자 알림')
        self.assertEqual(notice['if'], "failure() && steps.result.outcome == 'success'")
        self.assertNotIn('ANTHROPIC_API_KEY', notice['env'])
        self.assertEqual(notice['env']['NAVER_CLIENT_ID'], 'notification-only')
        self.assertNotIn('RECIPIENTS', notice['env'])


if __name__ == '__main__':
    unittest.main()
