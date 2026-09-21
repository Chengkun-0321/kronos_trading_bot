"""研究流程的時間洩漏、模型隔離與舊通知遷移回歸測試。"""
import argparse
import json
import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd

from src.main import execute, TAIPEI
from src.notify import send
from src.research_data import Samples, build_snapshot, write_json
from src.storage import Store
from src.training import next_day_loss, schedule_window
from src.evaluation import summarize


class ResearchTests(unittest.TestCase):
    """合成多商品日K驗證時序與故障邊界，不下載、不發送。"""

    def setUp(self):
        """建立180個交易日及獨立SQLite，避免碰觸正式紀錄。"""
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'market.sqlite3')
        self.days = pd.bdate_range('2025-01-01', periods=180).strftime('%Y-%m-%d').tolist()
        self.universe = {'A': {'name': '股票'}, '0050': {'name': 'ETF'}}
        for symbol in self.universe:
            rows = [dict(symbol=symbol, name=symbol, market='TWSE', date=day,
                         open=100.+i, high=110.+i, low=90.+i, close=102.+i,
                         volume=1000.+i, amount=100000.+i) for i, day in enumerate(self.days)]
            self.store.save_bars(rows, 'TWSE', self.days[-1])

    def tearDown(self):
        """關閉並移除每個案例的暫存資料。"""
        self.store.db.close()
        self.temp.cleanup()

    def test_target_dates_disjoint_and_etf_included(self):
        """相同交易日的所有商品共用切分；驗證可借過去輸入但不借未來標籤。"""
        manifest = build_snapshot(self.store, self.universe, self.days, self.root / 'snapshot')
        targets = []
        for split in ('train', 'validation', 'test'):
            data = Samples(self.root / 'snapshot', split)
            days = {str(data.series[int(sid)]['dates'][end]) for sid, end in data.indices}
            targets.append(days)
            self.assertEqual({int(sid) for sid, _ in data.indices}, {0, 1})
            x, stamps = data[0]
            self.assertEqual(x.shape, (121, 6))
            self.assertEqual(stamps.shape, (121, 5))
        self.assertLess(max(targets[0]), min(targets[1]))
        self.assertLess(max(targets[1]), min(targets[2]))
        self.assertEqual(min(targets[1]), manifest['boundaries'][0])

    def test_future_label_cannot_change_normalized_input(self):
        """改變第121列不應改變前120列統計量或模型輸入。"""
        build_snapshot(self.store, self.universe, self.days, self.root / 'snapshot')
        data = Samples(self.root / 'snapshot', 'train')
        before, _ = data[0]
        sid, end = data.indices[0]
        data.series[int(sid)]['values'][end] *= 100
        after, _ = data[0]
        np.testing.assert_array_equal(before[:120], after[:120])

    def test_missing_day_and_bad_ohlc_are_excluded(self):
        """停牌缺日不能壓縮為连续視窗；資料異常必須留略過原因。"""
        self.store.db.execute('DELETE FROM bars WHERE symbol=? AND date=?', ('A', self.days[100]))
        self.store.db.execute('UPDATE bars SET high=1 WHERE symbol=? AND date=?', ('0050', self.days[160]))
        self.store.db.commit()
        manifest = build_snapshot(self.store, self.universe, self.days, self.root / 'snapshot')
        self.assertGreater(manifest['skipped']['視窗缺交易日'], 0)
        self.assertGreater(manifest['skipped']['視窗含無效行情'], 0)

    def test_snapshot_tampering_rejected(self):
        """資料摘要阻止續訓時靜默使用變更後的歷史資料。"""
        build_snapshot(self.store, self.universe, self.days, self.root / 'snapshot')
        with (self.root / 'snapshot/0.npz').open('ab') as stream:
            stream.write(b'changed')
        with self.assertRaisesRegex(ValueError, '快照已變更'):
            Samples(self.root / 'snapshot', 'train')

    def test_legacy_delivery_states_survive_atomic_migration(self):
        """已送達與不明狀態都歸原版，重開資料庫不重置。"""
        path = self.root / 'legacy.sqlite3'
        db = sqlite3.connect(path)
        db.executescript('CREATE TABLE runs(date TEXT PRIMARY KEY,payload TEXT NOT NULL);'
                         'CREATE TABLE deliveries(date TEXT PRIMARY KEY,state TEXT NOT NULL,message_id TEXT);')
        for i, state in enumerate(('sent', 'sending', 'unknown')):
            db.execute('INSERT INTO deliveries VALUES (?,?,?)', (str(i), state, '123'))
        db.execute('INSERT INTO runs VALUES (?,?)', ('0', '{"date":"0"}'))
        db.commit()
        db.close()
        for _ in range(2):
            migrated = Store(path)
            self.assertEqual(migrated.run('0'), {'date': '0'})
            for i, state in enumerate(('sent', 'sending', 'unknown')):
                self.assertEqual(migrated.delivery(str(i)), (state, '123'))
                self.assertIsNone(migrated.delivery(str(i), 'taiwan-v1'))
            migrated.db.close()

    def test_two_notifications_and_unknown_isolated(self):
        """原版unknown不阻止微調版；微調版成功後重跑不重送。"""
        report = dict(date='2026-09-14', target_date='2026-09-15', universe_count=2, predictions=[], skipped=[])
        self.store.set_delivery(report['date'], 'unknown')
        client = Mock()
        client.post.return_value = Mock(status_code=200)
        client.post.return_value.json.return_value = {'id': 'new'}
        with self.assertRaises(RuntimeError):
            send(self.store, report, 'https://discord.com/api/webhooks/test/token', client)
        report['model_id'] = 'taiwan-v1'
        self.assertEqual(send(self.store, report, 'https://discord.com/api/webhooks/test/token', client), 'sent')
        self.assertEqual(send(self.store, report, 'https://discord.com/api/webhooks/test/token', client), 'already_sent')
        self.assertEqual(client.post.call_count, 1)
        self.assertEqual(self.store.delivery(report['date'])[0], 'unknown')

    @patch('src.main.send')
    @patch('src.evaluation.model_digest', return_value='hash')
    @patch('src.main.make_report')
    @patch('src.main.Calendar')
    @patch('src.main.DataSource')
    def test_first_model_failure_does_not_block_second(self, source, calendar, make, digest, notify):
        """原版失敗仍保存微調版，整體失敗退出供排程重試。"""
        (self.root / 'models').mkdir()
        write_json(self.root / 'models/taiwan-active.json', dict(model_id='taiwan-v1', checkpoint='unused',
                   model_sha256='hash', cutoff='2026-09-10'))
        calendar.return_value.is_session.return_value = True
        calendar.return_value.window.return_value = [date(2026, 9, 11)]
        calendar.return_value.shift.return_value = date(2026, 9, 14)
        source.return_value.universe.return_value = self.universe
        make.side_effect = [RuntimeError('load failed'), dict(date='2026-09-11', target_date='2026-09-14',
                            universe_count=2, predictions=[], skipped=[])]
        args = argparse.Namespace(db=self.root / 'market.sqlite3', command='daily', date='2026-09-11', no_send=True)
        with patch('src.main.ROOT', self.root), patch('builtins.print'), self.assertRaises(RuntimeError):
            execute(args, datetime(2026, 9, 14, 10, tzinfo=TAIPEI))
        self.assertIsNotNone(self.store.run('2026-09-11', 'taiwan-v1'))
        self.assertEqual(source.return_value.sync.call_count, 1)
        notify.assert_not_called()

    @patch('src.main.make_report')
    @patch('src.main.Calendar')
    @patch('src.main.DataSource')
    def test_failed_market_sync_never_reaches_model(self, source, calendar, make):
        """下載失敗後第二模型也必須重驗完整資料，不能使用半份行情。"""
        (self.root / 'models').mkdir()
        write_json(self.root / 'models/taiwan-active.json', dict(model_id='taiwan-v1'))
        calendar.return_value.is_session.return_value = True
        source.return_value.sync.side_effect = RuntimeError('download failed')
        args = argparse.Namespace(db=self.root / 'market.sqlite3', command='daily', date='2026-09-11', no_send=True)
        with patch('src.main.ROOT', self.root), self.assertRaises(RuntimeError):
            execute(args, datetime(2026, 9, 14, 10, tzinfo=TAIPEI))
        make.assert_not_called()
        self.assertEqual(source.return_value.sync.call_count, 2)

    def test_metrics_use_open_close_and_daily_equal_weight(self):
        """正值前20、不足照實；Spearman以排名計算，不依賴scipy。"""
        result = summarize([dict(target='2026-09-11', symbol='A', score=.2, actual=.1),
                            dict(target='2026-09-11', symbol='B', score=-.2, actual=-.1)])
        self.assertAlmostEqual(result['mean_return'], .1)
        self.assertEqual(result['mean_win_rate'], 1.)
        self.assertAlmostEqual(result['mean_rank_ic'], 1.)

    def test_daily_gpu_reservation_boundaries(self):
        """保留時段含17:45、不含20:00，避免訓練佔用18:00排程。"""
        for hour, minute, blocked in [(17, 44, False), (17, 45, True), (19, 59, True), (20, 0, False)]:
            self.assertEqual(schedule_window(datetime(2026, 9, 15, hour, minute, tzinfo=TAIPEI)), blocked)

    @patch('src.evaluation.schedule_window', return_value=False)
    @patch('src.evaluation.model_digest', return_value='fixed')
    @patch('src.evaluation.Predictor')
    def test_evaluation_cache_and_activation(self, predictor, digest_mock, paused):
        """完整保留集評估後才登錄；重跑沿用逐筆結果，不再次推論。"""
        from src.evaluation import evaluate
        from src.research_data import digest
        folder = self.root / 'snapshot'
        build_snapshot(self.store, self.universe, self.days, folder)
        output = self.root / 'models/taiwan-v1'
        output.mkdir(parents=True)
        write_json(output / 'training.json', dict(model_id='taiwan-v1', checkpoint=str(output / 'epoch-1'),
                   dataset_sha256=digest(folder / 'manifest.json'), cutoff=self.days[-1]))
        predictor.return_value.predict_batch.side_effect = lambda items: [dict(open=100., high=110., low=90., close=105., volume=1., amount=100.) for _ in items]
        with patch('src.evaluation.ROOT', self.root):
            report = evaluate(folder, output, activate=True)
            evaluate(folder, output, activate=True)
        self.assertEqual(predictor.call_count, 2)
        self.assertEqual(set(report['models']), {'pretrained','taiwan-v1'})
        active = json.loads((self.root / 'models/taiwan-active.json').read_text())
        self.assertEqual(active['model_id'], 'taiwan-v1')
        self.assertEqual(active['model_sha256'], 'fixed')

    @patch('src.research_run.subprocess.run')
    def test_pipeline_stops_on_real_error(self, process):
        """下載失敗不可繼續訓練或啟用；普通錯誤不自動反覆重試。"""
        from src.research_run import run
        process.return_value.returncode = 1
        with self.assertRaises(RuntimeError):
            run(self.root / 'pipeline', self.root / 'model', '2026-09-14', True)
        self.assertEqual(process.call_count, 1)
        state = json.loads((self.root / 'pipeline/pipeline.json').read_text())
        self.assertEqual(state['state'], 'failed')
        self.assertEqual(state['stage'], 'prepare')

    def test_training_early_stop_and_resume_state(self):
        """固定驗證損失於第四輪早停，尾批也完成optimizer步驟且保存續跑狀態。"""
        import torch
        from src.training import train
        dataset = Mock()
        dataset.manifest = dict(cutoff='2025-09-01', features=['open','high','low','close','volume','amount'])
        class TinyData:
            """33筆樣本使每輪涵蓋完整32筆及1筆尾批。"""
            manifest = dataset.manifest
            def __len__(self):
                """回傳33筆，驗證梯度累積尾批。"""
                return 33
            def __getitem__(self, index):
                """回傳固定形狀，模型損失由測試注入。"""
                return np.zeros((121,6),dtype=np.float32), np.zeros((121,5),dtype=np.float32)
        model = torch.nn.Linear(1,1)
        model.save_pretrained = Mock()
        folder = self.root / 'fake'
        folder.mkdir()
        (folder / 'manifest.json').write_text('{}')
        with patch('src.training.Samples', return_value=TinyData()), \
             patch('src.training.load_models', return_value=(torch, model, torch.nn.Linear(1,1))), \
             patch('src.training.schedule_window', return_value=False), \
             patch('torch.cuda.is_available', return_value=False), \
             patch('src.training.next_day_loss', side_effect=lambda m,k,x,s: m.weight.sum()*0+1):
            result = train(folder, self.root / 'trial')
        self.assertEqual(result['epochs'], 4)
        self.assertEqual(result['best_validation_loss'], 1.)
        model.save_pretrained.assert_called_once()
        state = torch.load(self.root / 'trial/resume.pt', weights_only=False)
        self.assertEqual(state['epoch'], 4)
        self.assertEqual(state['stale'], 3)
        self.assertEqual(float(next(iter(state['optimizer']['state'].values()))['step']), 8.)

    def test_model_input_excludes_target_row(self):
        """真實tensor驗證模型只見120列、損失只對第121列。"""
        import torch
        tokenizer = Mock()
        tokenizer.encode.side_effect = lambda x, half: [torch.zeros(x.shape[:2], dtype=torch.long)] * 2
        model = Mock()
        model.return_value = [torch.zeros(1, 120, 1024)] * 2
        model.head.compute_loss.return_value = (torch.tensor(1.), None, None)
        next_day_loss(model, tokenizer, torch.ones(1, 121, 6), torch.ones(1, 121, 5))
        self.assertEqual(model.call_args.args[0].shape, (1, 120))
        self.assertEqual(model.call_args.args[2].shape, (1, 120, 5))
        self.assertEqual(model.head.compute_loss.call_args.args[2].shape, (1, 1))


if __name__ == '__main__':
    unittest.main()
