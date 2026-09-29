# 排程設定與管理紀錄

本文件記錄專案的 systemd 使用者级排程。設定原始檔位於 `deploy/`；實際執行紀錄保存在 systemd journal，不是根目錄的文字日誌。

## 建立紀錄

2026-09-11 已在使用者 `ciot` 的 systemd 中安裝並啟用 `kronos-daily.timer`。建立後核對狀態為 `enabled`，當時下一次觸發為 **2026-09-11 18:00 Asia/Taipei**；此處為安裝紀錄，最新狀態請用下方指令查詢。主機的該使用者已啟用 `Linger=yes`，登出後仍可執行。

| 項目 | 設定 |
| --- | --- |
| 排程機制 | systemd 使用者服務，非 cron |
| 觸發時間 | 每日台灣時間 18:00；`OnCalendar=*-*-* 18:00:00 Asia/Taipei` |
| Timer 原始檔 | [deploy/kronos-daily.timer](deploy/kronos-daily.timer) |
| Service 原始檔 | [deploy/kronos-daily.service](deploy/kronos-daily.service) |
| 告警單元 | [deploy/kronos-failure-notify.service](deploy/kronos-failure-notify.service) |
| 安裝位置 | `/home/ciot/.config/systemd/user/`，符號連結指向專案 `deploy/` 檔案 |
| 工作目錄 | `/home/large_space_v2/kronos_trading_bot` |
| 執行指令 | `/home/large_space_v2/kronos_trading_bot/.venv/bin/python -m src.main daily` |
| 憑證設定 | 根目錄 `.env`，權限 600、Git 忽略；不在本文件記錄網址或 token |
| 失敗重啟 | 間隔5分鐘；23小時內最多3次啟動，耗盡後發 Discord 告警 |
| 單次執行上限 | `TimeoutStartSec=12h` |
| 關機補跑 | `Persistent=false`，錯過時間不補跑 |

## 每次執行行為

程式確認交易日後，更新平台可交易清單及交易所日 K，使用本機 Kronos 預測，保存完整結果，再發送正漲幅前20名至 Discord；不足20檔照實呈現。18:00是開始時間，通知於處理完成後送出。

休市日不發排行榜；官方日曆缺少的已確認臨時休市記錄於 [config/market_closures.json](config/market_closures.json)。機器必須開機且能連網。初始化與每日工作共用資料庫旁的程序鎖，重疊執行會退出並留下錯誤。暫時性行情 GET 最多重試五次；service 失敗後最多再啟動兩次，已完成的市場日快取不會重抓。

