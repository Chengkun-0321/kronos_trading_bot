# Kronos 台股全市場觀察

每日台灣時間 18:00，以平台可交易清單為範圍，從交易所取得日 K，使用本機 Kronos-small 預測下一交易日，再發一則 Discord 排行榜。沒有訓練、持倉查詢、下單或新聞分析。

## 專案交接

新工作先閱讀 [AGENTS.md](AGENTS.md)；已確認決策、階段範圍、驗證快照與後續方向見 [PROJECT_CONTEXT.md](PROJECT_CONTEXT.md)。

## 執行

在專案根目錄使用現有 `.venv`，缺依賴時執行 `.venv/bin/pip install -r requirements.txt`。模型須已放置於 `models/Kronos-small`、`models/Kronos-Tokenizer-base`，原始碼位於 `third_party/Kronos`。初始化與預覽不載入模型；推論自動優先使用 CUDA。

```bash
# 初始化全市場最近120個交易日；可中斷後重跑
.venv/bin/python -m src.main init

# 收盤後計算並保存結果，但不送通知
.venv/bin/python -m src.main daily --no-send

# 送出當日觀察名單；先在環境設定 DISCORD_WEBHOOK_URL
.venv/bin/python -m src.main daily

# 離線預覽最近報告，不下載資料、不使用GPU、不送訊息
.venv/bin/python -m src.main preview
.venv/bin/python -m src.main preview --json

# 用已結束交易日驗證全市場；歷史驗證只能搭配 --no-send
.venv/bin/python -m src.main daily --date 2026-09-10 --no-send
```

`--db PATH` 放在子命令之前可指定獨立 SQLite。所有命令共用資料庫旁的程序鎖，同時只能執行一個初始化／預測／修補工作。不要執行 `stock_final_project_for_class-main/main.py` 作為此工具入口，該舊範例含交易呼叫。

## 資料與請求量

平台 `/trading_api/stock_list` 每日成功下載一次並快取；商品數動態決定，不預設2330或固定2565檔。不呼叫逐檔 `stock_type`，也不呼叫平台歷史價格API。

交易所市場分類來自整批行情本身，包括上市、上櫃ETF，不會把全部ETF誤分為上市。價格採官方未還原日K，成交量統一為股、金額為元；不混用 Yahoo 調整價格。保留當時的原始價格，除權息可能影響預測，第一版不另外校正。

- TWSE：`https://www.twse.com.tw/exchangeReport/MI_INDEX?response=json&type=ALLBUT0999&date=YYYYMMDD`
- TPEX：`https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes?response=json&type=EW&date=YYYY/MM/DD`
- 交易日曆：`https://www.twse.com.tw/holidaySchedule/holidaySchedule?response=json&queryYear=民國年`

使用官方按日期整批日報，避免最新行情 OpenAPI 偶爾回傳前一日快取。日期不符或任一市場資料尚未更新即停止，不用舊行情產生當日排名。

首次初始化通常是120交易日 × 2市場＝240次行情GET，另加清單及日曆；每日正常更新是2次行情GET。缺日自動補抓；商品清單新增代號時，重抓視窗內整批日報補上新商品。GET序列執行，間隔至少2秒；429、5xx、連線／JSON異常最多五次嘗試，指數退避並尊重數值Retry-After（超過60秒則停止，稍後重跑）。沒有每檔三年下載。初始化實際時間受網路與上游節流影響。

原 fetcher 保留為手動個股修補入口，直接指定由交易所確認的市場，不查平台分類：

```bash
.venv/bin/python -m src.main repair --symbol 2330 --market TWSE --start 2026-08-01 --end 2026-08-31
```

該命令沿用既有逐月fetcher及其節流，只適合少量修補，不是每日全市場入口。

## 預測與保存

最近120根資料必須包含指定資料日，OHLC須為有限正值、最高／最低價一致，成交量與金額須為正。缺值不補零，不跨商品補值。興櫃無標準OHLC、新掛牌不足120根、停牌／異常者列入略過清單；範圍仍是完整平台商品清單。

每檔使用一個預測樣本，`pred_len=1`、`T=1`、`top_p=0.9`、`top_k=0`，依代號和預測日期固定種子；相同硬體與軟體環境便於重現，不保證跨裝置位元一致。模型與tokenizer只載入一次，逐檔推論。預測OHLC或成交資料異常者不參與排名。

分數為 `predicted_close / predicted_open - 1`；只保留正值，依分數遞減、代號遞增排序取前二十。不是上漲機率。不足二十檔照實呈現；全部無效會顯示異常空榜，不製造推薦。

`data/market.sqlite3` 儲存日K（代號＋日期唯一）、完整清單、日曆、下載進度、全部有效預測、每檔略過原因及通知狀態；同日報告封存後重跑直接使用。`data/reports/YYYY-MM-DD.json` 是供閱讀的報告副本，資料庫為主。資料日與目標交易日均保留；不讀未來K線。單股失敗不影響其他股票，模型載入失敗或市場下載不完整會中止整次執行。

官方日曆未公布／年份不符時停止；臨時停市未反映在年度表時，日報缺資料也會停止，須核對公告後將日期、原因及來源加入 `config/market_closures.json` 再重跑，不會猜測開市或產生虛構K線。已納入證券商公會公告的2026-07-10颱風休市；排程啟動時讀取此設定。

## Discord 與排程

排程建立紀錄、設定位置、查詢日誌與啟停指令見根目錄 [SCHEDULE.md](SCHEDULE.md)。

憑證放 `data/discord.env`，格式參考 `.env.example`，權限設為600；此檔與本機資料均被Git忽略。手動CLI讀程序環境，systemd讀該環境檔。不在程式、報告或日誌保存Webhook網址。

先完成初始化、全市場 `--no-send` 驗證及測試，再安裝：

```bash
mkdir -p ~/.config/systemd/user
cp deploy/kronos-daily.service deploy/kronos-daily.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now kronos-daily.timer
systemctl --user list-timers kronos-daily.timer
journalctl --user -u kronos-daily.service -n 50
```

範本路徑為本專案 `/home/large_space_v2/kronos_trading_bot`，搬移後須修改service。開機未登入仍需排程的主機應啟用該使用者的linger。機器關機期間不補發過期通知；休市日不發排行榜。下載失敗後可當晚手動重跑daily，資料缺口會補齊。長時間初始化不應與18:00任務重疊。

通知使用 `wait=true` 取得訊息ID；成功後同日不重送。明確429可重試；POST逾時、5xx、程序中斷可能已送達，保留 `unknown`／`sending` 並停止盲目重送。若發生此狀態，先在Discord核對：已送達則將該日delivery標為sent並補訊息ID；確認未送達才改為failed後重跑。這是外部Webhook無冪等鍵下的取捨，不宣稱網路故障時能保證恰好送達一次。

## 測試

```bash
.venv/bin/python -m unittest discover -s tests -v
PYTHONPATH=stock_final_project_for_class-main .venv/bin/python -m unittest discover -s stock_final_project_for_class-main/tests -v
```

自動測試使用合成行情及HTTP替身，不發送Discord、不呼叫交易API；涵蓋全清單掃描與ETF、資料品質、日期／休市、增量快取、排名、重試、預览與防重送。
