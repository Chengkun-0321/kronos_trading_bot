"""初始化、每日觀察與離線預覽 CLI；所有執行共用單一程序鎖。"""
import argparse
import fcntl
import json
import logging
import os
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .data import Calendar, DataSource, Http
from .notify import render, send
from .prediction import make_report
from .storage import Store

ROOT = Path(__file__).resolve().parents[1]
TAIPEI = ZoneInfo('Asia/Taipei')
LOG = logging.getLogger(__name__)


def execute(args, now=None):
    """執行解析後參數；now 可注入測試，所有日期以台北收盤界線為準。"""
    now = now or datetime.now(TAIPEI)
    today = now.date()
    store = Store(args.db)
    try:
        if args.command == 'preview':
            row = store.db.execute('SELECT date FROM runs ORDER BY date DESC LIMIT 1').fetchone()
            day = args.date or (row[0] if row else None)
            report = store.run(day) if day else None
            if report is None:
                raise ValueError('沒有已保存報告，請先執行 daily --no-send')
            print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else render(report))
            return
        http = Http()
        calendar = Calendar(store, http, today)
        source = DataSource(store, http)
        day = date.fromisoformat(args.date) if args.date else today
        if day > today:
            raise ValueError('不可讀取未來日期')
        if args.command == 'repair':
            end = date.fromisoformat(args.end)
            if end >= today:
                raise ValueError('手動修補限今日以前的歷史資料')
            start = date.fromisoformat(args.start)
            if start > end:
                raise ValueError('修補起日不可晚於迄日')
            source.repair_symbol(args.symbol, args.market, start, end)
            return
        if args.command == 'daily':
            if day == today and now.hour < 18:
                raise ValueError('每日流程須在台北時間18:00之後執行')
            if not calendar.is_session(day):
                LOG.info('%s 休市，不發排行榜', day)
                return
            if args.date and not args.no_send and day != today:
                raise ValueError('歷史驗證必須加 --no-send，避免發送過期通知')
            existing = store.run(day.isoformat())
            if existing:
                print(render(existing))
                if not args.no_send:
                    LOG.info('Discord: %s', send(store, existing, os.environ.get('DISCORD_WEBHOOK_URL', '')))
                return
            if not args.no_send and not os.environ.get('DISCORD_WEBHOOK_URL'):
                raise ValueError('缺少 DISCORD_WEBHOOK_URL')
        elif day == today and now.hour < 18:
            day = calendar.shift(day, -1)
        days = calendar.window(day)
        day = days[-1]
        universe = source.universe(today)
        source.sync(universe, days)
        if args.command == 'init':
            LOG.info('初始化完成：%s 個商品，%s 至 %s', len(universe), days[0], day)
            return
        target = calendar.shift(day, 1)
        report = make_report(store, universe, day, target)
        # 預測期間跨過目標日，表示初始化耗時過久；不得將事後結果當作事前預測送出。
        if not args.no_send and datetime.now(TAIPEI).date() >= target:
            raise RuntimeError('已跨入預測目標日，停止發送過期排行榜')
        store.save_run(report)
        reports = args.db.parent / 'reports'
        reports.mkdir(exist_ok=True)
        (reports / f'{day}.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(render(report))
        if not args.no_send:
            LOG.info('Discord: %s', send(store, report, os.environ['DISCORD_WEBHOOK_URL']))
    finally:
        store.db.close()


def main():
    """解析 CLI 並持有資料庫旁的程序鎖，失敗回傳非零退出碼供 systemd 追蹤。"""
    parser = argparse.ArgumentParser(description='Kronos 台股全市場觀察（無交易功能）')
    parser.add_argument('--db', type=Path, default=ROOT / 'data/market.sqlite3')
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('init', 'daily', 'preview'):
        cmd = sub.add_parser(name)
        cmd.add_argument('--date', help='YYYY-MM-DD；daily 歷史日期限 --no-send')
        if name == 'daily':
            cmd.add_argument('--no-send', action='store_true')
        if name == 'preview':
            cmd.add_argument('--json', action='store_true')
    repair = sub.add_parser('repair')
    repair.add_argument('--symbol', required=True)
    repair.add_argument('--market', choices=['TWSE', 'TPEX'], required=True)
    repair.add_argument('--start', required=True)
    repair.add_argument('--end', required=True)
    repair.set_defaults(date=None)
    args = parser.parse_args()
    args.db = args.db.resolve()
    args.db.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    with args.db.with_suffix('.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.exit(1, '已有初始化／預測程序執行中\n')
        try:
            execute(args)
        except (ValueError, RuntimeError) as exc:
            LOG.error('%s', exc)
            raise SystemExit(1) from None
        except Exception as exc:
            LOG.error('執行失敗：%s（未輸出可能含機密的例外原文）', type(exc).__name__)
            raise SystemExit(1) from None


if __name__ == '__main__':
    main()
