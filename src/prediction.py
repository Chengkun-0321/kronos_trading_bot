"""以本機 Kronos 預訓練權重產生全市場數值預測及正報酬排名。"""
import hashlib
import logging
import random
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

COLS = ['open', 'high', 'low', 'close', 'volume', 'amount']
ROOT = Path(__file__).resolve().parents[1]
LOG = logging.getLogger(__name__)


class DataQualityError(ValueError):
    """可安全寫入報告的資料品質錯誤，不含上游例外或憑證。"""


def validate(frame: pd.DataFrame, day: str, lookback: int = 120):
    """驗證固定視窗；異常不補零或刪行，以免把停牌／缺漏當作正常行情。"""
    if len(frame) != lookback:
        raise DataQualityError(f'有效歷史不足 {lookback} 根')
    if frame.iloc[-1]['date'] != day:
        raise DataQualityError('最新日K過期或當日無交易')
    values = frame[COLS].to_numpy(dtype=float)
    if not np.isfinite(values).all():
        raise DataQualityError('行情存在缺值或非有限數字')
    if (frame[['open', 'high', 'low', 'close']] <= 0).any().any():
        raise DataQualityError('價格必須為正值')
    if (frame[['volume', 'amount']] <= 0).any().any():
        raise DataQualityError('成交量／金額為零或負值')
    if ((frame.high < frame[['open', 'close', 'low']].max(axis=1)) |
            (frame.low > frame[['open', 'close', 'high']].min(axis=1))).any():
        raise DataQualityError('OHLC 高低價矛盾')


def rank(predictions: list[dict]) -> list[dict]:
    """按預測日內報酬選正值前二十，同分以字串代號排序。"""
    return sorted((p for p in predictions if p['score'] > 0),
                  key=lambda p: (-p['score'], p['symbol']))[:20]


class Predictor:
    """延遲載入 GPU 模型，讓初始化與預覽不需要 CUDA。"""

    def __init__(self, device: str | None = None):
        """device 可指定 cpu/cuda:0；省略時使用 Kronos 的裝置偵測。"""
        sys.path.insert(0, str(ROOT / 'third_party/Kronos'))
        import torch
        from model import Kronos, KronosTokenizer, KronosPredictor
        model = Kronos.from_pretrained(str(ROOT / 'models/Kronos-small'), local_files_only=True)
        tokenizer = KronosTokenizer.from_pretrained(str(ROOT / 'models/Kronos-Tokenizer-base'), local_files_only=True)
        model.eval()
        tokenizer.eval()
        self.torch = torch
        self.engine = KronosPredictor(model, tokenizer, device=device, max_context=512)

    def predict(self, symbol: str, frame: pd.DataFrame, target: date) -> dict:
        """依代號及目標日固定亂數種子，單次抽樣下一交易日完整 OHLCVA。"""
        seed = int.from_bytes(hashlib.sha256(f'42:{symbol}:{target}'.encode()).digest()[:4], 'big')
        random.seed(seed)
        np.random.seed(seed)
        self.torch.manual_seed(seed)
        if self.torch.cuda.is_available():
            self.torch.cuda.manual_seed_all(seed)
        with self.torch.inference_mode():
            result = self.engine.predict(frame[COLS], pd.to_datetime(frame['date']),
                                         pd.Series(pd.to_datetime([target])), pred_len=1,
                                         T=1.0, top_p=0.9, top_k=0, sample_count=1, verbose=False)
        return {key: float(result.iloc[0][key]) for key in COLS}


def make_report(store, universe: dict, day: date, target: date, predictor_factory=Predictor) -> dict:
    """逐檔產生報告；資料或單股推論失敗留原因，模型無法載入則整次失敗。"""
    predictions, skipped = [], []
    predictor = None
    for index, (symbol, info) in enumerate(sorted(universe.items()), 1):
        frame = store.history(symbol, day.isoformat())
        try:
            if frame.empty:
                raise DataQualityError('無上市／上櫃標準日K（含興櫃或未支援商品）')
            validate(frame, day.isoformat())
        except ValueError as exc:
            skipped.append({'symbol': symbol, 'reason': str(exc)})
            continue
        if predictor is None:
            predictor = predictor_factory()
        try:
            predicted = predictor.predict(symbol, frame, target)
            check = pd.DataFrame([dict(predicted, date=target.isoformat())])
            validate(check, target.isoformat(), lookback=1)
            score = predicted['close'] / predicted['open'] - 1
            predictions.append({'symbol': symbol, 'name': info['name'],
                                'market': frame.iloc[-1]['market'], 'score': score,
                                'last_close': float(frame.iloc[-1]['close']),
                                'input_start': frame.iloc[0]['date'], 'input_end': day.isoformat(),
                                'prediction': predicted})
        except Exception as exc:
            # 不包含 exception 原文，避免第三方套件在錯誤中輸出憑證或整份資料。
            reason = str(exc) if isinstance(exc, DataQualityError) else type(exc).__name__
            skipped.append({'symbol': symbol, 'reason': f'預測失敗／輸出異常：{reason}'})
        if index % 100 == 0:
            LOG.info('預測進度 %s/%s', index, len(universe))
    return {'date': day.isoformat(), 'target_date': target.isoformat(),
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'model': 'Kronos-small', 'tokenizer': 'Kronos-Tokenizer-base',
            'lookback': 120, 'sample_count': 1, 'seed_scheme': 'sha256(42:symbol:target)',
            'universe_count': len(universe), 'predictions': predictions,
            'skipped': skipped, 'top20': rank(predictions)}
