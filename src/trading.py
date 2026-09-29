"""模擬平台協定邊界；契約未確認前禁止正式委託。"""
import os
import requests

BASE_URL = 'https://ciot.imis.ncku.edu.tw/sim_stock/trading_api'
BLOCKED = '平台持股欄位、錯誤格式及未成交效期尚未確認，正式委託停用'


def require_trading_contract():
    """契約未完成時一律拋出RuntimeError，無可繞過的設定開關。"""
    # 不能以環境變數略過；須取得平台契約及對應測試後才能解除。
    raise RuntimeError(BLOCKED)


class Platform:
    """沿用實驗室API端點；可注入HTTP替身，不把查詢失敗視為空倉。"""
    def __init__(self, session=None):
        """session須提供requests相容的post；省略時建立無重試Session。"""
        self.session = session or requests.Session()

    def holdings(self):
        """唯讀查詢持股；僅已確認的空list能轉為空dict，其餘拒絕解析。"""
        account, password = os.environ.get('account'), os.environ.get('password')
        if not account or not password:
            raise ValueError('缺少 account／password')
        try:
            response = self.session.post(
                BASE_URL + '/get_user_stocks', data={'account': account, 'password': password},
                timeout=(10, 30), allow_redirects=False)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError):
            raise RuntimeError('持股查詢失敗，不將查詢失敗視為空倉') from None
        if not isinstance(payload, dict) or payload.get('result') != 'success':
            raise RuntimeError('持股查詢被拒絕')
        if payload.get('data') == []:
            return {}
        raise RuntimeError('非空持股欄位尚未確認；請使用已核對的模擬持股JSON')

    def submit(self, order):
        """order為代號、方向、張數及限價；契約確認前不發HTTP。"""
        require_trading_contract()
        return self._post_order(order)

    def _post_order(self, order):
        """單次POST保留去機密回應；呼叫者須先通過一般契約或限期補跑授權。"""
        account, password = os.environ.get('account'), os.environ.get('password')
        if not account or not password:
            raise ValueError('缺少 account／password')
        if order['side'] not in ('buy', 'sell'):
            raise ValueError('未知委託方向')
        response = self.session.post(
            BASE_URL + '/' + order['side'],
            data={'account': account, 'password': password, 'stock_code': order['symbol'],
                  'stock_shares': order['lots'], 'stock_price': order['price']},
            timeout=(10, 30), allow_redirects=False)
        payload = response.json()
        # 平台若回顯登入資訊，原始結果僅保留去機密版本。
        def redact(value):
            """保留JSON結構，只遮蔽回應中的登入值，避免洩漏機密。"""
            if isinstance(value, dict):
                return {redact(k): redact(v) for k, v in value.items()}
            if isinstance(value, list):
                return [redact(v) for v in value]
            if isinstance(value, str):
                return value.replace(account, '[redacted]').replace(password, '[redacted]')
            return value
        raw = redact(payload)
        state = 'accepted' if response.status_code == 200 and isinstance(payload, dict) and payload.get('result') == 'success' else 'unknown'
        # TODO: 官方失敗格式核對後，加入rejected／insufficient_funds的精確映射。
        return {'state': state, 'raw': raw}
