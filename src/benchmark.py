"""隔離實測批次與精度；試跑不寫正式checkpoint，通過穩定性檢查才套用。"""
import argparse
import gc
import fcntl
import json
import logging
import os
from contextlib import nullcontext
import statistics
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

from .research_data import Samples, digest, write_json
from .training import (ROOT, ResearchPaused, schedule_window, training_config,
                       load_models, update_group, batch_tensors, batch_loss)

LOG = logging.getLogger(__name__)


def check_window():
    """每日保留時段拋出ResearchPaused；僅接受測速CLI明示的當日例外。"""
    # 當日例外隔日自動失效，不沿用研究程序的無期限override。
    from datetime import datetime
    from .training import TAIPEI
    now = datetime.now(TAIPEI)
    if os.environ.get('KRONOS_BENCHMARK_WINDOW_EXCEPTION_DATE') == now.date().isoformat():
        return
    if schedule_window(now):
        raise ResearchPaused('測速讓出17:45–20:00每日GPU時段')


def trial(folder, checkpoint, config, seconds=0, warmup=10, iterations=100):
    """回傳獨立CUDA試跑指標；checkpoint唯讀，seconds>0延長至指定穩定測試秒數。

    warmup/iterations以完整權重更新計數；包含CPU取樣、資料傳輸及梯度更新。
    呼叫端必須持有GPU鎖，測速權重與optimizer永不寫回正式模型。
    """
    check_window()
    torch, model, tokenizer = load_models()
    if not torch.cuda.is_available():
        raise ValueError('GPU測速需要CUDA')
    torch.manual_seed(42)
    model.cuda().train()
    tokenizer.cuda().float().eval()
    state = None
    if checkpoint:
        state = torch.load(checkpoint, map_location='cpu', weights_only=False)
        if state['signature'] != digest(folder / 'manifest.json'):
            raise ValueError('checkpoint資料摘要不同')
        model.load_state_dict(state['model'])
    train_data = Samples(folder, 'train')
    val_data = Samples(folder, 'validation')
    optimizer = torch.optim.AdamW(model.parameters(), lr=4e-5, betas=(.9, .95), weight_decay=.1)
    scaler = torch.amp.GradScaler('cuda', enabled=config['precision'] == 'fp16', init_scale=1024.)
    if state is not None:
        optimizer.load_state_dict(state['optimizer'])
        if config['precision'] == 'fp16' and state.get('scaler'):
            scaler.load_state_dict(state['scaler'])
        del state
    effective = config['batch_size'] * config['accumulation_steps']
    order = np.random.default_rng(42).permutation(len(train_data))
    free, total_memory = torch.cuda.mem_get_info()
    external_memory = total_memory - free - torch.cuda.memory_reserved()
    torch.cuda.reset_peak_memory_stats()
    seen = 0

    def step(index):
        """index為更新序號，回傳實際樣本數與loss，尾批依實際數量平均。"""
        check_window()
        start = (index * effective) % len(order)
        ids = order[start:min(start + effective, len(order))]
        loss = update_group(model, tokenizer, optimizer, scaler, train_data, ids, 'cuda', config)
        return len(ids), loss

    for i in range(warmup):
        step(i)
    torch.cuda.synchronize()
    started = time.perf_counter()
    i = 0
    while i < iterations or (seconds and time.perf_counter() - started < seconds):
        count, loss = step(i + warmup)
        seen += count
        i += 1
    torch.cuda.synchronize()
    elapsed = time.perf_counter() - started
    updates = i
    train_peak = torch.cuda.max_memory_allocated()
    reserved_peak = torch.cuda.max_memory_reserved()
    model.eval()
    val_order = np.random.default_rng(2026).permutation(len(val_data))
    size = config['batch_size']
    with torch.no_grad():
        for i in range(warmup):
            x, stamps = batch_tensors(val_data, val_order[i * size:(i + 1) * size], 'cuda')
            batch_loss(model, tokenizer, x, stamps, config['precision'])
        torch.cuda.synchronize()
        val_start = time.perf_counter()
        val_seen, val_loss = 0, 0.
        for i in range(iterations):
            check_window()
            start = (i * size) % len(val_order)
            ids = val_order[start:min(start + size, len(val_order))]
            x, stamps = batch_tensors(val_data, ids, 'cuda')
            value = batch_loss(model, tokenizer, x, stamps, config['precision'])
            if not torch.isfinite(value):
                raise RuntimeError('驗證損失非有限數值')
            val_loss += float(value) * len(ids)
            val_seen += len(ids)
        torch.cuda.synchronize()
        val_seconds = time.perf_counter() - val_start
    peak_reserved = max(reserved_peak, torch.cuda.max_memory_reserved())
    used_estimate = peak_reserved + external_memory
    result = dict(status='ok', config=config, samples=seen, updates=updates,
                  train_seconds=elapsed, samples_per_second=seen / elapsed,
                  validation_samples_per_second=val_seen / val_seconds,
                  validation_loss=val_loss / val_seen, final_loss=float(loss),
                  peak_allocated_bytes=max(train_peak, torch.cuda.max_memory_allocated()),
                  peak_reserved_bytes=peak_reserved, estimated_total_used_bytes=used_estimate,
                  total_memory_bytes=total_memory, memory_safe=used_estimate <= total_memory * .9,
                  scaler=scaler.state_dict(), stability_seconds=seconds,
                  device=torch.cuda.get_device_name(), torch_version=torch.__version__)
    result['epoch_seconds'] = len(train_data) / result['samples_per_second'] + len(val_data) / result['validation_samples_per_second']
    return result


