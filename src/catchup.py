"""僅2026-09-22授權的四交易日補跑；不開放一般歷史委託。"""
import hashlib
import json
import math
import os
from datetime import date, datetime

from .data import Calendar, DataSource, Http
from .notify import send
from .research_data import write_json
from .storage import Store
from .strategy import Journal, TAIPEI, forecast, order_states
from .trading import Platform

SIGNAL = date(2026, 9, 18)
PRICE = date(2026, 9, 21)
EXECUTION = date(2026, 9, 22)
TARGETS = ['2026-09-21', '2026-09-22', '2026-09-23', '2026-09-24']
RUN_ID = 'catchup-20260918-four-sessions'


def check_date(now=None):
    """本次外部操作僅授權9/22；now為可注入的台北時間。"""
    if (now or datetime.now(TAIPEI)).astimezone(TAIPEI).date() != EXECUTION:
        raise ValueError('一次性補跑僅允許2026-09-22執行；其他日期只能--preview')


def buy_plan(store, predictions):
    """分數只來自9/18訊號；另讀9/21收盤價，缺價不以第11名遞補。"""
    orders, skipped = [], []
    ranked = sorted(predictions, key=lambda p: (-p['score'], p['symbol']))
    for rank, p in enumerate(ranked[:10], 1):
        if not math.isfinite(p['score']) or p['score'] <= .01:
            continue
        frame = store.history(p['symbol'], PRICE.isoformat(), limit=1)
        if frame.empty or frame.iloc[-1]['date'] != PRICE.isoformat():
            skipped.append(dict(symbol=p['symbol'], reason='缺9/21收盤價'))
            continue
        price = frame.iloc[-1]['close']
        if price is None or not math.isfinite(float(price)) or float(price) <= 0:
            skipped.append(dict(symbol=p['symbol'], reason='9/21收盤價無效'))
            continue
        orders.append(dict(id=len(orders)+1, symbol=p['symbol'], side='buy', lots=10,
                           price=float(price), price_date=PRICE.isoformat(), score=p['score'],
                           rank=rank, reason='四交易日Top10且>1%', depends_on=None))
    return dict(holdings={}, orders=orders, target={o['symbol']: 10 for o in orders},
                warnings=[], price_skipped=skipped)


class CatchupPlatform(Platform):
    """只接受已封存的本次買單，不解除一般Platform.submit契約限制。"""
    def __init__(self, orders, session=None):
        """orders為完整核定買單list，最多10檔且每檔10張。"""
        super().__init__(session)
        self.orders = orders
        self.sent = set()
        if len(orders) > 10 or len({o['symbol'] for o in orders}) != len(orders):
            raise ValueError('一次性委託清單重複或超過10檔')

    def submit(self, order):
        """比對完整封存內容後單次提交；任何非success結果交由上層停止。"""
        check_date()
        if (order not in self.orders or order['id'] in self.sent or order['side'] != 'buy'
                or order['lots'] != 10 or order['price_date'] != PRICE.isoformat()
                or not 1 <= order['rank'] <= 10 or not math.isfinite(order['score'])
                or order['score'] <= .01 or not math.isfinite(order['price']) or order['price'] <= 0):
            raise ValueError('委託不符合本次授權')
        self.sent.add(order['id'])
        # 副作用：本次明示授權的模擬平台買單；不自動重試POST。
        return self._post_order(order)


def submit_once(journal, report, broker):
    """開始標記先提交；中斷後不再啟動買單迴圈，只呈現既有結果。"""
    marker = RUN_ID + ':execution'
    if journal.get(marker):
        return
    # 與一般策略共用委託庫及鎖，任何既存外部委託都先阻擋。
    if journal.db.execute('SELECT 1 FROM orders LIMIT 1').fetchone():
        raise RuntimeError('已有委託紀錄，本次禁止另外建倉')
    holdings = broker.holdings()
    if holdings:
        raise RuntimeError('本次僅支援確認空倉後建倉')
    journal.initialize_orders(report)
    journal.put(marker, {'state': 'started', 'at': datetime.now(TAIPEI).isoformat()})
    for order in report['plan']['orders']:
        journal.transition(report['date'], order['id'], 'sending')
        try:
            result = broker.submit(order)
            state = result.get('state', 'unknown')
            if state not in ('accepted', 'rejected', 'insufficient_funds'):
                state = 'unknown'
            journal.transition(report['date'], order['id'], state, result.get('raw'))
        except Exception:
            journal.transition(report['date'], order['id'], 'unknown')
            state = 'unknown'
        if state != 'accepted':
            journal.put(marker, {'state': 'stopped', 'reason': state, 'order_id': order['id']})
            return
    journal.put(marker, {'state': 'completed', 'at': datetime.now(TAIPEI).isoformat()})


