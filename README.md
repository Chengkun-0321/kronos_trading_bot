# Kronos 台股全市場觀察

## 這是什麼

這個專案用來觀察 AI 模型挑選台股的結果。程式先取得平台上的可交易股票清單，下載每天的股價資料（日 K）。它用每檔股票最近 120 個交易日的資料，預測下一個交易日的價格。

程式會計算「預測收盤價 ÷ 預測開盤價 − 1」，只選出結果為正的前 20 名，送到 Discord 供人工觀察。不足 20 檔就照實顯示。這是模型的預測結果，不代表股票真的會上漲；每日觀察流程不會下單。

專案也有台股資料微調、兩個模型比較和五日策略模擬。一般五日策略的正式下單功能目前停用。

## 怎麼安裝

需要 Python 3、可使用的交易平台帳號，以及另外取得的本機模型檔案。發送 Discord 通知時，還需要 Webhook。模型檔案不在 Git 專案裡。

在專案根目錄執行：

```bash
git submodule update --init --recursive
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
```

把平台的 `account`、`password` 填入 `.env`。需要 Discord 通知時，再填入 `DISCORD_WEBHOOK_URL`。把模型檔案分別放在 `models/Kronos-small/` 和 `models/Kronos-Tokenizer-base/`。憑證、模型和下載資料都不會提交到 Git。

## 怎麼執行

第一次使用，依序執行：

```bash
# 下載最近 120 個交易日的股價；第一次可能需要一段時間
.venv/bin/python -m src.main init

# 台北時間 18:00 後產生當日報告，不送 Discord
.venv/bin/python -m src.main daily --no-send

# 看剛才存下的報告，不重新下載或預測
.venv/bin/python -m src.main preview
```

遇到休市日不會產生排行榜。如果交易所資料尚未更新，程式會停止；個別股票的資料缺漏或不合理時，會記錄跳過原因，不會補零或使用未來資料。

## 專案怎麼組成

- `src/`：下載資料、執行預測、保存報告與發送通知的程式；入口是 `src.main`。
- `third_party/Kronos/` 和 `models/`：Kronos 原始碼及本機模型檔案。
- `data/`：下載的股價、資料庫和報告；這些本機資料不會提交到 Git。
- `deploy/`、`docs/` 和 `tests/`：排程設定、研究與策略說明，以及測試。

`stock_final_project_for_class-main/main.py` 是含交易呼叫的舊範例，不是這個專案的執行入口。

## 怎麼使用

平常想看結果，用 `.venv/bin/python -m src.main preview`。若要查看已保存的微調版報告，用 `.venv/bin/python -m src.main preview --model taiwan-v1`。

`.venv/bin/python -m src.main daily` 會把當日排行榜送到 Discord。專案的每日通知由台北時間 18:00 的 systemd 排程負責；如何安裝、查看狀態或處理通知問題，見 [排程說明](SCHEDULE.md)。歷史日期只能搭配 `--no-send` 執行，避免補送舊通知。

想了解模型微調與評估，見 [研究說明](docs/research.md)；想試五日策略模擬，見 [策略說明](docs/strategy.md)。正式策略下單仍未開放。股價使用未還原的官方資料，除權息可能影響預測；歷史評估也沒有扣除交易成本。

## 測試

```bash
.venv/bin/python -m unittest discover -s tests -v
PYTHONPATH=stock_final_project_for_class-main .venv/bin/python -m unittest discover -s stock_final_project_for_class-main/tests -v
```

測試使用模擬資料，不會發送 Discord 或下單。
