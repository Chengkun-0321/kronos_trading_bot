# 專案工作指引

## 開始工作前

先閱讀 [PROJECT_CONTEXT.md](PROJECT_CONTEXT.md)、[README.md](README.md) 與 [SCHEDULE.md](SCHEDULE.md)，再檢查相關程式及 `git status`。交接紀錄中的數量、測試結果與排程狀態是帶日期的快照，處理相關問題時應重新核對。以使用者最新要求及專案實際內容為準。

## 回應與維護習慣

使用繁體中文，回應原則上不超過300字、列表不超過5項；使用者要求完整輸出時除外。程式碼直接提供，未要求時不附基礎解說。避免空泛前言，文件記錄決策與原因，保留既有使用者變更。

## 現行範圍

- 以平台完整可交易清單為範圍，包含有合格日K的ETF；不要預設只跑2330，也不要寫死商品數。
- 使用本機 `Kronos-small` 與 `Kronos-Tokenizer-base`，以120根日K預測下一交易日；已擴充一次性近五年台股日K微調與雙模型觀察。每日觀察不下單；新增五日策略模擬驗證，平台契約未確認前正式委託停用。不做新聞或語言模型質化分析，未授權週期性重訓。
- 分數為 `predicted_close / predicted_open - 1`，只選正值前20名；不足照實呈現。不要改回前10名，也不要自行加上前50檔量化篩選。
- 平台只提供每日快取的商品清單；股價及市場分類來自TWSE／TPEX整批行情。不要恢復逐檔平台分類／股價請求，避免增加平台負擔。
- 資料過期、缺值、OHLC矛盾、歷史不足及無標準OHLC商品須記錄略過原因；不得以補零或未來資料繞過檢查。

## 執行與副作用

入口為 `.venv/bin/python -m src.main`，日常功能有 `init`、`daily`、`preview`、`repair`，研究功能有 `prepare`、`train`、`evaluate`。查閱結果優先使用 `preview`，微調版可指定 `preview --model taiwan-v1`；驗證推論使用 `daily --no-send`，歷史日期必須搭配 `--no-send`。`repair` 僅供指定市場的少量個股修補。五日策略入口為 `strategy --dry-run --no-send` 與 `strategy-preview`，`--execute` 因平台契約尚未確認而硬性停用；19:00 timer僅提供未啟用範本。正常每日通知由已建立的systemd排程負責，不因一般檢查額外發送測試訊息。

一次性研究流程入口為 `.venv/bin/python -m src.research_run`，測速入口為 `.venv/bin/python -m src.benchmark tune`；參數與操作方式見 [docs/research.md](docs/research.md)。研究與測速不發Discord，維護或驗證不代表重新訓練、套用測速設定或啟用模型的授權。

不要把 `stock_final_project_for_class-main/main.py` 當作測試入口，該舊範例包含交易呼叫。僅五日策略可查持倉；正式買賣須先完成平台契約核對，其他入口不得接入買賣API。非必要不修改 `third_party/Kronos`；微調使用獨立版本目錄，不得覆寫原始模型或Tokenizer權重。

帳密與Webhook統一存於根目錄 `.env`（權限600），手動CLI載入但程序環境優先，systemd讀同一EnvironmentFile；文件只記錄位置，不輸出憑證內容。保留憑證、本機資料庫、報告及日誌的Git忽略規則。

維持SQLite下載進度、報告封存、程序鎖及通知防重送行為。報告與送達狀態以 `(date, model_id)` 區分，舊紀錄歸 `pretrained`，保留原狀態及訊息ID，不補發歷史通知。`sending`／`unknown` 不代表未送達，須先人工核對Discord，不可直接刪除狀態後重送。排程與維運方式以 [SCHEDULE.md](SCHEDULE.md) 為準。

## 研究、GPU與模型啟用

- 研究資料庫與每日行情庫分離；保留研究快照、檔案摘要及品質略過計數。依市場目標日期70／15／15切分，正規化只使用過去120日輸入；固定Tokenizer，不以測試績效選權重，保留存活偏差與未扣交易成本等限制。
- 保留每日資料庫鎖、研究 `research.lock` 及共用 `data/gpu.lock`。訓練、評估與測速遵守台北時間17:45–20:00每日GPU保留時段；測速當日例外須有使用者明示授權，不沿用過去例外或修改每日timer。
- 安全暫停先建立模型目錄的 `pause.request`，確認checkpoint保存及程序暫停後才停止研究服務；恢復前移除標記，訓練以 `--resume` 續跑。保留原子checkpoint、optimizer、GradScaler與參數歷史，只載入本機可信checkpoint；設定優先序與一次性加速回退依 [研究文件](docs/research.md) 及實作，不把某次測速結果寫成固定預設。
- 每日維持逐股推論；離線評估使用FP32、每批128筆與每樣本固定種子。`evaluation-batch128-v1.sqlite3` 與舊逐筆 `evaluation.sqlite3` 不混用；批次故障中止，保留已提交批次供續跑，不將故障整批標為資料無效。
- 完整評估且兩模型皆有有效結果後，才由 `evaluate --activate` 登錄 `models/taiwan-active.json`，不立即通知。啟用後每日兩模型共用行情、依序推論，各發正值前20；單一模型失敗不阻止另一模型。績效較差仍可觀察，新版本於下一資料日啟用。進度須核對 `pipeline.json`、`training.json`、`evaluation.json`、登錄檔與日誌，不以下載完成或歷史服務紀錄推定訓練、評估或啟用完成。

## 驗證與交接更新

依改動執行相關測試；純文件變更檢查內容、連結與diff即可。

```bash
.venv/bin/python -m unittest discover -s tests -v
PYTHONPATH=stock_final_project_for_class-main .venv/bin/python -m unittest discover -s stock_final_project_for_class-main/tests -v
```

需求、架構、已完成範圍或已知限制改變時，同步更新 `PROJECT_CONTEXT.md`；操作方式改動更新README，排程改動更新SCHEDULE。不要把完整聊天逐字稿或機密寫入交接文件。

## 一次性補跑例外（2026-09-22）

使用者明示授權strategy-catchup當日執行：9/18訊號預測9/21～9/24四交易日，9/21收盤價買10張並通知Discord。例外只限本次固定日期，空倉確認後才建倉；任何非明確受理結果停止，不重送或續單。一般策略契約限制、19:00停用timer及其他日期禁補單不變。
