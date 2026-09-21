"""固定台股研究快照：官方整批行情、逐商品視窗及按目標日切分。"""
import hashlib
import json
import logging
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from .data import Calendar, DataSource, Http
from .prediction import COLS
from .storage import Store

LOG = logging.getLogger(__name__)


def write_json(path: Path, value):
    """原子保存可稽核 JSON；path 的父目錄須存在，不允許 NaN。"""
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temp.replace(path)


def digest(path: Path) -> str:
    """串流計算檔案 SHA256，避免模型與資料快照不一致。"""
    sha = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            sha.update(block)
    return sha.hexdigest()


def valid_rows(values: np.ndarray) -> np.ndarray:
    """六欄 OHLCVA 回傳逐列品質遮罩；缺值、零量及矛盾價格不補值。"""
    return (np.isfinite(values).all(axis=1) & (values > 0).all(axis=1)
            & (values[:, 1] >= values[:, [0, 2, 3]].max(axis=1))
            & (values[:, 2] <= values[:, [0, 1, 3]].min(axis=1)))


def build_snapshot(store: Store, universe: dict, sessions: list[str], folder: Path) -> dict:
    """將完整市場日期切分70/15/15；驗證／測試可借用過去輸入，標籤不跨界。"""
    if len(sessions) < 140:
        raise ValueError('至少需要140個市場交易日以建立三個非空集合')
    folder.mkdir(parents=True, exist_ok=True)
    boundaries = [sessions[int(len(sessions) * .70)], sessions[int(len(sessions) * .85)]]
    split_indices = {key: [] for key in ('train', 'validation', 'test')}
    symbols, hashes, skipped = [], {}, Counter()
    quality = {}
    session_index = {day: i for i, day in enumerate(sessions)}
    for symbol in sorted(universe):
        stats = Counter()
        quality[symbol] = stats
        frame = pd.read_sql_query('SELECT * FROM bars WHERE symbol=? AND date BETWEEN ? AND ? ORDER BY date',
                                 store.db, params=(symbol, sessions[0], sessions[-1]))
        if frame.empty:
            skipped['無標準日K商品'] += 1
            stats['無標準日K商品'] += 1
            continue
        values = frame[COLS].to_numpy(dtype=np.float64)
        days = frame.date.to_numpy(dtype='U10')
        valid = valid_rows(values)
        skipped['無效日K列'] += int((~valid).sum())
        stats['無效日K列'] = int((~valid).sum())
        locations = np.array([session_index.get(day, -10000) for day in days])
        sid = len(symbols)
        symbols.append({'symbol': symbol, 'name': universe[symbol]['name'],
                        'market': frame.market.iloc[-1]})
        stamp = pd.to_datetime(frame.date)
        stamps = np.column_stack([stamp.dt.minute, stamp.dt.hour, stamp.dt.weekday,
                                  stamp.dt.day, stamp.dt.month]).astype(np.float32)
        filename = f'{sid}.npz'
        np.savez_compressed(folder / filename, values=values, dates=days, stamps=stamps)
        hashes[filename] = digest(folder / filename)
        if len(days) < 121:
            skipped['不足121根商品'] += 1
            stats['不足121根商品'] += 1
        for end in range(120, len(days)):
            # 相鄰列未必是相鄰交易日；停牌或漏抓不得當成完整120日。
            if not valid[end-120:end+1].all():
                skipped['視窗含無效行情'] += 1
                stats['視窗含無效行情'] += 1
                continue
            if not (np.diff(locations[end-120:end+1]) == 1).all():
                skipped['視窗缺交易日'] += 1
                stats['視窗缺交易日'] += 1
                continue
            key = 'train' if days[end] < boundaries[0] else 'validation' if days[end] < boundaries[1] else 'test'
            split_indices[key].append((sid, end))
    for key, indices in split_indices.items():
        if not indices:
            raise ValueError(f'{key} 沒有有效樣本')
        np.save(folder / f'{key}.npy', np.asarray(indices, dtype=np.int32))
        hashes[f'{key}.npy'] = digest(folder / f'{key}.npy')
    manifest = dict(version=1, start=sessions[0], cutoff=sessions[-1], boundaries=boundaries,
                    sessions=sessions, features=COLS, lookback=120, symbols=symbols,
                    counts={k: len(v) for k, v in split_indices.items()}, skipped=dict(skipped),
                    quality_by_symbol=quality,
                    hashes=hashes, price_basis='官方未還原價；成交量股、金額元',
                    bias='採準備日平台清單，未涵蓋已下市商品，有存活偏差；除權息未校正。')
    write_json(folder / 'manifest.json', manifest)
    return manifest


