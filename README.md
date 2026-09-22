# Steam 新遊戲每日通知

每小時自動抓取 Steam 當天新上架 / 預計上架的遊戲、更新一個可公開瀏覽的歷史紀錄網站
（GitHub Pages），每天早上 6:00（台北時間）彙整當天累積的新遊戲發一則 Discord 通知。
全部跑在 GitHub Actions 上，不需要你的電腦開機。

- 公開網站：https://w2715456899-sketch.github.io/steam-new-releases/
- 原始碼／自動化腳本：https://github.com/w2715456899-sketch/steam-new-releases

## 平常怎麼用

- **看新遊戲**：直接開公開網站，首頁固定是「今天」。上面有「← 前一天 / 所有日期 / 後一天 →」
  可以翻歷史（保留最近 30 天），點左上角貓咪 logo 隨時回首頁。
- **看某個標籤的遊戲**：點任何一款遊戲下面的標籤（例如「Rogue」），會列出目前紀錄裡所有有這個
  標籤的遊戲，不限日期。
- **分享給朋友**：把上面那個網站網址傳給他們就好，不需要帳號、不需要裝東西。
- **Discord 通知**：每天早上 6:00（台北時間）收到一則「日期 新遊戲來了 🫠」+ 網站連結，內容是
  當天累積到目前為止的新遊戲；當天沒有新遊戲就不會發訊息（安靜不打擾）。
- **自動更新**：GitHub Actions 每小時執行一次（`.github/workflows/update.yml`），抓資料 →
  更新網站 → commit + push——但只有早上 6:00 那次會真的發 Discord，其他 23 次都是安靜更新
  網站內容，讓朋友隨時開網站看到的都是新的。全程在 GitHub 的伺服器上跑，你的電腦不用開機、
  不用裝任何東西。

## GitHub Actions 設定（自動排程用，已經設定好，這節是給以後想改的時候看）

排程設定在 `.github/workflows/update.yml`，執行時所需的密鑰放在 repo 的 GitHub Secrets：

1. GitHub repo 頁面 → **Settings → Secrets and variables → Actions → New repository secret**
2. 新增 `STEAM_API_KEY`：你的 Steam Web API 金鑰
   （https://steamcommunity.com/dev/apikey 申請）。
3. 新增 `DISCORD_WEBHOOK_URL`：Discord 頻道「編輯頻道 → 整合 → Webhook」取得的網址。
4. 存好之後不用做任何事，`update.yml` 每小時會自動觸發（`cron: "0 * * * *"`），也可以到
   repo 的 **Actions** 分頁手動點 **Run workflow** 立即測試一次。
5. `language`、`country`、`site_url`、`retention_days` 等其他設定值直接寫在 `update.yml`
   裡（不是密碼，不需要放 Secrets），要改就直接編輯那個檔案。

## 本機測試（不影響正式排程，僅供除錯用）

```
python -m pip install -r requirements.txt
```

複製 `config.example.json` 為 `config.json`，填入 `webhook_url` 跟 `steam_api_key`（這個檔案
已加入 `.gitignore`，不會被推到公開 repo），然後：

```
python steam_new_releases.py --dry-run --debug
```

不會真的發 Discord、不會寫入任何檔案，只印出這次會抓到什麼。確認沒問題後可以直接執行
`python steam_new_releases.py` 跑一次真的（會發 Discord、push 到 GitHub）；加
`--skip-notify` 則會更新資料跟網站但不發 Discord、不標記已通知（跟 GitHub Actions 的
「安靜更新」那幾次行為一樣）。

## 運作方式

資料來源是 Steam 官方但沒公開文件化的 Web API（`IStoreQueryService`、`IStoreBrowseService`、
`IStoreService`，用 [xPaw 的整理文件](https://steamapi.xpaw.me/) 找到的），不是爬網頁 HTML：

1. `IStoreService/GetTagList` 先抓一次完整的標籤 ID → 名稱對照表。
2. `IStoreQueryService/Query` 用 `release_date_filter` 直接查某個日期範圍內、`steam_release_date`
   落在裡面的所有遊戲，一次拿到名稱、圖片、價格、折扣、標籤、上架時間（都是結構化欄位，不用
   再解析文字或猜格式）。日期統一用**台北時間**認定，跟預計上架的倒數徽章時間一致；Steam 自己
   商店頁面顯示的日期是用美國西岸時間算的，兩者在美西／台北換日的那 15-16 小時之間可能會差一
   天——這是刻意的取捨（網站內部一致優先於跟 Steam 官方頁面逐字對齊）。
3. 每次執行都會順便檢查「記錄裡特價已經過期」的遊戲，用 `IStoreBrowseService/GetItems`（一次最
   多查 50 款）重新確認目前狀態，過期就更新回正常價格。

這個 API 不會套用 Steam 網頁搜尋那層「僅限成人內容需要登入帳號才看得到」的過濾，所以連那類分級
的遊戲也抓得到（前提是你知道要查哪個日期範圍——這個 API 本身沒有這層限制，跟帳號登入無關）。

已知小狀況：這個 API 是未公開文件化的，分頁在筆數剛好卡在頁尾（第 100 筆左右）時偶爾會不穩定，
極少數情況下重跑會抓到、下次不一定抓得到同一款遊戲。目前沒有完美解法，如果發現某天的清單好像
少了一款，重新跑一次 `--backfill` 通常就會補上。

處理完的清單：

- 寫入 `history.json`，保留 `retention_days` 天。
- 用 `docs/` 重新產生整個靜態網站（首頁、每日頁面、標籤頁面、搜尋頁）。
- 對照 `state.json` 找出「這次新出現、之前沒通知過的遊戲」；只有早上 6:00 那次執行（沒帶
  `--skip-notify`）才會真的發 Discord、把這些標記成已通知——平常每小時的安靜更新只會更新
  `history.json`/網站，不動 `state.json`，所以累積一整天的新遊戲都會在早上那次一次發出。
- GitHub Actions 每次執行完會把 `history.json`、`state.json`、`docs/` 一起 commit + push。

## 檔案說明

- `steam_new_releases.py` — 主程式
- `.github/workflows/update.yml` — GitHub Actions 排程設定
- `config.json` — 本機測試用的私人設定（webhook、API 金鑰…），**不進版控**；正式排程的密鑰放
  在 GitHub Secrets，不是這個檔案
- `history.json` — 歷史資料，**會進版控**（只是遊戲清單，沒有敏感資訊）
- `state.json` — 已通知過的 App ID 記錄，避免重複推播，**會進版控**
- `docs/` — 產生出來的靜態網站，**會進版控**，GitHub Pages 直接從這個資料夾發布
