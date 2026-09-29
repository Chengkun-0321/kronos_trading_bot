"""五日訊號、週調倉及可中斷的委託紀錄；正式平台目前封鎖。"""
import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .data import Calendar, DataSource, Http
from .prediction import DataQualityError, Predictor, validate
from .storage import Store
from .trading import Platform, require_trading_contract

STRATEGY = 'weekly-v1'
LIVE_STRATEGY = 'weekly-v1-live'
TAIPEI = ZoneInfo('Asia/Taipei')


def weekly_session(calendar, day):
    """依Calendar及date判定當週最後開市日，回傳bool；不等同五日間隔。"""
    if not calendar.is_session(day):
        return False
    sunday = day + timedelta(days=6 - day.weekday())
    return all(not calendar.is_session(day + timedelta(days=n))
               for n in range(1, (sunday - day).days + 1))


def check_holdings(holdings):
    """驗證模擬{代號:張數}並原樣回傳；實際平台格式不得猜測。"""
    if not isinstance(holdings, dict) or any(
        not isinstance(s, str) or not s.isalnum() or isinstance(q, bool)
        or not isinstance(q, (int, float)) or not math.isfinite(q) or q <= 0
        for s, q in holdings.items()
    ):
        raise ValueError('模擬持股須為 {代號: 正數張數}，零持股請省略')
    return holdings


def forecast(store, universe, calendar, day, predictor_factory=Predictor, horizon=5):
    """以資料日及全清單產生報告；horizon預設5，補跑可明示4，不改日常。"""
    if horizon not in (4, 5):
        raise ValueError("策略預測只支援4或5個交易日")
    targets = []
    target = day
    for _ in range(horizon):
        target = calendar.shift(target, 1)
        targets.append(target)
    expected = [d.isoformat() for d in calendar.window(day)]
    predictions, skipped = [], []
    predictor = None
    for index, (symbol, info) in enumerate(sorted(universe.items()), 1):
        if index % 100 == 0:
            import logging
            logging.getLogger(__name__).info("策略預測進度 %s/%s", index, len(universe))
        frame = store.history(symbol, day.isoformat())
        try:
            validate(frame, day.isoformat())
            if frame['date'].tolist() != expected:
                raise ValueError('120日視窗缺交易日')
        except ValueError as exc:
            skipped.append({'symbol': symbol, 'reason': str(exc)})
            continue
        if predictor is None:
            predictor = predictor_factory()
        try:
            path = predictor.predict_path(symbol, frame, targets)
            if len(path) != horizon:
                raise DataQualityError('預測日數不符')
            for bar, target in zip(path, targets):
                validate(pd.DataFrame([dict(bar, date=target.isoformat())]), target.isoformat(), 1)
            score = path[-1]['close'] / path[0]['open'] - 1
            if not math.isfinite(score):
                raise DataQualityError('預測分數非有限值')
            predictions.append(dict(symbol=symbol, name=info['name'], score=score,
                                    last_close=float(frame.iloc[-1]['close']), prediction=path))
        except Exception as exc:
            skipped.append({'symbol': symbol, 'reason': str(exc) if isinstance(exc, DataQualityError) else type(exc).__name__})
    predictions.sort(key=lambda p: (-p['score'], p['symbol']))
    for rank, row in enumerate(predictions, 1):
        row['rank'] = rank
    return dict(date=day.isoformat(), model_id=STRATEGY, model='pretrained', lookback=120,
                horizon=horizon, target_date=targets[0].isoformat(),
                target_dates=[t.isoformat() for t in targets], universe_count=len(universe),
                predictions=predictions, skipped=skipped)


