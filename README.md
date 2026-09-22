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

1. 複製 `config.example.json` 為 `config.json`（這個檔案含 webhook 跟 API 金鑰，已加入
   `.gitignore`，不會被推到公開 repo）。
2. `webhook_url`：Discord 頻道「編輯頻道 → 整合 → Webhook」取得。
3. `steam_api_key`：去 https://steamcommunity.com/dev/apikey 用你的 Steam 帳號申請一組（免費，
   網域名稱欄位隨便填）。抓資料用的是 Steam 官方 Web API，這組金鑰是必填的。
4. `site_url`：你的 GitHub Pages 網址，會放進 Discord 通知裡。
5. `language` / `country`：Steam 商店語言與地區（連遊戲名稱、標籤翻譯都會跟著變）。
6. `retention_days`：網站保留幾天的歷史紀錄（預設 30）。
7. `backfill_days`：第一次執行時回填過去幾天的清單（預設 30）。
8. `git_auto_push`：`true` 時每次執行後自動 `git commit + push` 更新公開網站；不想自動推的話
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

資料來源是 Steam 官方但沒公開文件化的 Web API（`IStoreQueryService`、`IStoreBrowseService`、
`IStoreService`，用 [xPaw 的整理文件](https://steamapi.xpaw.me/) 找到的），不是爬網頁 HTML：

1. `IStoreService/GetTagList` 先抓一次完整的標籤 ID → 名稱對照表。
2. `IStoreQueryService/Query` 用 `release_date_filter` 直接查某個日期範圍內、`steam_release_date`
   落在裡面的所有遊戲，一次拿到名稱、圖片、價格、折扣、標籤、上架時間（都是結構化欄位，不用
   再解析文字或猜格式）。日期用**美國西岸時間**認定（Steam 自己判定「上架日」就是用這個時區，
   跟你商店頁面看到的日期一致，不是用你電腦的時區）。
3. 每次執行都會順便檢查「記錄裡特價已經過期」的遊戲，用 `IStoreBrowseService/GetItems`（一次最
   多查 50 款）重新確認目前狀態，過期就更新回正常價格。

這個 API 不會套用 Steam 網頁搜尋那層「僅限成人內容需要登入帳號才看得到」的過濾，所以連那類分級
的遊戲也抓得到（前提是你知道要查哪個日期範圍——這個 API 本身沒有這層限制，跟帳號登入無關）。

已知小狀況：這個 API 是未公開文件化的，分頁在筆數剛好卡在頁尾（第 100 筆左右）時偶爾會不穩定，
極少數情況下重跑會抓到、下次不一定抓得到同一款遊戲。目前沒有完美解法，如果發現某天的清單好像
少了一款，重新跑一次 `--backfill` 通常就會補上。

處理完的清單：

- 寫入 `history.json`（本機，不進版控），保留 `retention_days` 天。
- 用 `docs/` 重新產生整個靜態網站（首頁、每日頁面、標籤頁面、搜尋頁），並自動 push 到 GitHub
  讓 GitHub Pages 更新。
- 對照 `state.json` 找出「這次新出現、之前沒通知過的遊戲」，有的話才發 Discord 訊息。

## 檔案說明

- `steam_new_releases.py` — 主程式
- `config.json` — 你的私人設定（webhook、語言…），**不進版控**
- `history.json` — 歷史資料快取，**不進版控**（網站內容從這裡產生）
- `state.json` — 已通知過的 App ID 記錄，避免重複推播，**不進版控**
- `docs/` — 產生出來的靜態網站，**會進版控**，GitHub Pages 直接從這個資料夾發布
