from .symbols import get_stock_market
from .fetchers import (
    get_twse_stock_data,
    get_tpex_stock_data,
    get_esb_stock_data,
)
import requests

def to_legacy_schema(df):
    legacy_columns = [
        "date",
        "capacity",
        "turnover",
        "high",
        "low",
        "close",
        "change",
        "transaction_volume",
        "stock_code_id",
        "open",
        "market",
        "close_is_proxy",
    ]
    return df[legacy_columns].copy()


def get_all_stock_list():
    data = requests.get(
        "https://ciot.imis.ncku.edu.tw/sim_stock/trading_api/stock_list"
    ).json()

    stock_codes_list = list(data.keys())
    
    return stock_codes_list

    
def get_taiwan_stock_data(stock_code: str, start_date: str, end_date: str):
    """取得股票資訊"""
    market = get_stock_market(stock_code)

    if market == "TWSE":
        return to_legacy_schema(get_twse_stock_data(stock_code, start_date, end_date))
    elif market == "TPEX":
        return to_legacy_schema(get_tpex_stock_data(stock_code, start_date, end_date))
    elif market == "ESB":
        return to_legacy_schema(get_esb_stock_data(stock_code, start_date, end_date))
    else:
        raise ValueError(f"不支援的市場別: {market}")
    
    
BASE_URL = "https://ciot.imis.ncku.edu.tw/sim_stock/trading_api"

def Get_User_Stocks(account: str, password: str):
    """取得持有股票"""
    data = {'account': account,
            'password': password
            }
    response = requests.post(f"{BASE_URL}/get_user_stocks", data=data)
    result = response.json()
    if(result['result'] == 'success'):
        return result['data']
    return dict([])

# 預約購入股票
def Buy_Stock(account, password, stock_code, stock_shares, stock_price):
    """預約購入股票"""
    print('Buying stock...')
    data = {'account': account,
            'password': password,
            'stock_code': stock_code,
            'stock_shares': stock_shares,
            'stock_price': stock_price}
    
    response = requests.post(f"{BASE_URL}/buy", data=data)
    result = response.json()
    print('Result: ' + result['result'] + "\nStatus: " + result['status'])
    return result['result'] == 'success'

# 預約售出股票
def Sell_Stock(account, password, stock_code, stock_shares, stock_price):
    """預約售出股票"""
    print('Selling stock...')
    data = {'account': account,
            'password': password,
            'stock_code': stock_code,
            'stock_shares': stock_shares,
            'stock_price': stock_price}
    response = requests.post(f"{BASE_URL}/sell", data=data)
    result = response.json()
    print('Result: ' + result['result'] + "\nStatus: " + result['status'])
    return result['result'] == 'success'