def render(report):
    """通知區分訊號、價格及提交日期，accepted絕不標為成交。"""
    labels = {'accepted': '已受理（未確認成交）', 'planned': '未提交', 'unknown': '結果不明',
              'sending': '送出中／待核對', 'rejected': '被拒絕', 'insufficient_funds': '資金不足'}
    states = {o['id']: o['state'] for o in report.get('order_states', [])}
    lines = ['Kronos 五日策略｜本週四交易日一次性補跑',
             '訊號截至2026-09-18｜預測9/21～9/24（D4收盤／D1開盤−1）',
             '委託限價：9/21實際收盤價｜補跑執行：2026-09-22',
             f"有效預測 {len(report['predictions'])}｜略過 {len(report['skipped'])}"]
    if report.get('execution_error'):
        lines.append(report['execution_error'])
    for o in report['plan']['orders']:
        state = labels.get(states.get(o['id'], 'planned'), '待核對')
        lines.append(f"#{o['rank']} {o['symbol']} {o['score']:+.2%}｜買10張 @{o['price']:g}｜{state}")
    for row in report['plan']['price_skipped']:
        lines.append(f"略過 {row['symbol']}：{row['reason']}")
    if not report['plan']['orders']:
        lines.append('無可提交買單')
    lines.append('受理不代表成交；發生拒絕或不明結果即停止，未提交單不自動續送。')
    return '\n'.join(lines)[:1950]


def execute(args, now=None):
    """固定日期補跑：預設只準備；--execute建倉，--send明示通知。"""
    preview = args.preview
    if preview and args.send:
        raise ValueError('--preview不能搭配--send；通知請用--execute --send')
    if args.db.name == 'strategy.sqlite3':
        raise ValueError('--db須指向行情庫，不可與strategy.sqlite3相同')
    if not preview:
        check_date(now)
    journal = Journal(args.db.parent / 'strategy.sqlite3')
    market = None
    try:
        report = journal.run(SIGNAL.isoformat(), RUN_ID)
        if preview:
            if report is None:
                raise ValueError('尚無一次性補跑報告')
        else:
            if args.send and not os.environ.get('DISCORD_WEBHOOK_URL'):
                raise ValueError('缺少DISCORD_WEBHOOK_URL')
            account = os.environ.get('account')
            if not account:
                raise ValueError('缺少account')
            fingerprint = hashlib.sha256(account.encode()).hexdigest()
            saved = journal.get('trading_account')
            if saved and saved != fingerprint:
                raise ValueError('策略庫已綁定不同帳戶')
            journal.put('trading_account', fingerprint)
            if report is None:
                market = Store(args.db)
                calendar = Calendar(market, Http(), EXECUTION)
                dates, day = [], SIGNAL
                for _ in range(4):
                    day = calendar.shift(day, 1)
                    dates.append(day.isoformat())
                if dates != TARGETS or calendar.is_session(date(2026, 9, 25)):
                    raise ValueError('官方日曆不符合本次四交易日設定')
                source = DataSource(market, calendar.http)
                universe = source.universe(EXECUTION)
                # 取價與預測截止日分開；history始終限定SIGNAL，防止9/21洩入模型。
                source.sync(universe, calendar.window(SIGNAL) + [PRICE])
                report = forecast(market, universe, calendar, SIGNAL, horizon=4)
                report.update(model_id=RUN_ID, price_date=PRICE.isoformat(),
                              execution_date=EXECUTION.isoformat(), mode='catchup')
                report['plan'] = buy_plan(market, report['predictions'])
                journal.save_run(report)
            if args.execute:
                try:
                    submit_once(journal, report, CatchupPlatform(report['plan']['orders']))
                except (ValueError, RuntimeError) as exc:
                    report['execution_error'] = '執行前檢查未通過，未新增委託：' + str(exc)
                    journal.put(RUN_ID + ':preflight_error', report['execution_error'])
        report['order_states'] = order_states(journal, report['date'])
        report['execution'] = journal.get(RUN_ID + ':execution')
        report['execution_error'] = journal.get(RUN_ID + ':preflight_error')
        if not preview:
            (args.db.parent / 'reports').mkdir(parents=True, exist_ok=True)
            write_json(args.db.parent / 'reports' / f'{SIGNAL}.{RUN_ID}.json', report)
        print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else render(report))
        if args.send:
            if not report['execution'] and not report['execution_error']:
                raise ValueError('尚未執行，請勿以正式結果通知')
            check_date(now)
            send(journal, report, os.environ['DISCORD_WEBHOOK_URL'])
        if not preview and (report['execution_error'] or (report['execution'] and report['execution']['state'] != 'completed')):
            raise RuntimeError('補跑未全部受理，保留紀錄且不自動續送')
        return report
    finally:
        if market is not None:
            market.db.close()
        journal.db.close()


def add_parser(sub):
    """固定日期子命令不接受任意歷史參數；預覽不使用GPU或平台。"""
    cmd = sub.add_parser('strategy-catchup', help='9/18訊號、9/21限價的四交易日一次性補跑')
    mode = cmd.add_mutually_exclusive_group()
    mode.add_argument('--execute', action='store_true', help='本次授權買單；僅2026-09-22')
    mode.add_argument('--preview', action='store_true')
    cmd.add_argument('--send', action='store_true')
    cmd.add_argument('--json', action='store_true')
