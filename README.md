# Kronos 台股全市場觀察

每日台灣時間 18:00，以平台可交易清單為範圍，從交易所取得日 K，使用本機 Kronos-small 預測下一交易日，再發一則 Discord 排行榜。支援一次性台股微調及雙模型觀察；每日觀察不查持倉、不下單、不做新聞分析；另有五日策略模擬驗證，正式委託暫停用。

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

交易所市場分類來自整批行情本身，包括上市、上櫃ETF，不會把全部ETF誤分為上市。價格採官方未還原日K，成交量統一為股、金額為元；不混用 Yahoo 調整價格。保留當時的原始價格，除權息可能影響預測，目前不另外校正；研究快照明確記錄此限制。

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

憑證統一放根目錄 `.env`，格式參考 `.env.example`，權限設為600；此檔與本機資料均被Git忽略。手動CLI載入根目錄 `.env`，程序環境值優先；systemd讀同一環境檔。帳號鍵為 `account`、密碼鍵為 `password`、通知鍵為 `DISCORD_WEBHOOK_URL`。不在程式、報告或日誌保存Webhook網址。

先完成初始化、全市場 `--no-send` 驗證及測試，再安裝：

```bash
mkdir -p ~/.config/systemd/user
cp deploy/kronos-daily.service deploy/kronos-daily.timer deploy/kronos-failure-notify.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now kronos-daily.timer
systemctl --user list-timers kronos-daily.timer
journalctl --user -u kronos-daily.service -n 50
```

範本路徑為本專案 `/home/large_space_v2/kronos_trading_bot`，搬移後須修改service。開機未登入仍需排程的主機應啟用該使用者的linger。機器關機期間不補發過期通知；休市日不發排行榜。下載失敗後可當晚手動重跑daily，資料缺口會補齊。長時間初始化不應與18:00任務重疊。

行情 GET 遇分塊回應中斷、逾時、429 或 5xx 時最多重試五次；每日服務仍失敗時隔五分鐘整體重啟，23小時內最多三次啟動。中途重啟不告警，耗盡後才使用同一 webhook 發送一次無 mention 的失敗告警。若 Discord 本身故障，告警也可能無法送達，仍須查 journal。

通知使用 `wait=true` 取得訊息ID；成功後同日不重送。明確429可重試；POST逾時、5xx、程序中斷可能已送達，保留 `unknown`／`sending` 並停止盲目重送。若發生此狀態，先在Discord核對：已送達則將該日delivery標為sent並補訊息ID；確認未送達才改為failed後重跑。這是外部Webhook無冪等鍵下的取捨，不宣稱網路故障時能保證恰好送達一次。

## 測試

```bash
.venv/bin/python -m unittest discover -s tests -v
PYTHONPATH=stock_final_project_for_class-main .venv/bin/python -m unittest discover -s stock_final_project_for_class-main/tests -v
```

自動測試使用合成行情及HTTP替身，不發送Discord、不呼叫交易API；涵蓋全清單掃描與ETF、資料品質、日期／休市、增量快取、排名、重試、預览與防重送。

## 台股微調與雙模型觀察

使用近五年官方未還原日K，平台清單含合格ETF。研究資料放在獨立目錄，不佔用每日行情庫；按市場日期70%／15%／15%切分訓練、驗證、測試，同日所有商品屬於同一集合。清單固定於準備日，有存活偏差；原始模型預訓練資料範圍不明，因此歷史測試不宣稱完全樣本外。

每筆樣本為同商品121日：前120日×6欄OHLCVA作輸入，第121日作標籤；另有分鐘、小時、星期、日、月五欄時間特徵。以輸入120日的均值與標準差正規化，裁切至±5，與原版推論一致。停牌缺日、非有限值、零量、OHLC矛盾及不足視窗排除並計數，不串接不同商品。驗證及測試的輸入可以包含之前集合的歷史，目標日不得跨集合。

```bash
# 截止日必須是今日以前的交易日；下載可中斷並以同一命令續跑
.venv/bin/python -m src.main prepare --dataset data/research/taiwan-20260914 --cutoff 2026-09-14

# 真實梯度及顯存測試：不保存或啟用模型
.venv/bin/python -m src.main train --dataset data/research/taiwan-20260914 --output models/taiwan-v1 --smoke-steps 2

# 第一次完整微調；中斷後加 --resume
.venv/bin/python -m src.main train --dataset data/research/taiwan-20260914 --output models/taiwan-v1
.venv/bin/python -m src.main train --dataset data/research/taiwan-20260914 --output models/taiwan-v1 --resume

# 測試集比較完成後登錄每日第二模型；此命令不發送Discord
.venv/bin/python -m src.main evaluate --dataset data/research/taiwan-20260914 --output models/taiwan-v1 --activate

# 查看已保存微調版，不下載、不推論、不通知
.venv/bin/python -m src.main preview --model taiwan-v1
```