def rebalance(predictions, holdings, blocked=()):
    """純函式：先必要賣出、補位，再按排名替換；缺訊號占用名額。"""
    check_holdings(holdings)
    rows = sorted(predictions, key=lambda p: (-p['score'], p['symbol']))
    if len({p['symbol'] for p in rows}) != len(rows) or any(
        not math.isfinite(p['score']) or not math.isfinite(p['last_close']) or p['last_close'] <= 0
        for p in rows
    ):
        raise ValueError('訊號重複或價格／分數無效')
    signals = {p['symbol']: dict(p, rank=i) for i, p in enumerate(rows, 1)}
    target, orders, warnings = dict(holdings), [], []
    blocked = set(blocked)

    def order(symbol, side, reason, dependency=None):
        """建立具穩定id的目標調整，dependency為須先受理的賣單id。"""
        record = dict(id=len(orders) + 1, symbol=symbol, side=side,
                      lots=holdings[symbol] if side == 'sell' else 10,
                      price=signals[symbol]['last_close'], reason=reason,
                      depends_on=dependency)
        orders.append(record)
        if side == 'sell':
            del target[symbol]
        else:
            target[symbol] = 10
        return record['id']

    for symbol in sorted(holdings):
        p = signals.get(symbol)
        if symbol in blocked:
            warnings.append(f'{symbol} 有未結委託，保留待核對')
        elif p is None:
            warnings.append(f'{symbol} 缺訊號，保留')
        elif p['rank'] > 20 or p['score'] <= 0:
            order(symbol, 'sell', '必要賣出')
    if len(target) > 10:
        warnings.append('既有持股超過10檔，停止新買入；不額外強制賣出')
    else:
        for p in rows[:10]:
            symbol = p['symbol']
            if p['score'] <= .01 or symbol in holdings or symbol in blocked:
                continue
            dependency = None
            if len(target) == 10:
                replaceable = [s for s in target if s in holdings and s in signals and s not in blocked]
                if not replaceable:
                    break
                worst = max(replaceable, key=lambda s: signals[s]['rank'])
                if signals[worst]['rank'] <= signals[symbol]['rank']:
                    continue
                dependency = order(worst, 'sell', '主動替換')
            order(symbol, 'buy', 'Top10補位' if dependency is None else '主動替換', dependency)
    # 全部賣單先提交；買單的原始id保持穩定，供中斷續跑辨識。
    orders.sort(key=lambda o: (o['side'] != 'sell', o['id']))
    return dict(holdings=holdings, target=target, orders=orders, warnings=warnings)