def prepare(folder: Path, cutoff: date, today: date):
    """下載截止日以前近五年行情至獨立快取；重跑沿用固定清單及下載進度。"""
    if cutoff >= today:
        raise ValueError('研究截止日必須早於今日，避免未收盤行情')
    folder.mkdir(parents=True, exist_ok=True)
    if (folder / 'manifest.json').exists():
        manifest = json.loads((folder / 'manifest.json').read_text())
        if manifest['cutoff'] != cutoff.isoformat():
            raise ValueError('快照截止日不同，請改用新的研究目錄')
        LOG.info('沿用已完成研究快照 %s', folder)
        return
    store = Store(folder / 'market.sqlite3')
    try:
        http = Http()
        source = DataSource(store, http)
        request = store.get('research_request')
        if request is None:
            request = dict(cutoff=cutoff.isoformat(), universe=source.universe(today), universe_date=today.isoformat())
            store.put('research_request', request)
        if request['cutoff'] != cutoff.isoformat():
            raise ValueError('下載中的截止日不可變更')
        calendar = Calendar(store, http, today)
        if not calendar.is_session(cutoff):
            raise ValueError('截止日必須是交易日')
        start = (pd.Timestamp(cutoff) - pd.DateOffset(years=5)).date()
        days = []
        while start <= cutoff:
            if calendar.is_session(start):
                days.append(start)
            start += timedelta(days=1)
        source.sync(request['universe'], days)
        manifest = build_snapshot(store, request['universe'], [d.isoformat() for d in days], folder)
        manifest['universe_date'] = request['universe_date']
        write_json(folder / 'manifest.json', manifest)
        LOG.info('研究資料完成 %s', manifest['counts'])
    finally:
        store.db.close()


class Samples:
    """從固定快照取同商品121日；回傳標準化行情及時間特徵，不讀線上資料。"""

    def __init__(self, folder: Path, split: str):
        """split 限 train/validation/test；載入前核對所有檔案摘要。"""
        self.folder = folder
        self.manifest = json.loads((folder / 'manifest.json').read_text())
        if split not in ('train', 'validation', 'test'):
            raise ValueError('未知資料切分')
        for filename, expected in self.manifest['hashes'].items():
            if digest(folder / filename) != expected:
                raise ValueError(f'研究快照已變更：{filename}')
        self.indices = np.load(folder / f'{split}.npy', allow_pickle=False)
        self.series = {}
        for sid in range(len(self.manifest['symbols'])):
            with np.load(folder / f'{sid}.npz', allow_pickle=False) as data:
                self.series[sid] = {key: data[key] for key in data.files}

    def __len__(self):
        """回傳固定樣本數，不以隨機抽樣取代樣本索引。"""
        return len(self.indices)

    def __getitem__(self, index: int):
        """index 為樣本位置；回傳(121×6行情,121×5時間)，第121列僅為標籤。"""
        sid, end = self.indices[index]
        series = self.series[int(sid)]
        x = series['values'][end-120:end+1].copy()
        mean, std = x[:120].mean(axis=0), x[:120].std(axis=0)
        x = np.clip((x - mean) / (std + 1e-5), -5, 5).astype(np.float32)
        return x, series['stamps'][end-120:end+1].copy()
