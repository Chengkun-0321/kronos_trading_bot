"""交易所日行情與交易日曆；平台只查清單，不呼叫 stock_type 或下單 API。"""
import json
import logging
import math
import random
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import requests

from .storage import Store

LOG = logging.getLogger(__name__)
PLATFORM = 'https://ciot.imis.ncku.edu.tw/sim_stock/trading_api/stock_list'
DAILY = {
    'TWSE': 'https://www.twse.com.tw/exchangeReport/MI_INDEX',
    'TPEX': 'https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes',
}


class Http:
    """序列化低速 GET，對暫時性失敗做有上限的指數退避。"""

    def __init__(self, delay: float = 2.0):
        """delay 是相鄰 GET 開始前的最小間隔秒數。"""
        self.session = requests.Session()
        self.delay = delay
        self.last = 0.0

    def get(self, url: str, params=None):
        """回傳 JSON；最多嘗試五次，永久 HTTP 錯誤不重試。"""
        for attempt in range(5):
            time.sleep(max(0, self.delay - (time.monotonic() - self.last)))
            self.last = time.monotonic()
            retry_after = 0
            try:
                response = self.session.get(url, params=params, timeout=(10, 40))
                if response.status_code == 429 or response.status_code >= 500:
                    try:
                        retry_after = float(response.headers.get('Retry-After', 0))
                    except ValueError:
                        pass
                    raise requests.ConnectionError('temporary upstream failure')
                response.raise_for_status()
                return response.json()
            except (requests.ConnectionError, requests.Timeout, requests.exceptions.JSONDecodeError):
                if attempt == 4:
                    raise RuntimeError('行情 GET 重試五次仍失敗') from None
                if retry_after > 60:
                    raise RuntimeError('上游限流等待超過60秒，稍後重跑') from None
                time.sleep(min(60, max(retry_after, 2 ** attempt + random.random())))


def parse_date(value: str) -> date:
    """將西元或民國的緊湊／分隔日期轉為 date，拒絕未知格式。"""
    value = str(value).strip()
    if re.fullmatch(r'\d{7,8}', value):
        year, month, day = int(value[:-4]), int(value[-4:-2]), int(value[-2:])
    else:
        parts = re.split(r'[-/]', value)
        if len(parts) != 3:
            raise ValueError('未知行情日期格式')
        year, month, day = map(int, parts)
    return date(year + 1911 if year < 1911 else year, month, day)


def number(value):
    """將交易所數字轉 float；缺值或無限值回傳 None 供品質檢查。"""
    try:
        result = float(str(value).replace(',', '').strip())
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def parse_daily(payload: dict, market: str, day: date) -> list[dict]:
    """解析指定市場完整日報；日期或欄位不符時拒絕寫入快取。"""
    if str(payload.get('stat', '')).lower() != 'ok':
        raise ValueError(f'{market} {day} 尚無有效日報')
    if parse_date(payload.get('date', '')) != day:
        raise ValueError(f'{market} 回傳日期不符')
    mapping = ({'symbol': '證券代號', 'name': '證券名稱', 'open': '開盤價', 'high': '最高價',
                'low': '最低價', 'close': '收盤價', 'volume': '成交股數', 'amount': '成交金額'}
               if market == 'TWSE' else
               {'symbol': '代號', 'name': '名稱', 'open': '開盤', 'high': '最高',
                'low': '最低', 'close': '收盤', 'volume': '成交股數', 'amount': '成交金額(元)'})
    rows = []
    for table in payload.get('tables', []):
        fields = table.get('fields') or []
        if mapping['symbol'] not in fields:
            continue
        if not set(mapping.values()).issubset(fields):
            raise ValueError(f'{market} 日報欄位改變')
        for values in table.get('data', []):
            if len(values) != len(fields):
                raise ValueError(f'{market} 日報列長度不符')
            raw = dict(zip(fields, values))
            row = {key: str(raw[field]).strip() if key in ('symbol', 'name') else number(raw[field])
                   for key, field in mapping.items()}
            rows.append(dict(row, date=day.isoformat(), market=market))
    if not rows:
        raise ValueError(f'{market} 日報沒有商品資料')
    return rows


