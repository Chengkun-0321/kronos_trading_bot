"""以合成訊號與平台替身驗證調倉和外部副作用邊界。"""
import argparse
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd
import requests

from src.environment import read_env, load_env
from src.strategy import (Journal, STRATEGY, TAIPEI, check_holdings, execute, execute_orders,
                          forecast, rebalance, render, weekly_session)
from src.trading import Platform
from src.notify import send


def signals(count=25):
    """產生排名遞減且全數正值的合成訊號list。"""
    return [dict(symbol=f'{i:04}', name='測試', score=.3-i*.005, last_close=100+i)
            for i in range(1, count+1)]


class WeekCalendar:
    """只供測試的週末及自訂休市日曆。"""
    def __init__(self, closed=()):
        """closed為date集合。"""
        self.closed = set(closed)

    def is_session(self, day):
        """回傳測試日是否開市。"""
        return day.weekday() < 5 and day not in self.closed

    def shift(self, day, direction):
        """依方向回傳下一個合成交易日。"""
        while True:
            day += timedelta(days=direction)
            if self.is_session(day):
                return day

    def window(self, day):
        """回傳截至day的120個合成交易日。"""
        dates = [day]
        for _ in range(119):
            dates.append(self.shift(dates[-1], -1))
        return sorted(dates)


