from stock_api import get_all_stock_list, get_taiwan_stock_data, Get_User_Stocks,Buy_Stock,Sell_Stock


account = '帳號'  # 使用者帳號
password = '密碼'  # 使用者密碼

# 取得所有股票代號
all_stock_list = get_all_stock_list()


# 取得股票資訊
df = get_taiwan_stock_data("2330", "2026-07-01", "2026-07-10")
print(df)
# 取得持有股票
user_stocks = Get_User_Stocks(account, password)

# 預約購入股票
Buy_Stock(account, password, 2330,1, 1975)

# 預約售出股票
Sell_Stock(account, password, 2330,1, 1978)