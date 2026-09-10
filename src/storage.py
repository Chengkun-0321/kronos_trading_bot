"""以 SQLite 保存原始日 K、下載進度及可稽核的預測／通知狀態。"""
import json
import sqlite3
from pathlib import Path

import pandas as pd


class Store:
    """每次寫入即提交，讓長時間初始化可中斷續跑。"""

    def __init__(self, path: Path):
        """以本機 path 開啟資料庫並建立所需表格。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS cache(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS bars(
                symbol TEXT, date TEXT, market TEXT, name TEXT,
                open REAL, high REAL, low REAL, close REAL, volume REAL, amount REAL,
                PRIMARY KEY(symbol,date));
            CREATE TABLE IF NOT EXISTS downloads(
                market TEXT, date TEXT, PRIMARY KEY(market,date));
            CREATE TABLE IF NOT EXISTS runs(date TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS deliveries(
                date TEXT PRIMARY KEY, state TEXT NOT NULL, message_id TEXT);
        ''')

    def get(self, key: str):
        """讀取 JSON 快取；不存在回傳 None。"""
        row = self.db.execute('SELECT value FROM cache WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key: str, value):
        """將可 JSON 序列化的 value 原子寫入指定 key。"""
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO cache VALUES (?,?)',
                            (key, json.dumps(value, ensure_ascii=False, allow_nan=False)))

    def save_bars(self, rows: list[dict], market: str, day: str):
        """一起提交某市場日資料及完成標記，避免部分下載被當作成功。"""
        with self.db:
            self.db.executemany('''INSERT OR REPLACE INTO bars VALUES
                (:symbol,:date,:market,:name,:open,:high,:low,:close,:volume,:amount)''', rows)
            self.db.execute('INSERT OR IGNORE INTO downloads VALUES (?,?)', (market, day))

    def downloaded(self, market: str, day: str) -> bool:
        """確認指定市場日期是否已完整解析並提交。"""
        return self.db.execute('SELECT 1 FROM downloads WHERE market=? AND date=?',
                               (market, day)).fetchone() is not None

    def history(self, symbol: str, day: str, limit: int = 120) -> pd.DataFrame:
        """回傳截至 ISO day 的最後 limit 根 K 線，禁止讀取未來資料。"""
        return pd.read_sql_query('''SELECT * FROM (
            SELECT * FROM bars WHERE symbol=? AND date<=? ORDER BY date DESC LIMIT ?
            ) ORDER BY date''', self.db, params=(symbol, day, limit))

    def run(self, day: str):
        """回傳已封存的該日預測；沒有則回傳 None。"""
        row = self.db.execute('SELECT payload FROM runs WHERE date=?', (day,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_run(self, report: dict):
        """封存完整輸入日期、預測及略過原因；同日不覆寫既有報告。"""
        with self.db:
            self.db.execute('INSERT INTO runs VALUES (?,?)',
                            (report['date'], json.dumps(report, ensure_ascii=False, allow_nan=False)))

    def delivery(self, day: str):
        """取得通知狀態與 Discord 訊息 ID；無紀錄回傳 None。"""
        return self.db.execute('SELECT state,message_id FROM deliveries WHERE date=?', (day,)).fetchone()

    def set_delivery(self, day: str, state: str, message_id=None):
        """持久化送信狀態，讓程序意外終止後仍能阻止盲目重送。"""
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO deliveries VALUES (?,?,?)',
                            (day, state, message_id))
