"""單GPU台股微調；固定Tokenizer、只學下一日，保留可續跑狀態與最佳權重。"""
import gc
import json
import logging
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from .research_data import Samples, digest, write_json

ROOT = Path(__file__).resolve().parents[1]
LOG = logging.getLogger(__name__)
TAIPEI = ZoneInfo('Asia/Taipei')


class ResearchPaused(RuntimeError):
    """研究工作讓出GPU時段，呼叫端可在時段外安全續跑。"""


def schedule_window(now=None) -> bool:
    """17:45至20:00保留給每日推論；回傳目前是否禁止研究GPU工作。"""
    if now is None and os.environ.get('KRONOS_RESEARCH_OVERRIDE_GPU_WINDOW') == '1':
        return False
    now = now or datetime.now(TAIPEI)
    return (17, 45) <= (now.hour, now.minute) < (20, 0)


def load_models():
    """只載入本機原始small及base tokenizer，回傳torch、模型及編碼器。"""
    sys.path.insert(0, str(ROOT / 'third_party/Kronos'))
    import torch
    from model import Kronos, KronosTokenizer
    model = Kronos.from_pretrained(str(ROOT / 'models/Kronos-small'), local_files_only=True)
    tokenizer = KronosTokenizer.from_pretrained(str(ROOT / 'models/Kronos-Tokenizer-base'), local_files_only=True)
    tokenizer.eval().requires_grad_(False)
    return torch, model, tokenizer


def next_day_loss(model, tokenizer, x, stamps, precision="fp32"):
    """121列樣本只對最後一日算交叉熵；模型輸入严格排除最後一列。"""
    import torch
    with torch.no_grad(), torch.autocast(device_type=x.device.type, enabled=False):
        # Tokenizer使用因果注意力；標籤另行編碼，輸入編碼明確只見120列。
        inputs = tokenizer.encode(x[:, :120], half=True)
        labels = tokenizer.encode(x, half=True)
    with torch.autocast(device_type=x.device.type, dtype=torch.float16, enabled=precision == "fp16"):
        logits = model(inputs[0], inputs[1], stamps[:, :120])
        loss, _, _ = model.head.compute_loss(logits[0][:, -1:], logits[1][:, -1:],
                                             labels[0][:, -1:], labels[1][:, -1:])
    return loss


def training_config(batch_size=1, accumulation_steps=32, precision="fp32"):
    """驗證正整數批次／累積步數與fp32/fp16精度，回傳可序列化的訓練設定。"""
    if type(batch_size) is not int or type(accumulation_steps) is not int or min(batch_size, accumulation_steps) < 1:
        raise ValueError('batch_size及accumulation_steps必須為正整數')
    if precision not in ('fp32', 'fp16'):
        raise ValueError('precision必須為fp32或fp16')
    return dict(batch_size=batch_size, accumulation_steps=accumulation_steps, precision=precision)


def batch_tensors(data, indices, device):
    """將非空索引集合堆疊為裝置上的(B,121,6)行情與(B,121,5)時間張量。"""
    import torch
    rows = [data[int(i)] for i in indices]
    return tuple(torch.from_numpy(np.stack(parts)).to(device) for parts in zip(*rows))


def batch_loss(model, tokenizer, x, stamps, precision):
    """依fp32/fp16計算單批平均loss；Tokenizer一律使用FP32。"""
    # 保留FP32呼叫介面，讓既有模型／測試替身仍可使用。
    if precision == 'fp32':
        return next_day_loss(model, tokenizer, x, stamps)
    return next_day_loss(model, tokenizer, x, stamps, precision)


def update_group(model, tokenizer, optimizer, scaler, data, indices, device, config):
    """非空indices完成一次權重更新並回傳平均loss；非有限梯度不得進入optimizer。"""
    import torch
    # AMP溢位時GradScaler會略過更新並降低倍率；同一組重試，不能漏掉樣本。
    for attempt in range(8):
        optimizer.zero_grad(set_to_none=True)
        total = torch.zeros((), device=device)
        size = config['batch_size']
        for start in range(0, len(indices), size):
            part = indices[start:start + size]
            x, stamps = batch_tensors(data, part, device)
            loss = batch_loss(model, tokenizer, x, stamps, config['precision'])
            if not torch.isfinite(loss):
                raise RuntimeError('訓練損失非有限數值')
            # 尾批可能不足batch_size，依樣本數加權才與整組平均梯度一致。
            weight = len(part) / len(indices)
            scaler.scale(loss * weight).backward()
            total += loss.detach() * weight
        scaler.unscale_(optimizer)
        norm = torch.nn.utils.clip_grad_norm_(model.parameters(), 3., error_if_nonfinite=False)
        if not torch.isfinite(norm):
            if not scaler.is_enabled():
                raise RuntimeError('訓練梯度非有限數值')
            # 直接降倍率且不呼叫optimizer，連有限梯度的範數溢位也不會誤更新。
            scaler.update(new_scale=scaler.get_scale() / 2)
            continue
        scaler.step(optimizer)
        scaler.update()
        return total
    raise RuntimeError('FP16梯度連續溢位，停止並保留最近有效checkpoint')


