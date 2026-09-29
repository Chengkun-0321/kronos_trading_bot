"""一次性補跑的日期、價格分離與不重送測試；全部使用替身。"""
import argparse
import os
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd
import requests

from src.catchup import (SIGNAL, PRICE, TARGETS, RUN_ID, CatchupPlatform, buy_plan,
                         check_date, execute, render, submit_once)
from src.strategy import Journal, TAIPEI, forecast
from src.trading import Platform
from test_strategy import WeekCalendar, signals


class CatchupTests(unittest.TestCase):
    """每項使用新SQLite，不能觸及真實持股、買單或Discord。"""
    def setUp(self):
        """準備兩日價差明顯的行情、訊號及買單替身。"""
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.journal = Journal(self.root/'strategy.sqlite3')
        self.market = Mock()
        self.market.history.return_value = pd.DataFrame([dict(date='2026-09-21', close=567)])
        self.report = dict(date=SIGNAL.isoformat(), model_id=RUN_ID, predictions=signals(),
                           skipped=[], target_dates=TARGETS, plan=buy_plan(self.market, signals()))
        self.broker = Mock()
        self.broker.holdings.return_value = {}
        self.broker.submit.return_value = {'state': 'accepted', 'raw': {'result': 'success'}}

    def tearDown(self):
        """釋放測試庫。"""
        self.journal.db.close()
        self.temp.cleanup()

    def test_prices_separate_from_signal_and_missing_not_backfilled(self):
        """限價須取9/21，缺價不改用9/18或第11名。"""
        plan = self.report['plan']
        self.assertEqual(len(plan['orders']), 10)
        self.assertTrue(all(o['price'] == 567 and o['lots'] == 10 for o in plan['orders']))
        self.market.history.assert_called_with('0010', '2026-09-21', limit=1)
        self.market.history.return_value = pd.DataFrame([dict(date='2026-09-18', close=100)])
        plan = buy_plan(self.market, signals())
        self.assertEqual(plan['orders'], [])
        self.assertEqual(len(plan['price_skipped']), 10)

    def test_threshold_and_invalid_prices(self):
        """精確1%不買，NaN限價也不能進委託。"""
        self.assertEqual(buy_plan(self.market, [dict(p, score=.01) for p in signals()])['orders'], [])
        self.market.history.return_value = pd.DataFrame([dict(date='2026-09-21', close=float('nan'))])
        self.assertEqual(buy_plan(self.market, signals())['orders'], [])

    def test_four_sessions_and_no_future_input(self):
        """有9/21資料的真實SQLite也只能餵入截至9/18的120根。"""
        cal = WeekCalendar([date(2026, 9, 25), date(2026, 9, 28)])
        rows = [dict(symbol='ETF', name='ETF', market='TWSE', date=d.isoformat(),
                     open=100, high=120, low=90, close=100, volume=100, amount=10000)
                for d in cal.window(SIGNAL)+[PRICE]]
        self.journal.save_bars(rows, 'TWSE', '2026-09-21')
        predictor = Mock()
        predictor.predict_path.return_value = [dict(open=100, high=120, low=90, close=100+i,
                                                    volume=100, amount=10000) for i in range(4)]
        report = forecast(self.journal, {'ETF': {'name': 'ETF'}}, cal, SIGNAL, lambda: predictor, horizon=4)
        self.assertEqual(report['target_dates'], TARGETS)
        self.assertEqual(report['horizon'], 4)
        self.assertAlmostEqual(report['predictions'][0]['score'], .03)
        frame = predictor.predict_path.call_args.args[1]
        self.assertEqual(len(frame), 120)
        self.assertEqual(frame.iloc[-1]['date'], '2026-09-18')

    def test_date_gate_and_general_gate_unchanged(self):
        """補跑例外隔日失效，一般委託仍完全停用。"""
        check_date(datetime(2026, 9, 22, tzinfo=TAIPEI))
        with self.assertRaises(ValueError):
            check_date(datetime(2026, 9, 23, tzinfo=TAIPEI))
        with self.assertRaises(RuntimeError):
            Platform(Mock()).submit({})

    def test_all_accepted_then_rerun_never_submits(self):
        """第一輪空倉查詢後建倉，已受理結果不當成交且不重送。"""
        submit_once(self.journal, self.report, self.broker)
        submit_once(self.journal, self.report, self.broker)
        self.assertEqual(self.broker.submit.call_count, 10)
        self.assertEqual(self.broker.holdings.call_count, 1)
        self.assertEqual(len(self.journal.unresolved()), 10)

    def test_refusal_stops_remaining_orders_and_reruns(self):
        """任一拒絕後其餘買單保留未提交，重跑也不續送。"""
        self.broker.submit.side_effect = [{'state': 'accepted'}, {'state': 'rejected'}]
        submit_once(self.journal, self.report, self.broker)
        submit_once(self.journal, self.report, self.broker)
        self.assertEqual(self.broker.submit.call_count, 2)
        self.assertEqual(self.journal.status(self.report['date'], 3), 'planned')

    def test_timeout_and_started_marker_are_terminal_for_resubmission(self):
        """POST逾時或整個程序中斷，持久化開始標記禁止第二輪。"""
        self.broker.submit.side_effect = requests.Timeout()
        submit_once(self.journal, self.report, self.broker)
        self.journal.db.close()
        self.journal = Journal(self.root/'strategy.sqlite3')
        submit_once(self.journal, self.report, self.broker)
        self.assertEqual(self.broker.submit.call_count, 1)
        self.assertEqual(self.journal.status(self.report['date'], 1), 'unknown')
        self.journal.put(RUN_ID+':execution', {'state': 'started'})
        submit_once(self.journal, self.report, self.broker)
        self.assertEqual(self.broker.submit.call_count, 1)

    def test_nonempty_holdings_or_previous_orders_block(self):
        """未確認的非空持股及其他委託都不可另行建倉。"""
        self.broker.holdings.return_value = {'2330': 10}
        with self.assertRaises(RuntimeError):
            submit_once(self.journal, self.report, self.broker)
        self.broker.submit.assert_not_called()
        self.journal.initialize_orders(self.report)
        with self.assertRaises(RuntimeError):
            submit_once(self.journal, self.report, self.broker)
        self.broker.submit.assert_not_called()

    def test_adapter_limits_and_fake_post(self):
        """限期API只接受封存的10張買單，相同id不第二次POST。"""
        session = Mock()
        session.post.return_value.status_code = 200
        session.post.return_value.json.return_value = {'result': 'success'}
        broker = CatchupPlatform(self.report['plan']['orders'], session)
        order = self.report['plan']['orders'][0]
        with patch('src.catchup.check_date'), patch.dict(os.environ, {'account': 'fake', 'password': 'fake-password'}):
            self.assertEqual(broker.submit(order)['state'], 'accepted')
            with self.assertRaises(ValueError):
                broker.submit(order)
            with self.assertRaises(ValueError):
                broker.submit(dict(order, lots=20))
        self.assertEqual(session.post.call_count, 1)

    def test_archived_execution_notifies_once_and_no_repeat_orders(self):
        """執行後通知含歷史訊號與限價日；重跑不重送Discord或買單。"""
        self.journal.save_run(self.report)
        args = argparse.Namespace(db=self.root/'market.sqlite3', preview=False, execute=True, send=True, json=False)
        session = Mock()
        session.post.return_value.status_code = 200
        session.post.return_value.json.return_value = {'id': 'discord-fake'}
        from src.notify import send as real_send
        with patch('src.catchup.CatchupPlatform', return_value=self.broker), patch('src.catchup.send', side_effect=lambda store, report, webhook: real_send(store, report, webhook, session)), patch.dict(os.environ, {'account': 'fake', 'DISCORD_WEBHOOK_URL': 'https://discord.com/api/webhooks/fake'}), patch('builtins.print'):
            execute(args, datetime(2026, 9, 22, tzinfo=TAIPEI))
            execute(args, datetime(2026, 9, 22, tzinfo=TAIPEI))
        self.assertEqual(self.broker.submit.call_count, 10)
        self.assertEqual(session.post.call_count, 1)
        content = session.post.call_args.kwargs['json']['content']
        self.assertIn('9/21實際收盤價', content)
        self.assertIn('已受理（未確認成交）', content)
        self.assertIn('2026-09-18', content)

    def test_unknown_still_notifies_and_exits_failed(self):
        """不明委託仍發送真實狀態摘要，不誤報全數完成。"""
        self.journal.save_run(self.report)
        self.broker.submit.return_value = {'state': 'unknown'}
        args = argparse.Namespace(db=self.root/'market.sqlite3', preview=False, execute=True, send=True, json=False)
        with patch('src.catchup.CatchupPlatform', return_value=self.broker), patch('src.catchup.send') as notify, patch.dict(os.environ, {'account': 'fake', 'DISCORD_WEBHOOK_URL': 'unused'}), patch('builtins.print'), self.assertRaises(RuntimeError):
            execute(args, datetime(2026, 9, 22, tzinfo=TAIPEI))
        self.assertEqual(self.broker.submit.call_count, 1)
        self.assertIn('結果不明', render(notify.call_args.args[1]))


if __name__ == '__main__':
    unittest.main()