class Journal(Store):
    """獨立策略SQLite；通知沿用Store協定，委託狀態不混入每日資料庫。"""
    def __init__(self, path):
        """於Path建立策略庫及委託表，不修改每日報告。"""
        super().__init__(path)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS orders(
                date TEXT, id INTEGER, symbol TEXT, state TEXT NOT NULL,
                payload TEXT NOT NULL, response TEXT, PRIMARY KEY(date,id));
        ''')

    def unresolved(self):
        """回傳set[str]，包含已受理但尚無終態的代號以防跨週重買。"""
        return {r[0] for r in self.db.execute(
            "SELECT DISTINCT symbol FROM orders WHERE state IN ('sending','unknown','accepted')")}

    def initialize_orders(self, report):
        """將封存報告dict的訂單一次提交；既存id保留重跑狀態。"""
        with self.db:
            for order in report['plan']['orders']:
                saved = self.db.execute('SELECT payload FROM orders WHERE date=? AND id=?',
                                        (report['date'], order['id'])).fetchone()
                if saved and json.loads(saved[0]) != order:
                    raise ValueError('封存委託內容不符，禁止修改後重送')
                self.db.execute('INSERT OR IGNORE INTO orders VALUES (?,?,?,?,?,NULL)',
                                (report['date'], order['id'], order['symbol'], 'planned', json.dumps(order)))

    def status(self, day, order_id):
        """依ISO資料日及整數id讀取持久化state字串。"""
        return self.db.execute('SELECT state FROM orders WHERE date=? AND id=?', (day, order_id)).fetchone()[0]

    def transition(self, day, order_id, state, response=None):
        """提交state與去機密的JSON回應；POST前須先寫sending。"""
        with self.db:
            self.db.execute('UPDATE orders SET state=?,response=? WHERE date=? AND id=?',
                            (state, json.dumps(response, ensure_ascii=False, allow_nan=False), day, order_id))


def execute_orders(journal, report, broker):
    """broker 回傳標準化state及raw；無法辨識的結果必須為unknown。"""
    journal.initialize_orders(report)
    day, plan = report['date'], report['plan']
    # 任何一輪有不明POST結果即停止；受理單須先取得平台終態才能跨週交易。
    if journal.db.execute("SELECT 1 FROM orders WHERE state IN ('sending','unknown') "
                          "OR (date<>? AND state='accepted') LIMIT 1", (day,)).fetchone():
        raise RuntimeError('存在待核對委託，不自動重送或啟動下一輪')
    target = set(plan['holdings'])
    for order in plan['orders']:
        state = journal.status(day, order['id'])
        if state == 'accepted':
            if order['side'] == 'sell':
                target.discard(order['symbol'])
            else:
                target.add(order['symbol'])
        elif state == 'insufficient_funds':
            return 'insufficient_funds'
    for order in plan['orders']:
        if journal.status(day, order['id']) != 'planned':
            continue
        if order['side'] == 'buy':
            dependency = order['depends_on']
            if len(target) >= 10 or order['symbol'] in target or (
                dependency is not None and journal.status(day, dependency) != 'accepted'
            ):
                journal.transition(day, order['id'], 'skipped')
                continue
        journal.transition(day, order['id'], 'sending')
        try:
            # 副作用：此處可能提交外部委託；sending已提交，崩潰後不重送。
            result = broker.submit(order)
            state = result.get('state', 'unknown')
            if state not in ('accepted', 'rejected', 'insufficient_funds'):
                state = 'unknown'
            journal.transition(day, order['id'], state, result.get('raw'))
        except Exception:
            journal.transition(day, order['id'], 'unknown')
            raise RuntimeError('委託結果不明，停止且不自動重送') from None
        if state == 'unknown':
            raise RuntimeError('委託結果不明，請人工核對')
        if state == 'insufficient_funds':
            return state
        if state == 'accepted':
            if order['side'] == 'sell':
                target.discard(order['symbol'])
            else:
                target.add(order['symbol'])
    return 'submitted'


def order_states(journal, day):
    """回傳ISO資料日的委託摘要list，不將原始平台回應放入通知。"""
    return [dict(id=i, symbol=symbol, state=state) for i, symbol, state in journal.db.execute(
        'SELECT id,symbol,state FROM orders WHERE date=? ORDER BY id', (day,))]


def render(report):
    """將模擬報告dict縮成Discord字串；完整資訊仍保留於JSON。"""
    plan = report['plan']
    label = "模擬驗證，未下單" if report.get("mode", "dry-run") == "dry-run" else "委託結果（非成交確認）"
    lines = [f"Kronos 五日策略｜{report['date']}｜原版模型",
             f"預測 {report['target_dates'][0]}～{report['target_dates'][-1]}｜{label}",
             f"有效 {len(report['predictions'])}｜略過 {len(report['skipped'])}｜目標 {len(plan['target'])}檔"]
    for order in plan['orders']:
        side = '賣' if order['side'] == 'sell' else '買'
        lines.append(f"{side} {order['symbol']} {order['lots']:g}張 @{order['price']:g}｜{order['reason']}")
    lines.extend(plan['warnings'])
    for result in report.get('order_states', []):
        lines.append(f"委託 {result['id']} {result['symbol']}：{result['state']}")
    if not report['predictions']:
        lines.append('異常：本輪沒有有效預測；全部持股保留，請查閱略過原因。')
    if not plan['orders']:
        lines.append('本輪無調倉委託')
    lines.append('委託價為訊號日收盤價；受理不代表成交。完整紀錄見本機JSON。')
    return '\n'.join(lines)[:1950]


def add_parsers(sub):
    """向argparse子命令集合加入策略與離線預覽，預設無外部寫入。"""
    cmd = sub.add_parser('strategy', help='五日策略；預設模擬驗證')
    cmd.add_argument('--date')
    mode = cmd.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true', help='模擬驗證，不送交易委託（預設）')
    mode.add_argument('--execute', action='store_true', help='正式委託：平台契約未確認，停用')
    cmd.add_argument('--holdings', type=Path, help='模擬持股JSON：{代號: 張數}；省略則唯讀查詢平台')
    notify = cmd.add_mutually_exclusive_group()
    notify.add_argument('--send', action='store_true', help='發送當日五日策略模擬通知')
    notify.add_argument('--no-send', dest='send', action='store_false')
    cmd.set_defaults(send=False)
    preview = sub.add_parser('strategy-preview', help='離線預览五日策略')
    preview.add_argument('--date')
    preview.add_argument('--json', action='store_true')
    preview.add_argument('--live', action='store_true', help='讀取正式委託封存；不執行交易')


def execute(args, now=None):
    """接受CLI Namespace及可注入時間，回傳封存報告或非調倉日的None。"""
    from .notify import send
    from .research_data import write_json
    import os
    live = getattr(args, 'execute', False)
    if live:
        require_trading_contract()
        if args.holdings:
            raise ValueError('正式委託禁止使用模擬持股檔')
    model_id = LIVE_STRATEGY if live or getattr(args, 'live', False) else STRATEGY
    now = (now or datetime.now(TAIPEI)).astimezone(TAIPEI)
    if args.db.name == 'strategy.sqlite3':
        raise ValueError('--db須指向行情庫，不可與strategy.sqlite3相同')
    journal = Journal(args.db.parent / 'strategy.sqlite3')
    market = None
    try:
        if args.command == 'strategy-preview':
            row = journal.db.execute('SELECT date FROM runs WHERE model_id=? ORDER BY date DESC LIMIT 1',
                                     (model_id,)).fetchone()
            day = args.date or (row[0] if row else None)
            report = journal.run(day, model_id) if day else None
            if report is None:
                raise ValueError('沒有五日策略報告')
            if model_id == LIVE_STRATEGY:
                report['order_states'] = order_states(journal, report['date'])
            print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else render(report))
            return report
        day = date.fromisoformat(args.date) if args.date else now.date()
        if day > now.date() or (day == now.date() and now.hour < 19):
            raise ValueError('五日策略須在資料日台北19:00之後執行')
        if live and day != now.date():
            raise ValueError('歷史策略禁止正式委託')
        if args.send and day != now.date():
            raise ValueError('歷史策略禁止通知')
        if args.send and not os.environ.get('DISCORD_WEBHOOK_URL'):
            raise ValueError('缺少 DISCORD_WEBHOOK_URL')
        market = Store(args.db)
        calendar = Calendar(market, Http(), now.date())
        if not weekly_session(calendar, day):
            return None
        if live:
            import hashlib
            account = os.environ.get('account', '')
            if not account:
                raise ValueError('缺少account')
            fingerprint = hashlib.sha256(account.encode()).hexdigest()
            saved_account = journal.get('trading_account')
            if saved_account and saved_account != fingerprint:
                raise ValueError('策略庫綁定不同帳戶，不可混用委託紀錄')
            journal.put('trading_account', fingerprint)
        report = journal.run(day.isoformat(), model_id)
        if report is None:
            holdings = check_holdings(json.loads(args.holdings.read_text()) if args.holdings else Platform().holdings())
            source = DataSource(market, calendar.http)
            universe = source.universe(now.date())
            source.sync(universe, calendar.window(day))
            report = forecast(market, universe, calendar, day)
            report['mode'] = 'live' if live else 'dry-run'
            report['model_id'] = model_id
            report['plan'] = rebalance(report['predictions'], holdings, journal.unresolved())
            report['holdings_source'] = 'fixture' if args.holdings else 'platform'
            journal.save_run(report)
        execution_error = None
        if live:
            if datetime.now(TAIPEI).date() != day:
                raise RuntimeError('資料日已過期，禁止委託')
            try:
                execute_orders(journal, report, Platform())
            except RuntimeError as exc:
                execution_error = exc
            report['order_states'] = order_states(journal, report['date'])
        (args.db.parent / 'reports').mkdir(parents=True, exist_ok=True)
        write_json(args.db.parent / 'reports' / f'{day}.{model_id}.json', report)
        print(render(report))
        if args.send:
            if datetime.now(TAIPEI).date() != day:
                raise RuntimeError('資料日已過期，停止策略通知')
            send(journal, report, os.environ['DISCORD_WEBHOOK_URL'])
        if execution_error:
            raise execution_error
        return report
    finally:
        if market is not None:
            market.db.close()
        journal.db.close()