class StrategyTests(unittest.TestCase):
    """每次建立獨立策略庫，不連線、不使用GPU。"""
    def setUp(self):
        """建立隔離SQLite與預設空倉調倉計畫。"""
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.journal = Journal(self.root / 'strategy.sqlite3')
        self.report = dict(date='2026-09-18', model_id=STRATEGY, target_date='2026-09-21',
                           target_dates=['2026-09-21', '2026-09-22', '2026-09-23', '2026-09-24', '2026-09-25'],
                           predictions=signals(), skipped=[], plan=rebalance(signals(), {}))
        self.broker = Mock()
        self.broker.submit.return_value = {'state': 'accepted', 'raw': {'result': 'success'}}

    def tearDown(self):
        """釋放測試資料。"""
        self.journal.db.close()
        self.temp.cleanup()

    def test_last_week_session_and_year_boundary(self):
        """休市週提前至週四，跨年不以年份判斷週別。"""
        cal = WeekCalendar([date(2026, 9, 25)])
        self.assertTrue(weekly_session(cal, date(2026, 9, 24)))
        self.assertFalse(weekly_session(cal, date(2026, 9, 25)))
        self.assertFalse(weekly_session(cal, date(2026, 9, 23)))
        self.assertFalse(weekly_session(cal, date(2026, 9, 26)))
        self.assertTrue(weekly_session(WeekCalendar([date(2027, 1, 1)]), date(2026, 12, 31)))

    def test_buy_threshold_and_ties(self):
        """1%不買、同分依代號且Top10以外不可買入。"""
        rows = [dict(symbol='B', score=.01, last_close=10), dict(symbol='A', score=.010001, last_close=20)]
        self.assertEqual([o['symbol'] for o in rebalance(rows, {})['orders']], ['A'])
        tied = [dict(p, score=.1) for p in reversed(signals())]
        plan = rebalance(tied, {})
        self.assertEqual(list(plan['target']), [f'{i:04}' for i in range(1, 11)])
        self.assertTrue(all(o['lots'] == 10 for o in plan['orders']))

    def test_forced_sell_and_keep_missing(self):
        """掉出20與零分整檔賣出，缺訊號保留且占位。"""
        rows = signals()
        rows[1]['score'] = 0
        plan = rebalance(rows, {'0025': 17, '0002': 2.5, 'missing': 3})
        sells = {o['symbol']: o['lots'] for o in plan['orders'] if o['side'] == 'sell'}
        self.assertEqual(sells, {'0025': 17, '0002': 2.5})
        self.assertEqual(len(plan['target']), 10)
        self.assertEqual(plan['target']['missing'], 3)
        self.assertTrue(plan['warnings'])

    def test_rank_twenty_positive_kept(self):
        """沒有合格新候選時，第20名正值持股續抱。"""
        plan = rebalance([dict(p, score=.01) for p in signals()], {'0020': 10})
        self.assertEqual(plan['orders'], [])

    def test_all_ten_can_be_replaced(self):
        """不再限制主動換兩檔，十檔可全換且全賣先於買。"""
        plan = rebalance(signals(), {f'{i:04}': 10 for i in range(11, 21)})
        self.assertEqual(len(plan['orders']), 20)
        self.assertTrue(all(o['side'] == 'sell' for o in plan['orders'][:10]))
        self.assertTrue(all(o['depends_on'] for o in plan['orders'][10:]))
        self.assertEqual(set(plan['target']), {f'{i:04}' for i in range(1, 11)})

    def test_existing_top_ten_not_bought_again(self):
        """既有持股不補張數，優勢候選不能換掉更高排名。"""
        holdings = {f'{i:04}': 2 for i in range(1, 11)}
        self.assertEqual(rebalance(signals(), holdings)['orders'], [])

    def test_over_capacity_and_pending(self):
        """超額既有持股不擅自清倉，未結候選不重買。"""
        holdings = {f'{i:04}': 10 for i in range(1, 12)}
        self.assertEqual(rebalance(signals(), holdings)['orders'], [])
        plan = rebalance(signals(), {}, {'0001'})
        self.assertNotIn('0001', plan['target'])
        self.assertEqual(len(plan['target']), 9)

    def test_invalid_holdings_and_duplicate_signals(self):
        """格式錯誤不可當作空倉。"""
        for value in ([], {'A': 0}, {'A': True}, {'A': float('nan')}, {'A': -1}):
            with self.assertRaises(ValueError):
                check_holdings(value)
        with self.assertRaises(ValueError):
            rebalance(signals()+signals(), {})

    def test_accepted_not_filled_and_rerun_no_post(self):
        """已受理保留原始結果與未結代號；重跑不重送。"""
        execute_orders(self.journal, self.report, self.broker)
        execute_orders(self.journal, self.report, self.broker)
        self.assertEqual(self.broker.submit.call_count, 10)
        self.assertEqual(len(self.journal.unresolved()), 10)
        raw = self.journal.db.execute('SELECT response FROM orders LIMIT 1').fetchone()[0]
        self.assertEqual(json.loads(raw), {'result': 'success'})

    def test_insufficient_funds_stops_and_persists(self):
        """資金不足後本輪停止買進，重跑也不繼續。"""
        self.broker.submit.side_effect = [{'state': 'accepted'}, {'state': 'insufficient_funds'}]
        self.assertEqual(execute_orders(self.journal, self.report, self.broker), 'insufficient_funds')
        execute_orders(self.journal, self.report, self.broker)
        self.assertEqual(self.broker.submit.call_count, 2)

    def test_timeout_and_crash_do_not_retry(self):
        """連線不明及送出後崩潰都停止，SQLite重開後仍保留阻擋狀態。"""
        self.broker.submit.side_effect = requests.Timeout()
        with self.assertRaises(RuntimeError):
            execute_orders(self.journal, self.report, self.broker)
        self.journal.db.close()
        self.journal = Journal(self.root / 'strategy.sqlite3')
        with self.assertRaises(RuntimeError):
            execute_orders(self.journal, self.report, self.broker)
        self.assertEqual(self.broker.submit.call_count, 1)
        self.journal.transition(self.report['date'], 1, 'sending')
        with self.assertRaises(RuntimeError):
            execute_orders(self.journal, self.report, self.broker)

    def test_rejected_sell_does_not_release_capacity(self):
        """賣單被拒絕不得釋放名額或執行其替換買單。"""
        self.report['plan'] = rebalance(signals(), {f'{i:04}': 10 for i in range(11, 21)})
        self.broker.submit.return_value = {'state': 'rejected'}
        execute_orders(self.journal, self.report, self.broker)
        self.assertEqual(self.broker.submit.call_count, 10)
        self.assertTrue(all(c.args[0]['side'] == 'sell' for c in self.broker.submit.call_args_list))

    def test_unknown_reply_and_cross_week_block(self):
        """未知狀態不冒充拒絕；跨週未結委託阻擋執行。"""
        self.broker.submit.return_value = {'state': 'unrecognized'}
        with self.assertRaises(RuntimeError):
            execute_orders(self.journal, self.report, self.broker)
        self.journal.transition(self.report['date'], 1, 'accepted')
        next_report = dict(self.report, date='2026-09-25')
        with self.assertRaises(RuntimeError):
            execute_orders(self.journal, next_report, self.broker)
        self.assertEqual(self.broker.submit.call_count, 1)

    def test_forecast_uses_fifth_close_first_open(self):
        """預測完整五根並排除缺日與中間OHLC矛盾的商品。"""
        cal, day = WeekCalendar(), date(2026, 9, 18)
        frame = pd.DataFrame([dict(date=d.isoformat(), open=100, high=120, low=90, close=100,
                                   volume=100, amount=10000) for d in cal.window(day)])
        store, predictor = Mock(), Mock()
        store.history.return_value = frame
        path = [dict(open=100, high=120, low=90, close=100+i, volume=100, amount=10000) for i in range(5)]
        predictor.predict_path.return_value = path
        report = forecast(store, {'ETF': {'name': 'ETF'}}, cal, day, lambda: predictor)
        self.assertAlmostEqual(report['predictions'][0]['score'], .04)
        self.assertEqual(len(predictor.predict_path.call_args.args[2]), 5)
        path[2]['high'] = 80
        self.assertEqual(forecast(store, {'ETF': {'name': 'ETF'}}, cal, day, lambda: predictor)['predictions'], [])
        frame.loc[0, 'date'] = '2000-01-01'
        predictor.reset_mock()
        self.assertEqual(len(forecast(store, {'ETF': {'name': 'ETF'}}, cal, day, lambda: predictor)['skipped']), 1)
        predictor.predict_path.assert_not_called()

    def test_live_gate_has_no_network(self):
        """正式CLI及API皆無可繞過的環境開關。"""
        session = Mock()
        with self.assertRaises(RuntimeError):
            Platform(session).submit({})
        with self.assertRaises(RuntimeError):
            execute(argparse.Namespace(execute=True))
        session.post.assert_not_called()

    def test_platform_empty_failure_and_unknown_schema(self):
        """僅明確成功的空list視為空倉。"""
        session = Mock()
        with patch.dict(os.environ, {'account': 'test-account', 'password': 'test-password'}):
            session.post.return_value.json.return_value = {'result': 'success', 'data': []}
            self.assertEqual(Platform(session).holdings(), {})
            for response in ({'result': 'error', 'data': []}, {'result': 'success', 'data': [{'unknown': 1}]}):
                session.post.return_value.json.return_value = response
                with self.assertRaises(RuntimeError):
                    Platform(session).holdings()

    def test_dry_run_archive_reused_preview_and_notify(self):
        """歷史模擬不碰平台、重跑重用封存、預览不用行情或GPU。"""
        self.journal.db.close()
        self.journal = Journal(self.root / 'other.sqlite3')
        fixture = self.root / 'holdings.json'
        fixture.write_text('{}')
        args = argparse.Namespace(command='strategy', execute=False, db=self.root/'market.sqlite3',
                                  date='2026-09-18', send=False, holdings=fixture)
        now = datetime(2026, 9, 22, 19, tzinfo=TAIPEI)
        with patch('src.strategy.Calendar', return_value=WeekCalendar()) as calendar, patch('src.strategy.DataSource') as source, patch('src.strategy.forecast', return_value=self.report) as predict, patch('src.strategy.Platform') as platform, patch('builtins.print'):
            calendar.return_value.http = Mock()
            source.return_value.universe.return_value = {}
            execute(args, now)
            execute(args, now)
            self.assertEqual(predict.call_count, 1)
            platform.assert_not_called()
            args.command, args.json = 'strategy-preview', True
            execute(args, now)
        archive = self.root / 'reports' / f'2026-09-18.{STRATEGY}.json'
        self.assertTrue(archive.exists())
        self.assertIn('未下單', render(self.report))
        session = Mock()
        session.post.return_value.status_code = 200
        session.post.return_value.json.return_value = {'id': '123'}
        send(self.journal, self.report, 'https://discord.com/api/webhooks/test', session)
        send(self.journal, self.report, 'https://discord.com/api/webhooks/test', session)
        self.assertEqual(session.post.call_count, 1)
        self.assertIn('五日策略', session.post.call_args.kwargs['json']['content'])

    def test_dates_and_historical_send_are_blocked(self):
        """19:00前、未來日、歷史通知都在下載前停止。"""
        args = argparse.Namespace(command='strategy', execute=False, db=self.root/'market.sqlite3',
                                  date=None, send=False, holdings=None)
        with self.assertRaises(ValueError):
            execute(args, datetime(2026, 9, 18, 18, 59, tzinfo=TAIPEI))
        args.date, args.send = '2026-09-18', True
        with self.assertRaises(ValueError):
            execute(args, datetime(2026, 9, 22, 19, tzinfo=TAIPEI))

    def test_resume_after_accepted_batch_interruption(self):
        """第一筆受理後中斷，重開SQLite僅送尚未發出的訂單。"""
        self.journal.initialize_orders(self.report)
        self.journal.transition(self.report['date'], 1, 'accepted', {'result': 'success'})
        self.journal.db.close()
        self.journal = Journal(self.root / 'strategy.sqlite3')
        execute_orders(self.journal, self.report, self.broker)
        self.assertEqual(self.broker.submit.call_count, 9)
        self.assertNotIn('0001', [c.args[0]['symbol'] for c in self.broker.submit.call_args_list])

    def test_sell_accepted_releases_target_capacity(self):
        """不用假裝成交便能補位，但仍保留所有accepted為待核對。"""
        self.report['plan'] = rebalance(signals(), {f'{i:04}': 10 for i in range(11, 21)})
        execute_orders(self.journal, self.report, self.broker)
        self.assertEqual(self.broker.submit.call_count, 20)
        self.assertEqual(len(self.journal.unresolved()), 20)

    def test_archived_order_cannot_change_on_retry(self):
        """相同委託id改價不會繼續發出POST。"""
        self.journal.initialize_orders(self.report)
        self.report['plan']['orders'][0]['price'] = 1
        with self.assertRaises(ValueError):
            execute_orders(self.journal, self.report, self.broker)
        self.broker.submit.assert_not_called()

    def test_platform_order_mapping_and_redaction_with_fake_http(self):
        """僅測試替身移除契約閘門，核對API張數及去機密原始結果。"""
        session = Mock()
        session.post.return_value.status_code = 200
        session.post.return_value.json.return_value = {'result': 'success', 'status': 'queued',
                                                      'echo': 'fake-account fake-password'}
        order = dict(symbol='2330', side='buy', lots=10, price=100)
        with patch('src.trading.require_trading_contract'), patch.dict(os.environ, {'account': 'fake-account', 'password': 'fake-password'}):
            result = Platform(session).submit(order)
            self.assertEqual(result['state'], 'accepted')
            self.assertEqual(session.post.call_args.kwargs['data']['stock_shares'], 10)
            self.assertEqual(result['raw']['echo'], '[redacted] [redacted]')
            session.post.return_value.json.return_value = {'result': 'error', 'status': 'unknown-format'}
            self.assertEqual(Platform(session).submit(order)['state'], 'unknown')

    def test_non_week_end_skips_platform_and_inference(self):
        """非調倉日不查持股、不取行情、不推論。"""
        args = argparse.Namespace(command='strategy', execute=False, db=self.root/'market.sqlite3',
                                  date='2026-09-22', send=False, holdings=None)
        with patch('src.strategy.Calendar', return_value=WeekCalendar()), patch('src.strategy.Platform') as platform, patch('src.strategy.forecast') as predict:
            self.assertIsNone(execute(args, datetime(2026, 9, 22, 19, tzinfo=TAIPEI)))
            platform.assert_not_called()
            predict.assert_not_called()

    def test_live_flow_with_fakes_has_separate_report(self):
        """以完整替身測試被封鎖流程的接線，正式與模擬報告不混用。"""
        self.journal.save_run(self.report)
        args = argparse.Namespace(command='strategy', execute=True, db=self.root/'market.sqlite3',
                                  date='2026-09-18', send=False, holdings=None)
        now = datetime(2026, 9, 18, 19, tzinfo=TAIPEI)
        report = dict(self.report)
        report.pop('plan')
        broker = Mock()
        broker.holdings.return_value = {}
        broker.submit.return_value = {'state': 'accepted', 'raw': {'result': 'success'}}
        calendar = WeekCalendar()
        calendar.http = Mock()
        with patch('src.strategy.require_trading_contract'), patch('src.strategy.Platform', return_value=broker), patch('src.strategy.Calendar', return_value=calendar), patch('src.strategy.DataSource'), patch('src.strategy.forecast', return_value=report), patch('src.strategy.datetime') as clock, patch.dict(os.environ, {'account': 'fake-account'}), patch('builtins.print'):
            clock.now.return_value = now
            live_report = execute(args, now)
            execute(args, now)
        self.assertEqual(live_report['mode'], 'live')
        self.assertEqual(broker.submit.call_count, 10)
        self.assertEqual(len(live_report['order_states']), 10)
        self.assertIsNotNone(self.journal.run('2026-09-18', STRATEGY))
        self.assertIsNotNone(self.journal.run('2026-09-18', 'weekly-v1-live'))

    def test_env_does_not_execute_or_override(self):
        """環境檔字串不執行shell，程序環境优先。"""
        path = self.root / '.env'
        path.write_text('account="test"\npassword=\'$(echo never)\'\n')
        self.assertEqual(read_env(path)['password'], '$(echo never)')
        with patch('src.environment.ROOT', self.root), patch.dict(os.environ, {'account': 'external'}):
            load_env()
            self.assertEqual(os.environ['account'], 'external')


if __name__ == '__main__':
    unittest.main()