def train(folder: Path, output: Path, resume=False, smoke_steps=0,
          batch_size=None, accumulation_steps=None, precision=None):
    """微調至多10輪並回傳訓練摘要；smoke_steps>0只測指定樣本數、不保存權重。

    folder/output為本機快照及模型目錄。可省略的批次／精度參數優先於
    training-config.json、resume.pt與舊版預設；resume限本程式的可信checkpoint。
    """
    if schedule_window():
        raise ResearchPaused('17:45–20:00保留每日推論，請20:00後再訓練')
    if (output / 'training.json').exists() and not smoke_steps:
        raise ValueError('此版本已完成訓練；不覆寫權重，請使用新目錄')
    config_path = output / 'training-config.json'
    config = training_config()
    if config_path.exists():
        config.update(json.loads(config_path.read_text()))
    overrides = dict(batch_size=batch_size, accumulation_steps=accumulation_steps, precision=precision)
    config.update({k: v for k, v in overrides.items() if v is not None})
    config = training_config(**config)
    train_data = Samples(folder, 'train')
    val_data = Samples(folder, 'validation')
    signature = digest(folder / 'manifest.json')
    torch, model, tokenizer = load_models()
    device = 'cuda:0' if torch.cuda.is_available() else 'cpu'
    random.seed(42)
    np.random.seed(42)
    torch.manual_seed(42)
    if config['precision'] == 'fp16' and device == 'cpu':
        raise ValueError('FP16訓練需要CUDA')
    scaler = torch.amp.GradScaler('cuda', enabled=config['precision'] == 'fp16', init_scale=1024.)
    model.to(device)
    tokenizer.to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=4e-5, betas=(.9, .95), weight_decay=.1)
    output.mkdir(parents=True, exist_ok=True)
    checkpoint = output / 'resume.pt'
    history = []
    epoch, offset, best, stale, best_path = 0, 0, float('inf'), 0, None
    if resume:
        if not checkpoint.exists():
            raise ValueError('沒有可續跑checkpoint')
        # 此檔僅由本程式在本機產生，不載入外來pickle checkpoint。
        state = torch.load(checkpoint, map_location="cpu", weights_only=False)
        if state['signature'] != signature:
            raise ValueError('續跑資料快照不同')
        if not config_path.exists():
            config = training_config(**{**state.get('config', training_config()),
                                       **{k: v for k, v in overrides.items() if v is not None}})
            if config['precision'] == 'fp16' and device == 'cpu':
                raise ValueError('FP16訓練需要CUDA')
            scaler = torch.amp.GradScaler('cuda', enabled=config['precision'] == 'fp16', init_scale=1024.)
        if config['precision'] == 'fp16' and state.get('scaler'):
            scaler.load_state_dict(state['scaler'])
        history = state.get('config_history', [])
        model.load_state_dict(state['model'])
        optimizer.load_state_dict(state['optimizer'])
        epoch, offset, best, stale, best_path = (state[k] for k in ('epoch', 'offset', 'best', 'stale', 'best_path'))
        torch.set_rng_state(state['rng'].cpu())
        if device.startswith('cuda'):
            torch.cuda.set_rng_state_all([v.cpu() for v in state['cuda_rng']])
        del state
    elif checkpoint.exists() and not smoke_steps:
        raise ValueError('已有訓練進度，請使用 --resume')

    if not history or history[-1]['config'] != config:
        history.append(dict(epoch=epoch, offset=offset, config=config.copy()))
    LOG.info('訓練設定 %s；續跑epoch=%s offset=%s', config, epoch + 1, offset)
    effective_batch = config['batch_size'] * config['accumulation_steps']
    last_save = time.monotonic()

    def save_progress(next_epoch, next_offset):
        """在完整optimizer步驟後原子保存狀態；中斷不遺失已完成累積梯度。"""
        nonlocal last_save
        state = dict(config=config, config_history=history, scaler=scaler.state_dict(), signature=signature, model=model.state_dict(), optimizer=optimizer.state_dict(),
                     epoch=next_epoch, offset=next_offset, best=best, stale=stale, best_path=best_path,
                     rng=torch.get_rng_state(), cuda_rng=torch.cuda.get_rng_state_all() if device.startswith('cuda') else [])
        temp = checkpoint.with_suffix('.tmp')
        torch.save(state, temp)
        temp.replace(checkpoint)
        last_save = time.monotonic()

    steps = 0
    if device.startswith('cuda'):
        torch.cuda.reset_peak_memory_stats()
    completed_epochs = epoch
    for current in range(epoch, 10 if stale < 3 else epoch):
        model.train()
        epoch_started = time.monotonic()
        epoch_samples = 0
        order = np.random.default_rng(42 + current).permutation(len(train_data))
        for start in range(offset if current == epoch else 0, len(order), effective_batch):
            if schedule_window() or (output / 'pause.request').exists():
                if not smoke_steps:
                    save_progress(current, start)
                raise ResearchPaused('訓練已讓出每日排程時段；使用 train --resume 續跑')
            group = order[start:start + effective_batch]
            if smoke_steps:
                group = group[:max(1, smoke_steps - steps)]
            loss = update_group(model, tokenizer, optimizer, scaler, train_data, group, device, config)
            steps += len(group)
            epoch_samples += len(group)
            if not smoke_steps and time.monotonic() - last_save >= 300:
                save_progress(current, start + len(group))
            if smoke_steps and steps >= smoke_steps:
                result = dict(device=device, steps=steps, loss=float(loss.detach()),
                              peak_memory_bytes=torch.cuda.max_memory_allocated() if device.startswith('cuda') else None)
                LOG.info('訓練顯存測試 %s', result)
                return result
            if start // 3200 != (start + len(group)) // 3200 or steps == len(group):
                LOG.info('訓練 epoch=%s samples=%s/%s loss=%.4f samples_per_second=%.2f',
                         current+1, start+len(group), len(order), float(loss.detach()),
                         epoch_samples / (time.monotonic() - epoch_started))
        model.eval()
        validation_started = time.monotonic()
        total = 0.
        training_rng = torch.get_rng_state()
        training_cuda_rng = torch.cuda.get_rng_state_all() if device.startswith('cuda') else []
        # 固定驗證亂數，且不改變下一輪訓練的亂數序列。
        devices = [torch.cuda.current_device()] if device.startswith('cuda') else []
        with torch.random.fork_rng(devices=devices), torch.no_grad():
            torch.manual_seed(2026)
            for index in range(0, len(val_data), config['batch_size']):
                if schedule_window() or (output / 'pause.request').exists():
                    torch.set_rng_state(training_rng)
                    if device.startswith('cuda'):
                        torch.cuda.set_rng_state_all(training_cuda_rng)
                    save_progress(current, len(order))
                    raise ResearchPaused('驗證暫停讓出排程；使用 train --resume 重新驗證本輪')
                indices = range(index, min(index + config['batch_size'], len(val_data)))
                x, stamps = batch_tensors(val_data, indices, device)
                loss = batch_loss(model, tokenizer, x, stamps, config['precision'])
                if not torch.isfinite(loss):
                    raise RuntimeError('驗證損失非有限數值')
                total += float(loss) * len(indices)
        validation = total / len(val_data)
        if not np.isfinite(validation):
            raise RuntimeError('驗證損失非有限數值')
        LOG.info('epoch=%s validation_loss=%.6f validation_samples_per_second=%.2f',
                 current+1, validation, len(val_data) / (time.monotonic() - validation_started))
        if validation < best:
            best, stale = validation, 0
            best_path = f'epoch-{current+1}'
            model.save_pretrained(str(output / best_path))
        else:
            stale += 1
        completed_epochs = current + 1
        save_progress(completed_epochs, 0)
        if stale >= 3:
            break
    if best_path is None:
        raise RuntimeError('尚無有效最佳權重')
    result = dict(model_id=output.name, checkpoint=str((output / best_path).resolve()),
                  dataset=str(folder.resolve()), dataset_sha256=signature, cutoff=train_data.manifest['cutoff'],
                  best_validation_loss=best, epochs=completed_epochs,
                  tokenizer='Kronos-Tokenizer-base', lookback=120, features=train_data.manifest['features'],
                  learning_rate=4e-5, **config, config_history=history, seed=42,
                  peak_memory_bytes=torch.cuda.max_memory_allocated() if device.startswith('cuda') else None)
    write_json(output / 'training.json', result)
    del model, tokenizer, optimizer
    gc.collect()
    if device.startswith('cuda'):
        torch.cuda.empty_cache()
    return result
