"""systemd 最終失敗處理器；使用既有 Discord webhook 發送無機密維運告警。"""
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from .notify import send_failure_alert

TAIPEI = ZoneInfo('Asia/Taipei')


def main():
    """發送台北當日失敗告警。

    Returns:
        None；失敗時以狀態碼1退出，安全錯誤由 journal 保留。
    """
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    day = datetime.now(TAIPEI).date().isoformat()
    try:
        result = send_failure_alert(os.environ.get('DISCORD_WEBHOOK_URL', ''), day)
    except (ValueError, RuntimeError) as exc:
        logging.error('%s', exc)
        raise SystemExit(1) from None
    logging.info('Discord failure alert: %s', result)


if __name__ == '__main__':
    main()
