"""批次梯度、續跑及測速選擇的回歸測試；CPU執行、不碰正式權重。"""
import json
import os
from datetime import datetime
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import torch

from src.training import training_config, update_group, train, ResearchPaused


class Data:
    """33筆固定行情，讓測試同時涵蓋整組與尾批。"""
    manifest = dict(cutoff='2026-09-14', features=list('abcdef'))

    def __len__(self):
        """回傳33，確保有效批次32後仍有一筆。"""
        return 33

    def __getitem__(self, index):
        """索引0至32映射為121日行情與時間特徵，便於追蹤樣本權重。"""
        return (np.full((121, 6), index + 1, dtype=np.float32),
                np.zeros((121, 5), dtype=np.float32))


class PerformanceTests(unittest.TestCase):
    """以小型CPU模型驗證加速不能破壞梯度、續跑與回退邊界。"""
    def test_tail_microbatch_weights_match_full_batch(self):
        """不整除尾批的更新結果必須與一次處理整組一致。"""
        results = []
        for batch in (1, 2, 5):
            model = torch.nn.Linear(1, 1, bias=False)
            model.weight.data.fill_(.1)
            optimizer = torch.optim.SGD(model.parameters(), lr=.01)
            scaler = torch.amp.GradScaler('cuda', enabled=False)
            def loss(m, t, x, stamps):
                """使用不同樣本值的均方誤差，讓錯誤尾批權重可被偵測。"""
                return ((m.weight * x[:, 0, 0] - 1) ** 2).mean()
            with patch('src.training.next_day_loss', side_effect=loss):
                update_group(model, None, optimizer, scaler, Data(), range(5), 'cpu',
                             training_config(batch, 1))
            results.append(model.weight.detach().clone())
        for value in results[1:]:
            torch.testing.assert_close(value, results[0])

    def test_nonfinite_gradient_never_updates_weights(self):
        """有限loss但無限梯度也不能進入optimizer更新。"""
        model = torch.nn.Linear(1, 1, bias=False)
        model.weight.data.zero_()
        optimizer = torch.optim.SGD(model.parameters(), lr=.1)
        with patch('src.training.next_day_loss', side_effect=lambda m, *a: m.weight.sqrt().sum()):
            with self.assertRaises(RuntimeError):
                update_group(model, None, optimizer, torch.amp.GradScaler('cuda', enabled=False),
                             Data(), range(2), 'cpu', training_config(2, 1))
        self.assertEqual(model.weight.item(), 0.)

    def test_scaler_overflow_retries_without_updating_invalid_gradient(self):
        """模擬首批縮放後溢位，確認降倍率重試且只更新一次有效梯度。"""
        model = torch.nn.Linear(1, 1, bias=False)
        model.weight.data.fill_(1.)
        optimizer = torch.optim.SGD(model.parameters(), lr=.1)
        scaler = torch.amp.GradScaler('cpu', init_scale=32.)
        calls = []
        def loss(m, *args):
            """只讓首次梯度溢位，用參數值確認沒有執行無效步驟。"""
            calls.append(1)
            value = m.weight.sum()
            if len(calls) == 1:
                value.register_hook(lambda grad: torch.full_like(grad, float('inf')))
            return value
        with patch('src.training.next_day_loss', side_effect=loss):
            update_group(model, None, optimizer, scaler, Data(), range(2), 'cpu', training_config(2, 1))
        self.assertEqual(len(calls), 2)
        self.assertAlmostEqual(model.weight.item(), .9, places=5)
        self.assertEqual(scaler.get_scale(), 16.)

    def test_resume_preserves_optimizer_and_config_with_partial_group(self):
        """在整組邊界暂停再續跑，確認尾筆不漏算、不重複更新。"""
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / 'manifest.json').write_text('{}')
            output = folder / 'model'
            model = torch.nn.Linear(1, 1)
            model.save_pretrained = Mock()
            with patch('src.training.Samples', return_value=Data()), \
                 patch('src.training.load_models', return_value=(torch, model, torch.nn.Linear(1, 1))), \
                 patch('torch.cuda.is_available', return_value=False), \
                 patch('src.training.next_day_loss', side_effect=lambda m, *a: m.weight.sum() * 0 + 1):
                with patch('src.training.schedule_window', side_effect=[False, False, True]):
                    with self.assertRaises(ResearchPaused):
                        train(folder, output, batch_size=8, accumulation_steps=4)
                state = torch.load(output / 'resume.pt', weights_only=False)
                self.assertEqual(state['offset'], 32)
                self.assertEqual(state['config'], training_config(8, 4))
                self.assertIn('scaler', state)
                with patch('src.training.schedule_window', return_value=False):
                    result = train(folder, output, resume=True)
                self.assertEqual(result['batch_size'], 8)
                state = torch.load(output / 'resume.pt', weights_only=False)
                self.assertEqual(float(next(iter(state['optimizer']['state'].values()))['step']), 8.)
                self.assertEqual(len(state['config_history']), 1)

    def test_grad_scaler_state_roundtrip(self):
        """以CPU GradScaler驗證縮放器動態狀態可保存還原，不只保留精度字串。"""
        scaler = torch.amp.GradScaler('cpu', init_scale=32., growth_interval=1)
        model = torch.nn.Linear(1, 1)
        optimizer = torch.optim.SGD(model.parameters(), lr=.01)
        scaler.scale(model(torch.ones(1, 1)).sum()).backward()
        scaler.step(optimizer)
        scaler.update()
        restored = torch.amp.GradScaler('cpu', init_scale=1024.)
        restored.load_state_dict(scaler.state_dict())
        self.assertEqual(restored.get_scale(), 64.)
        self.assertEqual(restored.state_dict(), scaler.state_dict())

    def test_benchmark_window_exception_expires_after_today(self):
        """僅測速CLI指定的當日例外可繞過保留時段，舊日期與一般override無效。"""
        from src.benchmark import check_window
        from src.training import TAIPEI
        with patch('src.benchmark.schedule_window', return_value=True):
            with patch.dict(os.environ, {'KRONOS_BENCHMARK_WINDOW_EXCEPTION_DATE': '2000-01-01',
                                         'KRONOS_RESEARCH_OVERRIDE_GPU_WINDOW': '1'}):
                with self.assertRaises(ResearchPaused):
                    check_window()
            with patch.dict(os.environ, {'KRONOS_BENCHMARK_WINDOW_EXCEPTION_DATE':
                                         datetime.now(TAIPEI).date().isoformat()}):
                check_window()

    def test_invalid_config_rejected(self):
        """非正整數、布林值與未知精度不能成為訓練設定。"""
        for args in [(0, 32, 'fp32'), (1, -1, 'fp32'), (1, 1, 'bf16'), (True, 1, 'fp32')]:
            with self.assertRaises(ValueError):
                training_config(*args)

    def test_tuning_excludes_memory_pressure_and_requires_stability(self):
        """最快但顯存不足者不入選，重複套用仍保留最初回退設定。"""
        from src.benchmark import tune
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / 'model'
            output.mkdir()
            checkpoint = output / 'resume.pt'
            checkpoint.write_bytes(b'local')
            calls = []
            def trial(folder, checkpoint, report, config, name, fd, seconds=0):
                """模擬批次8爆顯存、批次4餘裕不足；其他批次速度正比大小。"""
                calls.append((config, seconds))
                size = config['batch_size']
                return dict(config=config, status='oom' if size >= 8 else 'ok',
                            memory_safe=size < 4, samples_per_second=float(size), epoch_seconds=100)
            with patch('src.benchmark.check_window'), patch('src.benchmark.digest', return_value='fixed'), \
                 patch('src.main.process_lock', side_effect=lambda p: nullcontext(1)), \
                 patch('src.benchmark.run_trial', side_effect=trial):
                result = tune(root, checkpoint, root / 'report', output)
                tune(root, checkpoint, root / 'report', output)
            self.assertEqual(result['winner']['config']['batch_size'], 2)
            self.assertTrue(result['applied'])
            self.assertEqual(calls[-1][1], 600)
            self.assertEqual(json.loads((output / 'training-fallback.json').read_text()), training_config())

    def test_pipeline_falls_back_once_then_stops(self):
        """加速與原設定皆失敗時，只允許一次回退，避免無限重跑。"""
        from src.research_run import run
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / 'model'
            output.mkdir()
            (output / 'training-config.json').write_text(json.dumps(training_config(64, 1, 'fp16')))
            (output / 'training-fallback.json').write_text(json.dumps(training_config()))
            (output / 'resume.pt').touch()
            results = [Mock(returncode=x) for x in (0, 0, 1, 1)]
            with patch('src.research_run.subprocess.run', side_effect=results) as process:
                with self.assertRaises(RuntimeError):
                    run(root / 'dataset', output, '2026-09-14', False)
            self.assertEqual(process.call_count, 4)
            self.assertEqual(json.loads((output / 'training-config.json').read_text()), training_config())
            self.assertTrue((output / 'training-fallback-used.json').exists())


if __name__ == '__main__':
    unittest.main()
