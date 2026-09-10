# 專案工作指引

## 開始工作前

先閱讀 [PROJECT_CONTEXT.md](PROJECT_CONTEXT.md)、[README.md](README.md) 與 [SCHEDULE.md](SCHEDULE.md)，再檢查相關程式及 `git status`。交接紀錄中的數量、測試結果與排程狀態是帶日期的快照，處理相關問題時應重新核對。以使用者最新要求及專案實際內容為準。

## 回應與維護習慣

使用繁體中文，回應原則上不超過300字、列表不超過5項；使用者要求完整輸出時除外。程式碼直接提供，未要求時不附基礎解說。避免空泛前言，文件記錄決策與原因，保留既有使用者變更。

## 第一階段範圍

- 以平台完整可交易清單為範圍，包含有合格日K的ETF；不要預設只跑2330，也不要寫死商品數。
- 使用本機 `Kronos-small` 與 `Kronos-Tokenizer-base`，以120根日K預測下一交易日；第一階段不訓練、不下單、不做新聞或語言模型質化分析。
- 分數為 `predicted_close / predicted_open - 1`，只選正值前20名；不足照實呈現。不要改回前10名，也不要自行加上前50檔量化篩選。
- 平台只提供每日快取的商品清單；股價及市場分類來自TWSE／TPEX整批行情。不要恢復逐檔平台分類／股價請求，避免增加平台負擔。
- 資料過期、缺值、OHLC矛盾、歷史不足及無標準OHLC商品須記錄略過原因；不得以補零或未來資料繞過檢查。

## 執行與副作用

入口為 `.venv/bin/python -m src.main`，功能有 `init`、`daily`、`preview`、`repair`。查閱結果優先使用 `preview`；驗證推論使用 `daily --no-send`。正常每日通知由已建立的systemd排程負責，不因一般檢查額外發送測試訊息。

不要把 `stock_final_project_for_class-main/main.py` 當作測試入口，該舊範例包含交易呼叫。新增功能不得意外接入持倉或買賣API。非必要不修改 `third_party/Kronos` 或模型權重。

Webhook僅存於 `data/discord.env`，手動CLI讀程序環境、systemd讀EnvironmentFile；文件只記錄位置，不輸出憑證內容。保留憑證、本機資料庫、報告及日誌的Git忽略規則。

維持SQLite下載進度、報告封存、程序鎖及通知防重送行為。`sending`／`unknown` 不代表未送達，不可直接刪除狀態後重送。排程與維運方式以 [SCHEDULE.md](SCHEDULE.md) 為準。

## 驗證與交接更新

依改動執行相關測試；純文件變更檢查內容、連結與diff即可。

```bash
.venv/bin/python -m unittest discover -s tests -v
PYTHONPATH=stock_final_project_for_class-main .venv/bin/python -m unittest discover -s stock_final_project_for_class-main/tests -v
```

需求、架構、已完成範圍或已知限制改變時，同步更新 `PROJECT_CONTEXT.md`；操作方式改動更新README，排程改動更新SCHEDULE。不要把完整聊天逐字稿或機密寫入交接文件。
