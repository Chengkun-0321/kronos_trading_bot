"""初始化、每日觀察與離線預覽 CLI；所有執行共用單一程序鎖。"""
import argparse
import gc
import sys
from contextlib import contextmanager, nullcontext
import fcntl
import json
import logging
import os
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .data import Calendar, DataSource, Http
from .notify import render, send
from .prediction import make_report, Predictor
from .storage import Store
from .training import ResearchPaused

ROOT = Path(__file__).resolve().parents[1]
TAIPEI = ZoneInfo('Asia/Taipei')
LOG = logging.getLogger(__name__)


@contextmanager
def process_lock(path: Path):
    """持有指定程序鎖並提供fd至區塊結束；忙碌時立即失敗，避免重疊下載或GPU工作。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('已有工作持有程序鎖') from None
        yield stream.fileno()


def execute(args, now=None):
    """執行CLI工作；日期採台北時間，研究入口不寫每日資料庫或送通知。"""
    now = now or datetime.now(TAIPEI)
    today = now.date()
    if args.command in ('prepare', 'train', 'evaluate'):
        from .research_data import prepare
        from .training import train
        from .evaluation import evaluate
        if args.command == 'prepare':
            return prepare(args.dataset, date.fromisoformat(args.cutoff), today)
        if args.command == 'train':
            return train(args.dataset, args.output, args.resume, args.smoke_steps,
                         getattr(args, 'batch_size', None), getattr(args, 'accumulation_steps', None),
                         getattr(args, 'precision', None))
        return evaluate(args.dataset, args.output, args.activate)
    if args.command == 'strategy-catchup':
        from .catchup import execute as execute_catchup
        return execute_catchup(args, now)
    if args.command in ('strategy', 'strategy-preview'):
        from .strategy import execute as execute_strategy
        return execute_strategy(args, now)
    store = Store(args.db)
    try:
        if args.command == 'preview':
            model_id = getattr(args, 'model', 'pretrained')
            row = store.db.execute('SELECT date FROM runs WHERE model_id=? ORDER BY date DESC LIMIT 1',
                                   (model_id,)).fetchone()
            day = args.date or (row[0] if row else None)
            report = store.run(day, model_id) if day else None
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
            if not args.no_send and not os.environ.get('DISCORD_WEBHOOK_URL'):
                raise ValueError('缺少 DISCORD_WEBHOOK_URL')
        elif day == today and now.hour < 18:
            day = calendar.shift(day, -1)
        if args.command == 'init':
            days = calendar.window(day)
            universe = source.universe(today)
            source.sync(universe, days)
            LOG.info('初始化完成：%s 個商品，%s 至 %s', len(universe), days[0], day)
            return
        models = [('pretrained', None)]
        failures = []
        active_path = ROOT / 'models/taiwan-active.json'
        if active_path.exists():
            try:
                active = json.loads(active_path.read_text())
                model_id = active['model_id']
                if not isinstance(model_id, str) or not model_id or model_id == 'pretrained' or not all(c.isalnum() or c in '-_' for c in model_id):
                    raise ValueError('微調模型識別碼不合法')
                models.append((model_id, active))
            except (ValueError, KeyError, TypeError):
                failures.append('微調模型登錄檔異常')
        universe = None
        for model_id, active in models:
            try:
                report = store.run(day.isoformat(), model_id)
                if report is None:
                    if universe is None:
                        days = calendar.window(day)
                        candidate_universe = source.universe(today)
                        source.sync(candidate_universe, days)
                        universe = candidate_universe
                    target = calendar.shift(day, 1)
                    if active is None:
                        report = make_report(store, universe, day, target)
                    else:
                        from .evaluation import model_digest
                        path = Path(active['checkpoint'])
                        if model_digest(path) != active['model_sha256']:
                            raise ValueError('微調模型權重與登錄版本不同')
                        report = make_report(store, universe, day, target,
                                             lambda: Predictor(model_path=path))
                        report['model_id'] = model_id
                        report['training_cutoff'] = active['cutoff']
                        report['model_sha256'] = active['model_sha256']
                    store.save_run(report)
                    reports = args.db.parent / 'reports'
                    reports.mkdir(exist_ok=True)
                    name = str(day) if model_id == 'pretrained' else f'{day}.{model_id}'
                    from .research_data import write_json
                    write_json(reports / f'{name}.json', report)
                print(render(report))
                if not args.no_send:
                    # 每封各自檢查時效；第二模型失敗不會重送第一封。
                    if datetime.now(TAIPEI).date().isoformat() >= report['target_date']:
                        raise RuntimeError('已跨入預測目標日，停止發送過期排行榜')
                    LOG.info('Discord %s: %s', model_id, send(store, report, os.environ['DISCORD_WEBHOOK_URL']))
            except Exception as exc:
                LOG.error('模型 %s 未完成：%s', model_id, type(exc).__name__)
                failures.append(model_id)
            finally:
                gc.collect()
                if 'torch' in sys.modules and sys.modules['torch'].cuda.is_available():
                    sys.modules['torch'].cuda.empty_cache()
        if failures:
            raise RuntimeError('部分模型未完成：' + ', '.join(failures))
    finally:
        store.db.close()


def main():
    """解析 CLI 並持有資料庫旁的程序鎖，失敗回傳非零退出碼供 systemd 追蹤。"""
    parser = argparse.ArgumentParser(description='Kronos 台股觀察與五日策略（正式委託停用）')
    parser.add_argument('--db', type=Path, default=ROOT / 'data/market.sqlite3')
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('init', 'daily', 'preview'):
        cmd = sub.add_parser(name)
        cmd.add_argument('--date', help='YYYY-MM-DD；daily 歷史日期限 --no-send')
        if name == 'daily':
            cmd.add_argument('--no-send', action='store_true')
        if name == 'preview':
            cmd.add_argument('--json', action='store_true')
            cmd.add_argument('--model', default='pretrained', help='pretrained或已登錄微調版本名')
    repair = sub.add_parser('repair')
    repair.add_argument('--symbol', required=True)
    repair.add_argument('--market', choices=['TWSE', 'TPEX'], required=True)
    repair.add_argument('--start', required=True)
    repair.add_argument('--end', required=True)
    repair.set_defaults(date=None)
    for name in ('prepare', 'train', 'evaluate'):
        cmd = sub.add_parser(name)
        cmd.add_argument('--dataset', type=Path, required=True, help='獨立研究快照目錄')
        if name == 'prepare':
            cmd.add_argument('--cutoff', required=True, help='固定截止交易日 YYYY-MM-DD，必須早於今日')
        else:
            cmd.add_argument('--output', type=Path, required=True, help='models/內獨立微調版本目錄')
        if name == 'train':
            cmd.add_argument('--resume', action='store_true')
            cmd.add_argument('--batch-size', type=int)
            cmd.add_argument('--accumulation-steps', type=int)
            cmd.add_argument('--precision', choices=['fp32', 'fp16'])
            cmd.add_argument('--smoke-steps', type=int, default=0, help='僅測試N個梯度樣本，不保存模型')
        if name == 'evaluate':
            cmd.add_argument('--activate', action='store_true', help='完整評估後登錄每日第二模型')
    from .strategy import add_parsers
    from .environment import load_env
    add_parsers(sub)
    from .catchup import add_parser
    add_parser(sub)
    load_env()
    args = parser.parse_args()
    args.db = args.db.resolve()
    research = args.command in ('prepare', 'train', 'evaluate')
    if research:
        args.dataset = args.dataset.resolve()
        if hasattr(args, 'output'):
            args.output = args.output.resolve()
            if (args.output.parent != ROOT / 'models'
                    or args.output.name in ('Kronos-small', 'Kronos-Tokenizer-base', 'pretrained')
                    or not all(c.isalnum() or c in '-_' for c in args.output.name)):
                parser.error('--output須為models/內獨立版本目錄，不得覆寫原模型')
        if getattr(args, 'smoke_steps', 0) < 0:
            parser.error('--smoke-steps不可為負值')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    lock = args.dataset / 'research.lock' if research else args.db.with_suffix('.lock')
    gpu = process_lock(ROOT / 'data/gpu.lock') if args.command in ('daily', 'train', 'evaluate', 'strategy', 'strategy-catchup') and not getattr(args, 'preview', False) else nullcontext()
    strategy_lock = (process_lock(args.db.parent / 'strategy-run.lock')
                     if args.command in ('strategy', 'strategy-preview', 'strategy-catchup') else nullcontext())
    try:
        with process_lock(lock), strategy_lock, gpu:
            execute(args)
    except ResearchPaused as exc:
        LOG.info('%s', exc)
        raise SystemExit(75) from None
    except (ValueError, RuntimeError) as exc:
        LOG.error('%s', exc)
        raise SystemExit(1) from None
    except Exception as exc:
        LOG.error('執行失敗：%s（未輸出可能含機密的例外原文）', type(exc).__name__)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