def run_trial(folder, checkpoint, report_dir, config, name, gpu_lock_fd, seconds=0):
    """子程序繼承已持有的GPU鎖；同名JSON存在即續接，回傳單組測速紀錄。"""
    check_window()
    path = report_dir / f'{name}.json'
    if path.exists():
        return json.loads(path.read_text())
    command = [sys.executable, '-m', 'src.benchmark', 'trial', '--dataset', str(folder),
               '--report', str(path), '--batch-size', str(config['batch_size']),
               '--accumulation-steps', str(config['accumulation_steps']),
               '--precision', config['precision'], '--seconds', str(seconds),
               '--gpu-lock-fd', str(gpu_lock_fd)]
    if checkpoint:
        command += ['--checkpoint', str(checkpoint)]
    process = subprocess.run(command, cwd=ROOT, check=False, pass_fds=(gpu_lock_fd,))
    if process.returncode == 75:
        raise ResearchPaused('測速讓出每日排程')
    if not path.exists():
        raise RuntimeError(f'測速程序未產生報告，exit={process.returncode}')
    return json.loads(path.read_text())


def tune(folder, checkpoint, report_dir, apply_output=None):
    """搜尋並回傳穩定最快設定；apply_output指定時才原子套用並保存回退設定。

    Path參數指定同一資料快照、可信checkpoint及独立報告目錄；前三名重測，
    10分鐘穩定性與10%顯存餘裕皆通過才可套用。
    """
    from .main import process_lock
    report_dir.mkdir(parents=True, exist_ok=True)
    check_window()
    with process_lock(folder / 'research.lock'), process_lock(ROOT / 'data/gpu.lock') as gpu_lock_fd:
        identity = dict(dataset_sha256=digest(folder / 'manifest.json'),
                        checkpoint_sha256=digest(checkpoint) if checkpoint else None,
                        code_sha256={name: digest(ROOT / 'src' / name) for name in
                                     ('training.py', 'benchmark.py', 'research_data.py')})
        identity_path = report_dir / 'identity.json'
        if identity_path.exists() and json.loads(identity_path.read_text()) != identity:
            raise ValueError('測速報告來源不同，請使用新的report目錄')
        write_json(identity_path, identity)
        rows = []
        for precision in ('fp32', 'fp16'):
            size = 1
            while True:
                config = training_config(size, max(1, 32 // size), precision)
                row = run_trial(folder, checkpoint, report_dir, config, f'{precision}-{size}', gpu_lock_fd)
                rows.append(row)
                LOG.info('測速 %s', row)
                if row['status'] == 'oom':
                    break
                # 非OOM故障仍測其他批次；上限避免系統異常時無限倍增。
                if size >= 4096:
                    break
                size *= 2
        eligible = [r for r in rows if r['status'] == 'ok' and r['memory_safe']]
        if not eligible:
            raise RuntimeError('沒有穩定且保留10%顯存的設定')
        finalists = sorted(eligible, key=lambda r: r['samples_per_second'], reverse=True)[:3]
        ranked = []
        for row in finalists:
            config = row['config']
            repeats = [row] + [run_trial(folder, checkpoint, report_dir, config,
                                        f"{config['precision']}-{config['batch_size']}-repeat-{i}", gpu_lock_fd) for i in (1, 2)]
            if all(r['status'] == 'ok' and r['memory_safe'] for r in repeats):
                ranked.append((statistics.median(r['samples_per_second'] for r in repeats), row))
        for _, row in sorted(ranked, key=lambda item: item[0], reverse=True):
            config = row['config']
            stable = run_trial(folder, checkpoint, report_dir, config,
                               f"{config['precision']}-{config['batch_size']}-stable", gpu_lock_fd, seconds=600)
            if stable['status'] == 'ok' and stable['memory_safe']:
                break
        else:
            raise RuntimeError('候選設定未通過10分鐘穩定性檢查，保留原設定')
        baseline = next(r for r in rows if r['config'] == training_config())
        report = dict(identity=identity, trials=rows, winner=stable,
                      speedup=stable['samples_per_second'] / baseline['samples_per_second'],
                      baseline=baseline, applied=False,
                      window_exception_date=os.environ.get('KRONOS_BENCHMARK_WINDOW_EXCEPTION_DATE'))
        write_json(report_dir / 'summary.json', report)
        if apply_output:
            if not checkpoint or checkpoint.resolve() != (apply_output / 'resume.pt').resolve():
                raise ValueError('套用設定必須對應正式續跑checkpoint')
            if (apply_output / 'training.json').exists():
                raise ValueError('正式模型已完成，不能套用續跑設定')
            config_path = apply_output / 'training-config.json'
            fallback = json.loads(config_path.read_text()) if config_path.exists() else training_config()
            fallback_path = apply_output / 'training-fallback.json'
            # 同一份結果重複套用時，保留首次的回退設定。
            if fallback != stable['config'] or not fallback_path.exists():
                write_json(fallback_path, fallback)
            # 副作用：僅更換續跑設定；正式權重、optimizer與資料快照保持原檔。
            write_json(config_path, stable['config'])
            report['applied'] = True
            write_json(report_dir / 'summary.json', report)
        return report


def main():
    """提供trial/tune CLI；保留時段以75退出，正式權重不由測速程序保存。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['trial', 'tune'])
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--checkpoint', type=Path)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--apply-output', type=Path)
    parser.add_argument('--batch-size', type=int, default=1)
    parser.add_argument('--accumulation-steps', type=int, default=32)
    parser.add_argument('--precision', choices=['fp32', 'fp16'], default='fp32')
    parser.add_argument('--seconds', type=int, default=0)
    parser.add_argument('--allow-reserved-window-today', action='store_true',
                        help='明示允許今天於每日保留時段測速；隔日自動失效')
    parser.add_argument('--gpu-lock-fd', type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if args.allow_reserved_window_today:
        from datetime import datetime
        from .training import TAIPEI
        os.environ['KRONOS_BENCHMARK_WINDOW_EXCEPTION_DATE'] = datetime.now(TAIPEI).date().isoformat()
        LOG.warning('使用者明示允許今日保留時段測速；隔日自動失效')
    if args.mode == 'tune':
        try:
            tune(args.dataset, args.checkpoint, args.report, args.apply_output)
        except ResearchPaused as exc:
            LOG.info('%s', exc)
            raise SystemExit(75) from None
        return
    config = training_config(args.batch_size, args.accumulation_steps, args.precision)
    try:
        from .main import process_lock
        if args.gpu_lock_fd is not None:
            inherited = os.fstat(args.gpu_lock_fd)
            expected = (ROOT / 'data/gpu.lock').stat()
            if (inherited.st_dev, inherited.st_ino) != (expected.st_dev, expected.st_ino):
                raise ValueError('測速GPU鎖不符')
            fcntl.flock(args.gpu_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock = nullcontext() if args.gpu_lock_fd is not None else process_lock(ROOT / 'data/gpu.lock')
        with lock:
            result = trial(args.dataset, args.checkpoint, config, args.seconds)
    except ResearchPaused:
        raise SystemExit(75) from None
    except Exception as exc:
        import torch
        result = dict(status='oom' if isinstance(exc, torch.OutOfMemoryError) else 'failed',
                      config=config, error_type=type(exc).__name__, error=str(exc))
        gc.collect()
        torch.cuda.empty_cache()
    write_json(args.report, result)


if __name__ == '__main__':
    main()