固定原始Tokenizer，僅微調small。AdamW學習率4e-5、betas=(0.9,0.95)、weight_decay=0.1；預設每批1筆、累積32筆（可由測速結果調整）、梯度裁切3，尾批按實際數量平均。固定種子42，每輪完整打亂訓練樣本；只對第121日的兩組代碼計算交叉熵。最多10輪，驗證損失連續3輪未改善即停，保存最佳權重，不以測試績效選權重。完整五年全市場訓練可能很久，應先實測吞吐量。

`manifest.json`保存日期、切分、商品、品質略過計數及檔案SHA256；`resume.pt`保存訓練與optimizer進度，僅載入本機自行產生檔案；`training.json`保存最佳checkpoint與參數。不得覆寫原始模型。17:45–20:00讓出每日GPU時段，訓練於完整累積步驟後保存進度退出，20:00後以`--resume`續跑；驗證中斷則重做本輪驗證。準備行情不使用GPU，可同時運行。

評估逐筆快取在`evaluation.sqlite3`，可重跑續接；`evaluation.json`提供各模型及共同有效商品的按日勝率、前20等權開收盤報酬、全池排名相關性與無效預測數。沒有正值候選時指標為空，不以零收益代替。報酬未扣成本，尚未假設模擬平台可按開收盤成交。

完整評估且兩模型皆有有效結果後，`--activate`原子寫入`models/taiwan-active.json`。績效較差仍可啟用作觀察。每日原版與微調版共用行情、相同120日與抽樣參數，依序使用GPU，各發一封前20通知並標明版本。尚未登錄微調版時維持原版單封；登錄損壞或一個模型失敗時，另一模型仍獨立執行，整體退出失敗供既有排程重試。

報告與通知採`(date, model_id)`鍵；舊紀錄自動遷移為`pretrained`，保留sent／sending／unknown與訊息ID，不補發歷史通知。原版JSON仍為`YYYY-MM-DD.json`；微調版為`YYYY-MM-DD.版本.json`。未來新版本使用新目錄及版本名，於下一個資料日啟用，避免同日觀察混入多個微調版本。

### 一次性背景流程

```bash
.venv/bin/python -u -m src.research_run --dataset data/research/taiwan-20260914 --output models/taiwan-v1 --cutoff 2026-09-14 --activate
```

這個入口依序prepare、顯存測試、train、evaluate；一般錯誤立即停止，僅讓出每日GPU時段（退出碼75）會等到20:00後自動續跑。完成訓練後重跑不重訓，而是續做評估；`--activate`不立即發通知。進度在研究目錄`pipeline.json`。訓練正常暫停與每輪結束均寫resume.pt；程序意外中止時回到最近checkpoint，若第一輪尚未保存則重新開始。

2026-09-15已以使用者級臨時service啟動本次一次性工作：`kronos-research-20260914.service`，完整日誌`data/research-five-years.log`。這不是開機或定期重訓排程；主機重開後須以同一研究命令續跑。尚未完成完整微調與評估，不代表第二模型已啟用。

## GPU批次測速與加速續跑

訓練支援 `--batch-size`、`--accumulation-steps`、`--precision fp32|fp16`。設定優先序為CLI明示值、模型目錄的 `training-config.json`、續跑checkpoint、舊版預設1×32／FP32。Tokenizer始終FP32；FP16只用於預測模型並保存GradScaler狀態。批次或精度改變會記錄於 `config_history`，可能改變收斂與亂數序列，不保證與舊版逐筆訓練數值一致。

新版訓練每五分鐘於完整optimizer步驟後原子保存一次，亦在每日保留時段及手動暫停時保存。要安全交接，先建立 `models/taiwan-v1/pause.request`，確認日誌已顯示暫停及checkpoint更新後才停止研究service；移除該檔才恢復。驗證中暫停會重做本輪驗證。舊程序未載入新版時不支援此標記，須等下一次原子checkpoint完成再停止，從該進度續跑。

```bash
# 已安全暫停研究程序後，在17:45–20:00以外執行；本命令不發送Discord
.venv/bin/python -m src.benchmark tune --dataset data/research/taiwan-20260914 --checkpoint models/taiwan-v1/resume.pt --report data/gpu-benchmark-20260916 --apply-output models/taiwan-v1

# 套用完成後用同一研究入口續跑，既有評估與登錄流程維持
.venv/bin/python -m src.research_run --dataset data/research/taiwan-20260914 --output models/taiwan-v1 --cutoff 2026-09-14 --activate
```

測速預設遵守每日保留時段；使用者明示同意當日例外時，可加 `--allow-reserved-window-today`，隔日自動失效，且不改正式訓練的時段規則。測速取得 `research.lock` 及每日工作共用的 `data/gpu.lock`。每組使用獨立子程序，唯讀checkpoint複製模型與optimizer狀態，避免污染正式訓練。比較FP32／FP16、批次1倍增至OOM（防護上限4096）；批次≤32時保持有效批次32，以上每批更新。每組暖機10次、計時100次完整更新，前三名再各測兩次，以吞吐量中位數排序，第一個通過10分鐘穩定測試及保留10%顯存的設定才套用。

