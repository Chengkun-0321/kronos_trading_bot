"""以一則 Discord 訊息發送觀察名單，對不明送達狀態避免自動重送。"""
import time
from urllib.parse import urlsplit

import requests

from .prediction import rank


def _validate_webhook(webhook: str):
    """驗證 webhook 僅指向 Discord 官方 HTTPS 端點。

    Args:
        webhook: 待驗證的完整 webhook URL。
    Returns:
        None；格式錯誤時拋出 ValueError。
    """
    parsed = urlsplit(webhook)
    if parsed.scheme != 'https' or parsed.hostname != 'discord.com' or not parsed.path.startswith('/api/webhooks/'):
        raise ValueError('DISCORD_WEBHOOK_URL 格式不符')


def render(report: dict) -> str:
    """將完整報告縮成 Discord 單則訊息，保留日期、涵蓋數及不足二十檔狀態。"""
    if report.get('model_id') == 'catchup-20260918-four-sessions':
        from .catchup import render as render_catchup
        return render_catchup(report)
    if report.get('model_id') in ('weekly-v1', 'weekly-v1-live'):
        from .strategy import render as render_strategy
        return render_strategy(report)
    candidates = rank(report['predictions'])
    label = report.get("model_id", "pretrained")
    lines = [f"Kronos [{label}] 前20名觀察名單｜資料 {report['date']} → 預測 {report['target_date']}",
             f"範圍 {report['universe_count']}｜有效預測 {len(report['predictions'])}｜略過 {len(report['skipped'])}",
             '排名依預測開盤至收盤漲幅；僅供人工驗證，未下單。']
    for i, row in enumerate(candidates, 1):
        p = row['prediction']
        name = str(row['name']).replace('\n', ' ')[:24]
        lines.append(f"{i}. {row['symbol']} {name}｜開 {p['open']:.2f} → 收 {p['close']:.2f}｜{row['score']:+.2%}")
    if len(candidates) < 20:
        lines.append(f"正漲幅僅 {len(candidates)} 檔，不補入負值。")
    if not report['predictions']:
        lines.append('本次沒有有效預測，請查閱本機略過原因。')
    return '\n'.join(lines)


def send(store, report: dict, webhook: str, session=None) -> str:
    """發送已封存報告；回傳 sent/already_sent，狀態不明則要求人工核對。"""
    _validate_webhook(webhook)
    day = report['date']
    model_id = report.get('model_id', 'pretrained')
    delivery = store.delivery(day, model_id)
    if delivery:
        if delivery[0] == 'sent':
            return 'already_sent'
        if delivery[0] in ('sending', 'unknown'):
            raise RuntimeError('Discord 送達狀態不明；請先核對頻道，不自動重送')
    client = session or requests.Session()
    for attempt in range(5):
        store.set_delivery(day, 'sending', model_id=model_id)
        try:
            # POST 有外部副作用；先記錄 sending，程序中斷後不盲目再次發送。
            response = client.post(webhook, params={'wait': 'true'},
                                   json={'content': render(report), 'allowed_mentions': {'parse': []}},
                                   timeout=(10, 30), allow_redirects=False)
        except requests.RequestException:
            store.set_delivery(day, 'unknown', model_id=model_id)
            raise RuntimeError('Discord 連線中斷，送達狀態不明') from None
        if response.status_code == 429:
            store.set_delivery(day, 'retryable', model_id=model_id)
            try:
                delay = float(response.json().get('retry_after', 2 ** attempt))
            except (ValueError, TypeError):
                delay = 2 ** attempt
            if delay > 60:
                raise RuntimeError('Discord 限流等待超過60秒，稍後重新執行 daily')
            if attempt < 4:
                time.sleep(max(1, delay))
                continue
            raise RuntimeError('Discord 限流重試已達上限')
        if response.status_code >= 500:
            store.set_delivery(day, 'unknown', model_id=model_id)
            raise RuntimeError('Discord 伺服器錯誤，送達狀態不明')
        if not 200 <= response.status_code < 300:
            store.set_delivery(day, 'failed', model_id=model_id)
            raise RuntimeError(f'Discord 拒絕訊息：HTTP {response.status_code}')
        try:
            message_id = str(response.json()['id'])
        except (ValueError, KeyError, TypeError):
            store.set_delivery(day, 'unknown', model_id=model_id)
            raise RuntimeError('Discord 未回傳訊息 ID，送達狀態不明') from None
        store.set_delivery(day, 'sent', message_id, model_id=model_id)
        return 'sent'
    raise RuntimeError('Discord 未送達')


def send_failure_alert(webhook: str, day: str, session=None) -> str:
    """發送指定日期的排程失敗告警，不包含原始例外或憑證。

    Args:
        webhook: Discord 官方 webhook URL。
        day: 台北日期，格式為 YYYY-MM-DD。
        session: 可選 requests 相容客戶端，供測試注入。
    Returns:
        成功時回傳 sent；失敗時拋出無機密的 ValueError 或 RuntimeError。
    """
    _validate_webhook(webhook)
    content = (f'Kronos 每日預測失敗｜{day}\n'
               '自動執行已重試至上限，部分或全部模型未完成；已送達榜單不重送。\n'
               '請查看 journalctl --user -u kronos-daily.service -n 100。')
    client = session or requests.Session()
    try:
        # POST 可能已送達，連線結果不明時不可再次發送，以免重複告警。
        response = client.post(webhook, params={'wait': 'true'},
                               json={'content': content, 'allowed_mentions': {'parse': []}},
                               timeout=(10, 30), allow_redirects=False)
    except requests.RequestException:
        raise RuntimeError('Discord 告警連線中斷，送達狀態不明') from None
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f'Discord 告警失敗：HTTP {response.status_code}')
    return 'sent'