報告位於 `data/reports/YYYY-MM-DD.json`，資料庫位於 `data/market.sqlite3`。通知成功後同日不重送；送達狀態不明時須先人工核對 Discord。自動重啟使用 `RestartMode=direct`，中途失敗不觸發 `OnFailure`；三次啟動均失敗才用原 webhook 發送無 mention 告警。Discord 故障時告警也可能失敗，journal 仍保留紀錄。詳見 [README](README.md#怎麼使用)。

## 安裝與設定更新

以下是本機採用的連結式安裝方式；部署到其他路徑須先修改 service 的絕對路徑，並備妥憑證及歷史快取。使用實際執行排程的使用者操作，不加 `sudo`。

```bash
systemctl --user link /home/large_space_v2/kronos_trading_bot/deploy/kronos-daily.service /home/large_space_v2/kronos_trading_bot/deploy/kronos-daily.timer /home/large_space_v2/kronos_trading_bot/deploy/kronos-failure-notify.service
systemctl --user daemon-reload
systemctl --user enable --now kronos-daily.timer
```

修改 `deploy/` 中的 service 或 timer 後重新載入；修改觸發時間另須重啟 timer：

```bash
systemctl --user daemon-reload
systemctl --user restart kronos-daily.timer
```

只修改 Python 程式不需要重啟 timer，下次工作會使用新版程式。只有複製式安裝才需另將更新後的設定檔複製至 systemd 目錄。

## 狀態與執行紀錄

```bash
# 啟用狀態、下次執行時間與服務結果
systemctl --user is-enabled kronos-daily.timer
systemctl --user list-timers kronos-daily.timer --no-pager
systemctl --user status kronos-daily.service --no-pager

# 最近50筆、今日紀錄與即時追蹤
journalctl --user -u kronos-daily.service -n 50 --no-pager
journalctl --user -u kronos-daily.service --since today --no-pager
journalctl --user -u kronos-daily.service -f
journalctl --user -u kronos-failure-notify.service -n 20 --no-pager

# 匯出今日執行紀錄至專案已忽略的data目錄（在專案根目錄執行）
journalctl --user -u kronos-daily.service --since today --no-pager > data/schedule.log

# 確認登出後的排程支援
loginctl show-user ciot -p Linger
```

`Type=oneshot` 工作完成後顯示 `inactive (dead)` 可以是正常結果，須搭配退出狀態與日誌判斷；負責等待下次時間的是 timer。

## 手動觸發與停用

手動啟動會使用 service 的憑證設定，可能實際發送 Discord；程式仍檢查台北18:00界線與交易日。若只是查看已保存結果，使用預覽命令。

```bash
# 實際執行一次每日工作
systemctl --user start kronos-daily.service

# 離線預覽，不發訊息
.venv/bin/python -m src.main preview

# 停止未來排程；不會終止已開始的工作
systemctl --user disable --now kronos-daily.timer

# 如需同時中止執行中的工作
systemctl --user stop kronos-daily.service

# 恢復每日排程
systemctl --user enable --now kronos-daily.timer
```

## 研究工作與雙模型通知（2026-09-15）

既有18:00 timer不變。登錄models/taiwan-active.json後，daily改為原版、微調版各一封，部分失敗仍保留成功報告與送達狀態，重試只補未完成工作；告警改為「部分或全部模型未完成」。未登錄前仍是原版單封。

prepare使用獨立研究資料庫及research.lock，不搶每日market.lock。daily／train／evaluate共用data/gpu.lock；train／evaluate在17:45–20:00停止研究GPU工作，訓練保存可續跑進度，評估保留逐筆快取，20:00後以原命令續跑（train加--resume）。程序崩潰時鎖會由系統釋放。沒有新增每月重訓timer。

訓練不應由kronos-daily.service啟動。研究命令與狀態檔詳見 [研究文件](docs/research.md)；不因測試或訓練額外發Discord。資料日／模型版本分開防重送，sending／unknown仍需人工核對送達情況。

本次一次性研究已於2026-09-15以systemd-run啟動`kronos-research-20260914.service`，Nice=10，日誌為`data/research-five-years.log`；不含Webhook環境。研究入口遇GPU保留時段會等待並續跑，其他錯誤停止，狀態保存於`data/research/taiwan-20260914/pipeline.json`。此臨時service不會隨開機自動恢復，重開後用 [研究文件](docs/research.md) 的 research_run 命令續跑。

```bash
systemctl --user status kronos-research-20260914.service --no-pager
tail -n 30 data/research-five-years.log
# 中止本次研究，不影響每日通知timer
systemctl --user stop kronos-research-20260914.service
```


## GPU加速續跑（2026-09-16）

本次測速由kronos-gpu-benchmark-now-20260916.service於使用者授權的當日例外時段執行，19:07成功完成並套用FP16／batch128／累積1。舊研究kronos-research-20260914及原等待服務kronos-gpu-tune-20260916已停止；17:45checkpoint備份於models/taiwan-v1/resume-before-tuning.pt，未保存進度會重算。

目前續跑服務為 `kronos-research-accelerated-20260916.service`，沿用原資料、模型及 `data/research-five-years.log`，19:07核對為等待20:00保留時段結束。每日timer未變更，測速不發Discord；`--allow-reserved-window-today`只作用於當日benchmark，隔日自動失效。這是一次性使用者服務，不會隨開機自動恢復。

```bash
systemctl --user status kronos-research-accelerated-20260916.service --no-pager
tail -n 30 data/research-five-years.log
```

新版可先建立 `models/taiwan-v1/pause.request`，待checkpoint保存、研究狀態paused後再停止service；直接stop可能捨棄最近五分鐘未保存訓練，驗證中斷則重做該輪驗證。移除pause.request後才能續跑。加速設定失敗時研究入口回退一次到training-fallback.json，若仍失敗便停止。測速報告在data/gpu-benchmark-20260916/，交接日誌data/gpu-tune-20260916.log。

### 128筆批次評估切換（2026-09-16）

已停止舊`kronos-research-accelerated-20260916.service`以切換批次评估；新版一次性背景服務命名為`kronos-research-batch128-20260916.service`，沿用research_run、原資料與輸出目錄、data/research-five-years.log，已完成訓練不重跑。使用獨立批次評估快取重算兩模型；每日17:45–20:00保留、GPU鎖與完成後activate行為不變。查詢時使用新服務名。


## 五日策略19:00範本（2026-09-22，未啟用）

`deploy/kronos-strategy.timer` 每日19:00觸發，由程式依日曆判斷當週最後交易日；不是固定週五，也不是每五個交易日。`Persistent=false`不補跑錯過日期。搭配的service使用 `strategy --execute --send`，正式契約未確認會立即失敗；目前只提供範本，未安裝、未啟用，不會自動委託或通知。

正式啟用前須完成 [策略文件](docs/strategy.md) 列出的平台契約、持股解析、委託終態核對與平台整合測試。不得僅移除檢查或以設定開關繞過。service不自動重啟，避免含交易副作用的盲目重試。行情鎖及GPU鎖與每日工作共用；若18:00工作尚未完成，策略退出並留journal紀錄，不搶占GPU、不改研究保留時段。

所有服務的EnvironmentFile改讀根目錄 `.env`，現有每日timer的18:00時間與通知防重送不變；Python CLI也讀同檔，外部環境值優先。`data/discord.env`保留為未讀取的遷移備份，不再維護兩份設定。

本次已重新載入既有每日／告警服務並核對EnvironmentFile，未啟動服務，原每日timer保持enabled。新增策略timer未安裝或啟用。離線驗證79項主流程及6項fetcher測試通過，systemd單元驗證通過。

## 2026-09-22一次性補跑例外

使用者明示授權手動strategy-catchup：截至9/18的120日訊號，預測9/21～9/24，使用9/21收盤價買入並通知Discord。仅允許9/22執行；使用行情、策略及GPU鎖，不建立或啟用timer，也不修改原每日18:00工作。一般strategy --execute仍停用。此一次性例外不沿用至下週或其他日期。

本次02:30已手動完成補跑，10筆買單皆受理（尚未核對成交），Discord已送達；沒有新增自動執行排程。再次查看使用strategy-catchup --preview，不刪除started／completed或accepted紀錄後重跑。
