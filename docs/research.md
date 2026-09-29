# 台股微調、評估與 GPU 測速

以下是從專案根目錄執行的一次性研究範例。先核對資料集、模型版本與當前進度；沒有週期性重訓排程。研究與測速不發 Discord。日常操作見 [README](../README.md)。

## 資料與訓練

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

`evaluation.json`提供各模型及共同有效商品的按日勝率、前20等權開收盤報酬、全池排名相關性與無效預測數。沒有正值候選時指標為空，不以零收益代替。報酬未扣成本，尚未假設模擬平台可按開收盤成交。

完整評估且兩模型皆有有效結果後，`--activate`原子寫入`models/taiwan-active.json`。績效較差仍可啟用作觀察。每日原版與微調版共用行情、相同120日與抽樣參數，依序使用GPU，各發一封前20通知並標明版本。尚未登錄微調版時維持原版單封；登錄損壞或一個模型失敗時，另一模型仍獨立執行，整體退出失敗供既有排程重試。

報告與通知採`(date, model_id)`鍵；舊紀錄自動遷移為`pretrained`，保留sent／sending／unknown與訊息ID，不補發歷史通知。原版JSON仍為`YYYY-MM-DD.json`；微調版為`YYYY-MM-DD.版本.json`。未來新版本使用新目錄及版本名，於下一個資料日啟用，避免同日觀察混入多個微調版本。

## 一次性背景流程

```bash
.venv/bin/python -u -m src.research_run --dataset data/research/taiwan-20260914 --output models/taiwan-v1 --cutoff 2026-09-14 --activate
```

這個入口依序prepare、顯存測試、train、evaluate；一般錯誤立即停止，僅讓出每日GPU時段（退出碼75）會等到20:00後自動續跑。完成訓練後重跑不重訓，而是續做評估；`--activate`不立即發通知。進度在研究目錄`pipeline.json`。訓練正常暫停與每輪結束均寫resume.pt；程序意外中止時回到最近checkpoint，若第一輪尚未保存則重新開始。

研究進度須核對 `pipeline.json`、`training.json`、`evaluation.json`、`models/taiwan-active.json` 及日誌；不能依舊服務紀錄推定目前狀態。歷史紀錄見 [專案交接](../PROJECT_CONTEXT.md)。

## GPU 批次測速與加速續跑

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

## 批次評估

`evaluate`預設每批128筆、FP32，不使用梯度累積。每個樣本有獨立固定抽樣種子；批次運算可能與逐筆推論產生數值差異。新快取為輸出目錄下`evaluation-batch128-v1.sqlite3`，舊`evaluation.sqlite3`保留但不混用。重跑相同evaluate命令可續接新快取，尾批不足128照實處理。批次推論故障停止，已提交批次保留；進度與速度每2560筆寫入日誌。
