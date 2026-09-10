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
| 安裝位置 | `/home/ciot/.config/systemd/user/`，符號連結指向專案 `deploy/` 檔案 |
| 工作目錄 | `/home/large_space_v2/kronos_trading_bot` |
| 執行指令 | `/home/large_space_v2/kronos_trading_bot/.venv/bin/python -m src.main daily` |
| 憑證設定 | `data/discord.env`，權限 600、Git 忽略；不在本文件記錄網址或 token |
| 單次執行上限 | `TimeoutStartSec=12h` |
| 關機補跑 | `Persistent=false`，錯過時間不補跑 |

## 每次執行行為

程式確認交易日後，更新平台可交易清單及交易所日 K，使用本機 Kronos 預測，保存完整結果，再發送正漲幅前20名至 Discord；不足20檔照實呈現。18:00是開始時間，通知於處理完成後送出。

休市日不發排行榜；官方日曆缺少的已確認臨時休市記錄於 [config/market_closures.json](config/market_closures.json)。機器必須開機且能連網。初始化與每日工作共用資料庫旁的程序鎖，重疊執行會退出並留下錯誤。程式內有有限次請求重試，但 service 未設定整次工作自動重新啟動。

報告位於 `data/reports/YYYY-MM-DD.json`，資料庫位於 `data/market.sqlite3`。通知成功後同日不重送；送達狀態不明時須先人工核對 Discord，詳見 [README](README.md#discord-與排程)。

## 安裝與設定更新

以下是本機採用的連結式安裝方式；部署到其他路徑須先修改 service 的絕對路徑，並備妥憑證及歷史快取。使用實際執行排程的使用者操作，不加 `sudo`。

```bash
systemctl --user link /home/large_space_v2/kronos_trading_bot/deploy/kronos-daily.service /home/large_space_v2/kronos_trading_bot/deploy/kronos-daily.timer
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
