# 期末stock_project 
可至main.py測試功能

## 取得股票資訊
台股資料抓取工具，支援：
- TWSE（上市）
- TPEX（上櫃）
- ESB（興櫃）

### 安裝

```bash
pip install -r requirements.txt
```

### 使用方式

```python
from stock_api import get_taiwan_stock_data

# 取得股票資訊
# Input:
#   stock_code: 股票ID
#   start_date: 開始日期，YYYY-MM-DD
#   stop_date: 結束日期，YYYY-MM-DD
# Output: 股票資訊

df = get_taiwan_stock_data("2330", "2024-01-01", "2024-03-31")
```

### 執行解析測試

```bash
python -m unittest discover -s tests -v
```

測試不會呼叫持股、預約購入或預約售出功能。

### 輸出欄位

📊 資料欄位說明

新版資料介面維持舊版 API 的欄位名稱與資料語意，包含：

- date：日期
- capacity：成交股數
- turnover：成交金額（元）
- high：最高價
- low：最低價
- close：收盤價
- change：漲跌價差
- transaction_volume：成交筆數
- stock_code_id：股票代號
- open：開盤價
- market：資料市場（TWSE / TPEX / ESB）
- close_is_proxy：close 是否為代理值；興櫃為 True

為了方便 pandas 分析與模型訓練，資料型態改為：

- date 使用 datetime
- 數值欄位使用 numeric（int / float）
- 不再使用字串格式數值

### 注意事項

- TWSE / TPEX 使用官方標準 OHLC
- ESB 無官方標準 Open / Close
- ESB 的 `close` 為兩種成交方式合併後的加權平均成交價代理值
- ESB 沒有標準開盤價，因此 `open` 為 NaN

## 取得持有股票

### 使用方式

```python
# 取得持有股票
# Input:
#   account: 使用者帳號
#   password: 使用者密碼
# Output: 持有股票陣列

user_stocks = Get_User_Stocks(account, password)
```

## 預約購入股票

### 使用方式

```python
# 預約購入股票
# Input:
#   account: 使用者帳號
#   password: 使用者密碼
#   stock_code: 股票ID
#   stock_shares: 購入張數
#   stock_price: 購入價格
# Output: 是否成功預約購入(True/False)

Buy_Stock(account, password, "2330",1, 1975)
```

## 預約售出股票

### 使用方式

```python
# 預約售出股票
# Input:
#   account: 使用者帳號
#   password: 使用者密碼
#   stock_code: 股票ID
#   stock_shares: 售出張數
#   stock_price: 售出價格
# Output: 是否成功預約售出(True/False)

Sell_Stock(account, password, "2330",1, 1978)
```