# Steam 新遊戲每日通知

每天自動抓取 Steam 當天新上架的遊戲，推播到 Discord 頻道。

## 安裝

```
python -m pip install -r requirements.txt
```

## 設定

1. 複製 `config.example.json` 為 `config.json`。
2. 到你的 Discord 頻道「編輯頻道 → 整合 → Webhook → 新增 Webhook」，複製 Webhook URL，
   貼到 `config.json` 的 `webhook_url`。
3. 依需求調整：
   - `language` / `country`：Steam 商店語言與地區（影響顯示語言、價格幣別）。
   - `max_pages`：安全上限，避免異常情況下無限翻頁（每頁 50 筆）。

`config.json` 和 `state.json`（已通知過的遊戲記錄，避免重複推播）已加入 `.gitignore`，
不會被提交進版本控制。

## 測試

先用 `--dry-run` 確認能抓到資料，且不會真的發到 Discord：

```
python steam_new_releases.py --dry-run --debug
```

確認沒問題後，正式執行（會發送 Discord 通知並寫入 state.json）：

```
python steam_new_releases.py
```

## 設定每天自動執行（Windows 工作排程器）

用系統管理員權限開啟 PowerShell 或 cmd，執行（依你環境的 python 路徑調整）：

```
schtasks /create /tn "SteamNewReleases" ^
  /tr "\"C:\Users\Kuro\AppData\Local\Python\bin\python.exe\" \"C:\Users\Kuro\Downloads\stock_ai\Steam_search\steam_new_releases.py\"" ^
  /sc daily /st 06:00
```

- `/st 06:00`：每天早上 6:00 執行，可依喜好調整。
- 也可以用「工作排程器」GUI 建立：動作填 python.exe 路徑，引數填腳本完整路徑，
  觸發程序設定「每天」。

移除排程：

```
schtasks /delete /tn "SteamNewReleases" /f
```

手動立即測試排程是否正常：

```
schtasks /run /tn "SteamNewReleases"
```

## 運作方式

每次執行會合併兩份清單，湊出「今天」完整的新遊戲名單：

1. **已上架**：`sort_by=Released_DESC`（新到舊）+ `category1=998`（僅遊戲），
   抓已經正式上架、日期為今天的遊戲。
2. **預計今天上架**：`filter=comingsoon` + `sort_by=Released_ASC`（舊到新），
   抓 Steam 頁面已排定日期、但還沒正式解鎖的遊戲，篩出日期等於今天的部分。

兩份清單依 App ID 去重合併，footer 會標示 `✅ 已上架` 或 `⏳ 預計今天上架（可能延期）`——
後者是開發者預先填的日期，實際上架時間仍可能臨時異動或延期。

用 `state.json` 記錄已通知過的 App ID（保留 14 天），重複執行不會重複推播。
