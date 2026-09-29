# 五日策略模擬驗證

此功能提供訊號、調倉模擬與離線報告；一般 `strategy --execute` 因平台契約未確認而停用。以下命令從專案根目錄執行。日常觀察見 [README](../README.md)。

## 策略規則與狀態

每日18:00觀察保持原狀。五日策略在每週最後交易日19:00後，以原版Kronos-small的120根完整日K預測未來5個交易日，分數為第5日收盤／第1日開盤−1；全市場排名，同分依代號。這是每週一次，遇休市不保證間隔五個交易日；日期按官方日曆與臨時休市設定判定，日曆不明即停止。

管理帳戶全部持股。排名>20或分數≤0整檔賣出；缺有效訊號保留並占名額。Top10且分數嚴格>1%的未持股依排名補位，每次買10張；滿10檔時以更高排名候選替換最弱有效持股，不限主動替換次數。10張取代10%等權，保留股不調整張數。原有超過10檔且無必要賣出時，停止新買並提示，不擅自多賣。

全部賣单先於買單，價格使用訊號日實際收盤價。賣單受理即釋放目標名額，因此實際成交過程可能超過10檔；拒絕的賣單不釋放名額，其替換買單略過。資金不足停止本輪新買，重跑也不繼續；缺候選保留現金。必要賣出不限數量。

```bash
# 寫入已核對的模擬持股，單位為張；空倉為 {}，這不是平台原始回應格式
# 將JSON保存在已忽略的data/reports/內，例如 {"2330": 10}
.venv/bin/python -m src.main strategy --dry-run --holdings data/reports/holdings.json --no-send

# 省略--holdings時唯讀查詢平台；目前只接受已確認的成功空持股回應
.venv/bin/python -m src.main strategy --dry-run --no-send

# 歷史資料日必須是當週最後交易日；不能搭配--send
.venv/bin/python -m src.main strategy --date 2026-09-18 --holdings data/reports/holdings.json --no-send

# 離線查閱封存，不查持股、不下載、不使用GPU
.venv/bin/python -m src.main strategy-preview
.venv/bin/python -m src.main strategy-preview --json
```

預設不發Discord；只有明示 `--send` 才發當日「五日策略」模擬通知，明確標示未下單。`--no-send`只控制通知，`--dry-run`才表示不委託；目前 `--execute` 一律在外部操作之前被平台契約檢查拒絕，沒有環境變數可解除。一般策略未進行正式委託；一次性補跑例外見 [專案交接](../PROJECT_CONTEXT.md)。

策略報告與通知獨立存於行情庫旁 `strategy.sqlite3`，JSON副本為 `reports/YYYY-MM-DD.weekly-v1.json`。同日重跑沿用首次封存的訊號和持股，不重新套用不同持股檔；要比较不同模擬輸入，使用不同目錄的 `--db` 隔離整組資料。正式訊號另使用 `weekly-v1-live` 鍵，不重用模擬封存；`strategy-preview --live` 可離線讀取其委託狀態。策略庫綁定交易帳戶，不能切換帳戶混用歷史紀錄。保持行情庫程序鎖、strategy-run.lock與共用GPU鎖；與每日工作重疊時退出，不同時推論。Discord沿用sending／unknown防重送。

委託引擎已用替身驗證：POST前提交sending，受理記accepted，逾時／不明結果記unknown並停止；accepted不代表成交，不以查無持股推定單據過期。未結委託阻擋跨週執行，禁止自動撤銷、刪除紀錄或重送。正式API仍需核對非空持股欄位及張數、可賣量、拒絕／資金不足回應、委託效期與成交／撤單終態查詢，補齊平台解析及核對測試，再解除正式執行入口的契約封鎖。現階段完整提供訊號與調倉模擬、狀態機測試，尚非可啟用的自動交易系統。

憑證統一根目錄 `.env`，不輸出內容；舊 `data/discord.env` 僅作未讀取的遷移備份，仍維持Git忽略。調整根目錄檔案權限為600。19:00排程範本見 [排程文件](../SCHEDULE.md)，尚未安裝或啟用。
