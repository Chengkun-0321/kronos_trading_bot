"""在保留測試集比較兩模型；逐筆快取可續跑，不接交易或通知API。"""
import gc
import json
import logging
import time
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

from .prediction import COLS, Predictor, validate
from .research_data import Samples, digest, write_json
from .training import ROOT, schedule_window, ResearchPaused

LOG = logging.getLogger(__name__)
BATCH_SIZE = 128
PROTOCOL = "fp32-batch128-per-sample-v1"


def model_digest(folder: Path) -> str:
    """以模型設定及权重內容識別版本，拒絕不完整模型目錄。"""
    import hashlib
    files = sorted([*folder.glob('*.safetensors'), *folder.glob('*.bin')])
    if not files or not (folder / 'config.json').exists():
        raise ValueError('模型目錄缺少設定或權重')
    return hashlib.sha256(''.join(digest(p) for p in [folder / 'config.json', *files]).encode()).hexdigest()


def summarize(rows: list[dict]) -> dict:
    """依目標日計算前20勝率、等權日內報酬及全池Spearman；空值保留None。"""
    daily = []
    frame = pd.DataFrame(rows)
    if frame.empty:
        return dict(days=[], mean_return=None, mean_win_rate=None, mean_rank_ic=None)
    for day, group in frame.groupby('target'):
        top = group[group.score > 0].sort_values(['score', 'symbol'], ascending=[False, True]).head(20)
        correlation = (group.score.rank().corr(group.actual.rank())
                       if group.score.nunique() > 1 and group.actual.nunique() > 1 else float('nan'))
        daily.append(dict(target=day, evaluated=len(group), selected=len(top),
                          win_rate=float((top.actual > 0).mean()) if len(top) else None,
                          mean_return=float(top.actual.mean()) if len(top) else None,
                          universe_return=float(group.actual.mean()),
                          rank_ic=float(correlation) if np.isfinite(correlation) else None))
    def average(key):
        """按日等權平均已定義指標，不把缺少候選當成零收益。"""
        values = [d[key] for d in daily if d[key] is not None]
        return float(np.mean(values)) if values else None
    return dict(days=daily, mean_return=average('mean_return'), mean_win_rate=average('win_rate'),
                mean_rank_ic=average('rank_ic'))


def evaluate(folder: Path, output: Path, activate=False):
    """比較原始及output訓練版本；activate僅在完整技術驗證後原子登錄每日模型。"""
    training = json.loads((output / 'training.json').read_text())
    if training['dataset_sha256'] != digest(folder / 'manifest.json'):
        raise ValueError('評估資料不是原訓練保留快照')
    data = Samples(folder, 'test')
    models = [('pretrained', ROOT / 'models/Kronos-small'),
              (training['model_id'], Path(training['checkpoint']))]
    if models[1][0] == 'pretrained':
        raise ValueError('微調版本名稱不能使用pretrained')
    signatures = {key: model_digest(path) for key, path in models}
    cache = sqlite3.connect(output / 'evaluation-batch128-v1.sqlite3')
    cache.execute('CREATE TABLE IF NOT EXISTS outcomes(model TEXT, signature TEXT, symbol TEXT, target TEXT, '
                  'payload TEXT, PRIMARY KEY(model,signature,symbol,target))')
    summaries, results, invalid = {}, {}, {}
    try:
        for key, path in models:
            predictor = None
            try:
                completed = {(r[0], r[1]) for r in cache.execute(
                    'SELECT symbol,target FROM outcomes WHERE model=? AND signature=?', (key, signatures[key]))}
                started = time.monotonic()
                processed = 0
                for start in range(0, len(data.indices), BATCH_SIZE):
                    if schedule_window():
                        raise ResearchPaused('評估讓出每日排程；20:00後重跑evaluate續接快取')
                    items, rows = [], []
                    for sid, end in data.indices[start:start + BATCH_SIZE]:
                        info = data.manifest['symbols'][int(sid)]
                        series = data.series[int(sid)]
                        target = str(series['dates'][end])
                        if (info['symbol'], target) in completed:
                            continue
                        history = pd.DataFrame(series['values'][end-120:end], columns=COLS)
                        history['date'] = series['dates'][end-120:end]
                        items.append((info['symbol'], history, pd.Timestamp(target).date()))
                        rows.append(dict(symbol=info['symbol'], target=target,
                                         actual=float(series['values'][end, 3] / series['values'][end, 0] - 1)))
                    if not items:
                        continue
                    if predictor is None:
                        predictor = Predictor(model_path=path)
                    # 模型／OOM等批次故障中止續跑，不將整批錯記為資料無效。
                    predictions = predictor.predict_batch(items)
                    if len(predictions) != len(rows):
                        raise RuntimeError('批次推論結果數量不符')
                    for result, predicted in zip(rows, predictions):
                        try:
                            validate(pd.DataFrame([dict(predicted, date=result['target'])]), result['target'], 1)
                            result['score'] = predicted['close'] / predicted['open'] - 1
                            result['prediction'] = predicted
                        except ValueError as exc:
                            result['error'] = type(exc).__name__
                    with cache:
                        cache.executemany('INSERT INTO outcomes VALUES (?,?,?,?,?)',
                            [(key, signatures[key], r['symbol'], r['target'], json.dumps(r, allow_nan=False)) for r in rows])
                    processed += len(rows)
                    if start % (BATCH_SIZE * 20) == 0 or start + BATCH_SIZE >= len(data.indices):
                        LOG.info('評估 model=%s samples=%s/%s batch=128 samples_per_second=%.2f',
                                 key, len(completed) + processed, len(data.indices), processed / (time.monotonic() - started))
            finally:
                del predictor
                gc.collect()
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            all_rows = [json.loads(r[0]) for r in cache.execute('SELECT payload FROM outcomes WHERE model=? AND signature=?',
                                                               (key, signatures[key]))]
            results[key] = [r for r in all_rows if 'score' in r]
            invalid[key] = len(all_rows) - len(results[key])
            summaries[key] = summarize(results[key])
        common = set((r['symbol'], r['target']) for r in results['pretrained'])
        common &= set((r['symbol'], r['target']) for r in results[models[1][0]])
        comparison = {key: summarize([r for r in rows if (r['symbol'], r['target']) in common])
                      for key, rows in results.items()}
        report = dict(inference_protocol=PROTOCOL, batch_size=BATCH_SIZE, dataset_sha256=training['dataset_sha256'], model_sha256=signatures,
                      models=summaries, common_universe=comparison, invalid=invalid,
                      interpretation='開盤至收盤等權、未扣交易成本；不代表平台可成交收益。',
                      limitations=[data.manifest['bias'], '原始模型預訓練資料範圍不明，歷史測試不宣稱完全樣本外。'])
        write_json(output / 'evaluation.json', report)
        if activate:
            if not all(results.values()):
                raise ValueError('至少一個模型沒有有效測試結果，不能啟用')
            registration = dict(training, model_sha256=signatures[training['model_id']],
                                evaluation=str((output / 'evaluation.json').resolve()))
            write_json(ROOT / 'models/taiwan-active.json', registration)
        return report
    finally:
        cache.close()
