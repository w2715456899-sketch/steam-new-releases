# Steam 新遊戲每日通知

每天自動抓取 Steam 當天新上架 / 預計上架的遊戲，發一則 Discord 通知，並維護一個可公開瀏覽的
歷史紀錄網站（GitHub Pages）。

- 公開網站：https://w2715456899-sketch.github.io/steam-new-releases/
- 原始碼／自動化腳本：https://github.com/w2715456899-sketch/steam-new-releases

## 平常怎麼用

- **看新遊戲**：直接開公開網站，首頁固定是「今天」。上面有「← 前一天 / 所有日期 / 後一天 →」
  可以翻歷史（保留最近 30 天），點左上角貓咪 logo 隨時回首頁。
- **看某個標籤的遊戲**：點任何一款遊戲下面的標籤（例如「Rogue」），會列出目前紀錄裡所有有這個
  標籤的遊戲，不限日期。
- **分享給朋友**：把上面那個網站網址傳給他們就好，不需要帳號、不需要裝東西。
- **Discord 通知**：每天有新遊戲時會收到一則「📅 日期 新遊戲來了 🫠」+ 網站連結；當天沒有新
  遊戲就不會發訊息（安靜不打擾）。
- **自動更新**：每天早上 6:00 電腦會自動跑一次（Windows 工作排程器 `SteamNewReleases`），
  抓資料 → 更新網站 → 自動 commit + push 到 GitHub → 觸發 Discord 通知，全程不用手動做任何事。
  電腦當天沒開機/沒跑，就是少那一天的資料，之後開機重新跑一次 `python steam_new_releases.py`
  即可補上（沒抓到的日子不會自動回溯，只有第一次的 30 天回填是例外）。

## 安裝（僅供你自己維護用，日常不需要）

```
python -m pip install -r requirements.txt
```

## 設定

1. 複製 `config.example.json` 為 `config.json`（這個檔案含 webhook，已加入 `.gitignore`，
   不會被推到公開 repo）。
2. `webhook_url`：Discord 頻道「編輯頻道 → 整合 → Webhook」取得。
3. `site_url`：你的 GitHub Pages 網址，會放進 Discord 通知裡。
4. `language` / `country`：Steam 商店語言與地區（連遊戲名稱、標籤翻譯都會跟著變）。
5. `retention_days`：網站保留幾天的歷史紀錄（預設 30）。
6. `backfill_days` / `backfill_max_pages`：第一次執行時回填過去幾天的已上架清單。
7. `git_auto_push`：`true` 時每次執行後自動 `git commit + push` 更新公開網站；不想自動推的話
   設 `false`，改成自己手動 `git push`。

## 測試

```
python steam_new_releases.py --dry-run --debug
```

不會真的發 Discord、不會寫入任何檔案，只印出這次會抓到什麼。確認沒問題後直接執行：

```
python steam_new_releases.py
```

## Windows 工作排程器

已建立每天 06:00 自動執行的排程，指令參考：

```
schtasks /create /tn "SteamNewReleases" ^
  /tr "\"C:\Users\Kuro\AppData\Local\Python\bin\python.exe\" \"C:\Users\Kuro\Downloads\stock_ai\Steam_search\steam_new_releases.py\"" ^
  /sc daily /st 06:00
```

常用指令：

```
schtasks /run /tn "SteamNewReleases"      # 手動立即跑一次排程
schtasks /query /tn "SteamNewReleases"    # 查看排程狀態
schtasks /delete /tn "SteamNewReleases" /f  # 移除排程
```

## 運作方式

每次執行會合併兩份清單，湊出「今天」完整的新遊戲名單：

1. **已上架**：`sort_by=Released_DESC`（新到舊）+ `category1=998`（僅遊戲），抓已經正式
   上架、日期為今天的遊戲。
2. **預計今天上架**：`filter=comingsoon` + `sort_by=Released_ASC`（舊到新），抓 Steam 頁面
   已排定日期、但還沒正式解鎖的遊戲，篩出日期等於今天的部分。額外多抓一次每款遊戲自己的商店
   頁面，取得精確解鎖時間（非官方欄位，抓不到就顯示「預計上架」，不影響其他功能）與熱門標籤。

兩份清單依 App ID 去重合併後：

- 寫入 `history.json`（本機，不進版控），保留 `retention_days` 天。
- 用 `docs/` 重新產生整個靜態網站（首頁、每日頁面、標籤頁面），並自動 push 到 GitHub 讓
  GitHub Pages 更新。
- 對照 `state.json` 找出「這次新出現、之前沒通知過的遊戲」，有的話才發 Discord 訊息。

## 檔案說明

- `steam_new_releases.py` — 主程式
- `config.json` — 你的私人設定（webhook、語言…），**不進版控**
- `history.json` — 歷史資料快取，**不進版控**（網站內容從這裡產生）
- `state.json` — 已通知過的 App ID 記錄，避免重複推播，**不進版控**
- `docs/` — 產生出來的靜態網站，**會進版控**，GitHub Pages 直接從這個資料夾發布