報告目錄以來源摘要防止混用；`summary.json`包含最佳設定、每秒樣本數、加速倍數、峰值顯存及訓練＋驗證每輪估時。估時不含載入、checkpoint寫入、每日讓路或其他系統負載。顯存餘裕採CUDA峰值保留量加上外部／驅動開銷估算。`gpu.csv`僅由本次一次性交接流程另行記錄。

套用前保存 `training-fallback.json`。研究入口的加速訓練失敗時回退一次，記錄 `training-fallback-used.json`，從最近checkpoint續跑；原設定亦失敗則停止，避免無限重試。一般手動 `src.main train` 不自動回退。省略 `--apply-output` 可只測速；測速失敗不改正式設定。

### 批次評估

`evaluate`預設每批128筆、FP32，不使用梯度累積。每個樣本有獨立固定抽樣種子；批次運算可能與逐筆推論產生數值差異。新快取為輸出目錄下`evaluation-batch128-v1.sqlite3`，舊`evaluation.sqlite3`保留但不混用。重跑相同evaluate命令可續接新快取，尾批不足128照實處理。批次推論故障停止，已提交批次保留；進度與速度每2560筆寫入日誌。


## 五日策略（2026-09-22，僅模擬驗證）

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

預設不發Discord；只有明示 `--send` 才發當日「五日策略」模擬通知，明確標示未下單。`--no-send`只控制通知，`--dry-run`才表示不委託；目前 `--execute` 一律在外部操作之前被平台契約檢查拒絕，沒有環境變數可解除。未進行任何測試買賣。

策略報告與通知獨立存於行情庫旁 `strategy.sqlite3`，JSON副本為 `reports/YYYY-MM-DD.weekly-v1.json`。同日重跑沿用首次封存的訊號和持股，不重新套用不同持股檔；要比较不同模擬輸入，使用不同目錄的 `--db` 隔離整組資料。正式訊號另使用 `weekly-v1-live` 鍵，不重用模擬封存；`strategy-preview --live` 可離線讀取其委託狀態。策略庫綁定交易帳戶，不能切換帳戶混用歷史紀錄。保持行情庫程序鎖、strategy-run.lock與共用GPU鎖；與每日工作重疊時退出，不同時推論。Discord沿用sending／unknown防重送。

委託引擎已用替身驗證：POST前提交sending，受理記accepted，逾時／不明結果記unknown並停止；accepted不代表成交，不以查無持股推定單據過期。未結委託阻擋跨週執行，禁止自動撤銷、刪除紀錄或重送。正式API仍需核對非空持股欄位及張數、可賣量、拒絕／資金不足回應、委託效期與成交／撤單終態查詢，補齊平台解析及核對測試，再解除正式執行入口的契約封鎖。現階段完整提供訊號與調倉模擬、狀態機測試，尚非可啟用的自動交易系統。

憑證統一根目錄 `.env`，不輸出內容；舊 `data/discord.env` 僅作未讀取的遷移備份，仍維持Git忽略。調整根目錄檔案權限為600。19:00排程範本見SCHEDULE，尚未安裝或啟用。

## 2026-09-22一次性四交易日補跑

使用者另行明示授權本次例外：模型只讀截至9/18的120根日K，預測9/21～9/24四個交易日（9/25休市），排名使用D4收盤／D1開盤−1；9/21資料只用於買單限價，不進入模型。Top10且分數>1%，每檔10張，缺9/21收盤價略過且不以第11名遞補。

```bash
# 準備全市場訊號及買單，沒有外部委託或通知
.venv/bin/python -m src.main strategy-catchup
# 本次已授權的實際模擬平台委託與Discord通知，僅2026-09-22可用
.venv/bin/python -m src.main strategy-catchup --execute --send
# 離線預覽，不重新推論或送單
.venv/bin/python -m src.main strategy-catchup --preview
```

執行前重新查詢空倉；非空且格式未確認即停止。沿用strategy.sqlite3與共用鎖，報告鍵為catchup-20260918-four-sessions，價格日期與執行日期另存。任何其他既存委託阻擋新建倉。提交前持久化整輪started及逐筆sending；任何拒絕、不明結果或程序中斷都不在重跑時續送剩餘買單，accepted僅代表受理。Discord包含訊號日期、限價日期、實際委託狀態，採獨立通知鍵防重送；本次一般API錯誤尚無細分契約，非明確成功一律按unknown停止。

本次例外不解除一般strategy --execute的契約限制，不啟用19:00策略timer，不允許任意日期補單。新入口不接受變更日期或張數；隔日只能預覽，禁止新委託。原始平台回應去機密後保留於委託庫，勿將結果不明的單據刪除重送。
