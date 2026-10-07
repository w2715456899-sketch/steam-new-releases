# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**Steam美食家**：個人用的 Steam 新遊戲追蹤站。單一 Python 腳本 `steam_new_releases.py` 用 Steam
的非公開 Web API 抓每日新上架遊戲，每天發一則 Discord 通知，並產生靜態網站。

- 正式站：https://w2715456899-sketch.github.io/steam-new-releases/
- Repo：https://github.com/w2715456899-sketch/steam-new-releases
- 沒有伺服器、沒有資料庫：全部跑在 GitHub Actions（`.github/workflows/update.yml`）上，發佈到
  GitHub Pages（`docs/`）。

**跟同一個 Downloads 資料夾下的 `stock_ai` / `股票分析` 專案完全無關**——分開的專案、
分開的技術棧，不要把股票分析那邊的邏輯或慣例帶過來。

## 架構

- `steam_new_releases.py`：抓資料、更新 `history.json`、產生 `docs/` 全站、發 Discord，全在這支。
  HTML/CSS/JS 都是檔案內的字串模板（`PAGE_SHELL`、`STYLE_CSS`、`HISTORICAL_LOW_SCRIPT`，
  用 `__PLACEHOLDER__` 取代），CSS 用內容 hash 做快取更新。
- `history.json`：資料本體。`games` 只保留最近 30 天上架的遊戲（`retention_days`）；
  `historical_low` 是歷史新低清單，跟 `games` 完全獨立。
- `docs/`：產生出來的網站，**不要手改**，改程式後重新產生。
- 時區一律用台北（`LOCAL_TZ`）。Steam 商店頁用美西時間，所以有些遊戲網站日期會比 Steam 晚一天，
  這是刻意的取捨，不是 bug。

### 每小時更新（GitHub Actions）

- 每小時整點跑 `python steam_new_releases.py --no-backfill --skip-notify`（只更新資料和網站）；
  UTC 22:00（台北 06:00）那次不加 `--skip-notify`，會發 Discord。
- 金鑰存在 GitHub Secrets：`DISCORD_WEBHOOK_URL`、`STEAM_API_KEY`、`ITAD_API_KEY`，
  workflow 執行時寫成 `config.json`。
- 跑完 commit `docs/` 和 `history.json` 推回 `main`，所以 **`main` 大約每小時會自己前進**。
- 連續失敗 3 次才發 Discord 警告。
- **不要改 workflow 的排程/設定，除非使用者明確要求。**

### 歷史新低（`fetch_historical_lows`）

資料來源是 IsThereAnyDeal（ITAD）API，不是從 30 天新遊戲裡挑：

1. `POST /deals/v2`（金鑰放 `ITAD-API-Key` header；POST 版不吃 query 參數），filter：
   `flag: "N"`（創新低，等同 SteamDB 的藍色標記）、`type: [1]`（遊戲）、上架滿 30 天、
   Steam 評論 ≥ 1000。
2. `POST /lookup/shop/61/id/v1` 把 ITAD id 換成 Steam appid（61 = Steam；只有 `sub/` 的略過）。
3. Steam `IStoreBrowseService/GetItems` 補齊顯示資料，並且**Steam 上必須真的有折扣**才收。
4. ITAD 失敗就回傳 `None`，保留上一輪清單，不會清空頁面。

ITAD filter 的坑：range 類（如 `steamCount`）`min`/`max` 都要給，只給一個會被**靜默忽略**；
`releaseDate` 的 `min` 給 `null` 會 HTTP 500。SteamDB 沒有公開 API 且禁止爬取，不要用。

歷史新低頁：評論數多的排前面、每頁 15 款、上方標籤篩選（前 7 個，「…」每次多展開 20 個）。
這頁的遊戲標籤是篩選按鈕（`tag_links=False`），因為舊遊戲的標籤不一定有對應的標籤頁。

## 指令

```bash
pip install -r requirements.txt
python -W error -m py_compile steam_new_releases.py   # 改完先做語法檢查
```

- 執行腳本需要 `config.json`（從 `config.example.json` 複製，**已 gitignore，不在 repo 裡**）。
  沒有金鑰的環境（例如雲端工作階段）跑不了完整腳本。
- 只重新產生網站不需要金鑰：

```python
import json; from pathlib import Path; import steam_new_releases as s
h = s.load_history(Path("history.json"))
s.generate_site(h, Path("docs"), 30, s.local_today(), "https://w2715456899-sketch.github.io/steam-new-releases/")
```

- 本機 `config.json` 若 `git_auto_push: true`，直接跑腳本會**自己 commit+push `docs/`**。
  只想預覽時，請像上面那樣直接呼叫 `generate_site` 輸出到暫存資料夾。

## 部署流程（推上 main 之前）

因為 Actions 每小時會推 commit：

1. `git fetch origin`，看 `git log HEAD..origin/main`。
2. 遠端有新 commit：`history.json` / `docs/` 是產生物，衝突時取遠端版本，程式碼改動先 stash，
   `git merge --ff-only origin/main` 後 pop。**不要手動解 HTML 裡的衝突標記。**
3. 在同步後的 `history.json` 上重新產生 `docs/`。
4. `py_compile` 檢查 → commit → 再 fetch 一次 → push。

## 跟使用者合作的習慣

- 使用者用繁體中文溝通，回覆也用繁體中文。
- **新功能或畫面改動：先給預覽，使用者說沒問題才推上正式站。**
- 預設用靜態方式驗證（重新產生網站、檢查產出的 HTML）。**除非使用者明確要求，不要開瀏覽器
  截圖測試**，測試了也不用把截圖貼回來。
- 不碰 Steam 帳號登入/密碼；金鑰只用使用者自己貼的，不要在回覆裡重複金鑰內容。