class Calendar:
    """官方年度開休市表每日更新；無法確認年份時不猜測下一交易日。"""

    def __init__(self, store: Store, http: Http, today: date):
        """today 為台北日期，用於日曆快取更新頻率。"""
        self.store, self.http, self.today = store, http, today
        self.years = {}
        path = Path(__file__).resolve().parents[1] / 'config/market_closures.json'
        overrides = json.loads(path.read_text(encoding='utf-8'))
        self.closures = {date.fromisoformat(day) for day in overrides}

    def holidays(self, year: int) -> set[date]:
        """回傳年度休市日期；保留最後／開始交易日為開市日。"""
        if year not in self.years:
            key = f'calendar:{year}:{self.today}'
            raw = self.store.get(key)
            if raw is None:
                raw = self.http.get('https://www.twse.com.tw/holidaySchedule/holidaySchedule',
                                    {'response': 'json', 'queryYear': year - 1911})
                if str(raw.get('stat', '')).lower() != 'ok' or not raw.get('data'):
                    raise ValueError(f'{year} 交易日曆尚未提供')
                if any(parse_date(row[0]).year != year for row in raw['data']):
                    raise ValueError('交易日曆年份不符')
                self.store.put(key, raw)
            self.years[year] = {parse_date(row[0]) for row in raw['data']
                                if not any(word in row[1] for word in ('開始交易', '最後交易'))}
        return self.years[year]

    def is_session(self, day: date) -> bool:
        """依台灣週末及官方休市表判斷交易日。"""
        return day.weekday() < 5 and day not in self.closures and day not in self.holidays(day.year)

    def shift(self, day: date, direction: int) -> date:
        """取得 day 之前／之後的交易日，direction 必須為 -1 或 1。"""
        if direction not in (-1, 1):
            raise ValueError('direction 必須是 -1 或 1')
        for _ in range(370):
            day += timedelta(days=direction)
            if self.is_session(day):
                return day
        raise ValueError('找不到交易日')

    def window(self, day: date, count: int = 120) -> list[date]:
        """產生截至 day 的 count 個交易日，包含 day（若當日開市）。"""
        result = []
        if not self.is_session(day):
            day = self.shift(day, -1)
        while len(result) < count:
            result.append(day)
            if len(result) < count:
                day = self.shift(day, -1)
        return sorted(result)


class DataSource:
    """批次補抓全市場缺日；以平台清單過濾後才落地，保留 ETF 所屬市場。"""

    def __init__(self, store: Store, http: Http):
        """使用共同 store、http 保持快取與節流一致。"""
        self.store, self.http = store, http

    def universe(self, today: date) -> dict:
        """每日最多一次成功取得平台清單；不硬編碼商品數或代號。"""
        key = f'universe:{today}'
        result = self.store.get(key)
        if result is None:
            result = self.http.get(PLATFORM)
            if not isinstance(result, dict) or not result or any(
                not isinstance(v, dict) or 'name' not in v for v in result.values()
            ):
                raise ValueError('平台清單格式不符')
            self.store.put(key, result)
        return result

    def sync(self, universe: dict, days: list[date]):
        """以每市場每日一個請求補齊缺口；已完成日期不重抓。"""
        # 商品新增後須重新取得舊日報，否則過去被清單濾掉的行情無法補入。
        old = set(self.store.get('known_symbols') or [])
        if set(universe) - old:
            with self.store.db:
                self.store.db.execute('DELETE FROM downloads')
        self.store.put('known_symbols', sorted(universe))
        for day in days:
            for market, url in DAILY.items():
                if self.store.downloaded(market, day.isoformat()):
                    continue
                params = {'response': 'json', 'date': day.strftime('%Y%m%d'), 'type': 'ALLBUT0999'}
                if market == 'TPEX':
                    params.update(date=day.strftime('%Y/%m/%d'), type='EW', id='')
                rows = parse_daily(self.http.get(url, params), market, day)
                self.store.save_bars([r for r in rows if r['symbol'] in universe], market, day.isoformat())
                LOG.info('已快取 %s %s', market, day)

    def repair_symbol(self, symbol: str, market: str, start: date, end: date):
        """手動補抓單檔日K，直接呼叫既有 fetcher，絕不查平台市場分類。"""
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'stock_final_project_for_class-main'))
        from stock_api.fetchers import get_twse_stock_data, get_tpex_stock_data
        fetch = {'TWSE': get_twse_stock_data, 'TPEX': get_tpex_stock_data}[market]
        frame = fetch(symbol, start.isoformat(), end.isoformat()).rename(
            columns={'capacity': 'volume', 'turnover': 'amount', 'stock_code_id': 'symbol'})
        # 單檔修補不可設定「全市場已下載」標記。
        with self.store.db:
            for _, row in frame.iterrows():
                values = [symbol, row['date'].date().isoformat(), market, symbol]
                values += [number(row[k]) for k in ('open', 'high', 'low', 'close', 'volume', 'amount')]
                self.store.db.execute('INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?,?,?)', values)
