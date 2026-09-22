"""Fetch today's newly released Steam games, post them to Discord, and build a browsable history site."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from html import escape as esc
from pathlib import Path
from typing import Iterable

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

BASE_DIR = Path(__file__).resolve().parent
SEARCH_URL = "https://store.steampowered.com/search/results/"
APP_PAGE_URL = "https://store.steampowered.com/app/{appid}/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) steam-new-releases-bot/1.0",
    "Accept-Language": "en-US,en;q=0.9",
}
AGE_GATE_COOKIES = {"birthtime": "0", "wants_mature_content": "1", "lastagecheckage": "1-January-1970"}
STALE_STREAK = 5  # consecutive out-of-window rows before we stop paging
STATE_RETENTION_DAYS = 14
DEFAULT_RETENTION_DAYS = 30
DEFAULT_BACKFILL_DAYS = 30

log = logging.getLogger("steam_new_releases")


@dataclass
class Game:
    appid: str
    name: str
    url: str
    release_date: date
    image: str
    status: str  # "live" (already on the store) or "upcoming" (scheduled, may still slip)
    price_pct: str | None = field(default=None)  # e.g. "-12%", only set when discounted
    price_original: str | None = field(default=None)  # struck-through price, only set when discounted
    price_final: str | None = field(default=None)  # the price to actually pay, or "免費"; None = unknown/TBD
    release_epoch: int | None = field(default=None)  # exact unlock time, only meaningful for "upcoming"
    tags: list[str] = field(default_factory=list)
    header_image: str | None = field(default=None)  # higher-res image for the hover zoom
    discount_end: str | None = field(default=None)  # e.g. "10 月 6 日截止", only from the app's own page


def load_config(path: Path) -> dict:
    if not path.exists():
        log.error("Config file not found: %s (copy config.example.json to config.json first)", path)
        sys.exit(1)
    config = json.loads(path.read_text(encoding="utf-8"))
    if not config.get("webhook_url"):
        log.error("config.json is missing 'webhook_url'")
        sys.exit(1)
    return config


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"notified": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict) -> None:
    cutoff = (date.today() - timedelta(days=STATE_RETENTION_DAYS)).isoformat()
    state["notified"] = {k: v for k, v in state["notified"].items() if v >= cutoff}
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def load_history(path: Path) -> dict:
    if not path.exists():
        return {"games": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def save_history(path: Path, history: dict, retention_days: int) -> None:
    cutoff = (date.today() - timedelta(days=retention_days)).isoformat()
    history["games"] = {aid: g for aid, g in history["games"].items() if g["release_date"] >= cutoff}
    path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")


def upsert_history(history: dict, games: Iterable[Game]) -> None:
    for g in games:
        existing = history["games"].get(g.appid, {})
        history["games"][g.appid] = {
            "appid": g.appid,
            "name": g.name,
            "url": g.url,
            "release_date": g.release_date.isoformat(),
            "image": g.image,
            "status": g.status,
            # Always overwrite (not sticky like tags/etc below): the search listing that
            # produced this Game always came from a fresh, successful fetch, so a None
            # here genuinely means "no price right now" (still TBD), not a failed request.
            "price_pct": g.price_pct,
            "price_original": g.price_original,
            "price_final": g.price_final,
            "release_epoch": g.release_epoch if g.release_epoch is not None else existing.get("release_epoch"),
            "tags": g.tags if g.tags else existing.get("tags", []),
            "header_image": g.header_image or existing.get("header_image"),
            "discount_end": g.discount_end or existing.get("discount_end"),
        }


def fetch_page(start: int, count: int, language: str, country: str, coming_soon: bool) -> dict:
    params = {
        "query": "",
        "start": start,
        "count": count,
        "dynamic_data": "",
        "sort_by": "Released_ASC" if coming_soon else "Released_DESC",
        "category1": "998",  # Games only (excludes DLC/soundtracks/software)
        "supportedlang": language,
        "l": language,  # supportedlang alone filters results but doesn't localize names/tags
        "cc": country,
        "infinite": 1,
    }
    if coming_soon:
        params["filter"] = "comingsoon"
    cookies = {**AGE_GATE_COOKIES, "Steam_Language": language}
    resp = requests.get(SEARCH_URL, params=params, headers=HEADERS, cookies=cookies, timeout=20)
    resp.raise_for_status()
    return resp.json()


@dataclass
class GameDetails:
    epoch: int | None = None
    tags: list[str] = field(default_factory=list)
    header_image: str | None = None
    discount_end: str | None = None


def fetch_game_details(appid: str, language: str) -> GameDetails:
    """Best-effort scrape of the exact unlock timestamp, tags, header image, and sale countdown.

    The search listing only gives a day-level date, the small capsule image, no tags, and no
    discount end date. Individual app pages embed an absolute unix-epoch release time inside a
    JSON blob used by an unrelated widget (not a documented API), so the epoch part is fragile
    and silently returns None if Steam changes that markup - callers must treat it as optional.
    The header image has a different content hash than the capsule image for the same appid
    (they're separately-uploaded files), so it can't be derived by editing the capsule URL - it
    has to be read off the page. Same for the discount countdown text ("新品優惠！10 月 6 日截
    止") - the search listing's price block has no expiry info at all.
    """
    cookies = {**AGE_GATE_COOKIES, "Steam_Language": language}
    try:
        resp = requests.get(
            APP_PAGE_URL.format(appid=appid), params={"l": language}, headers=HEADERS, cookies=cookies, timeout=15
        )
        resp.raise_for_status()
    except requests.RequestException:
        return GameDetails()

    html_text = resp.text
    epoch = None
    pattern = (
        r"release_date&quot;:&quot;(\d+)&quot;,&quot;appname&quot;:&quot;.*?&quot;,"
        rf"&quot;steamworks_appid&quot;:{re.escape(appid)}\b"
    )
    match = re.search(pattern, html_text)
    if match:
        epoch = int(match.group(1))

    soup = BeautifulSoup(html_text, "html.parser")
    tags: list[str] = []
    tag_block = soup.select_one(".glance_tags.popular_tags")
    if tag_block:
        tags = [a.get_text(strip=True) for a in tag_block.select("a.app_tag")]

    header_image = None
    img_el = soup.select_one("img.game_header_image_full") or soup.select_one(".game_header_image_ctn img")
    if img_el and img_el.get("src"):
        header_image = img_el["src"]

    discount_end = None
    countdown_el = soup.select_one(".game_purchase_discount_countdown")
    if countdown_el:
        discount_end = countdown_el.get_text(strip=True)

    return GameDetails(epoch=epoch, tags=tags, header_image=header_image, discount_end=discount_end)


CJK_DATE_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")


def parse_release_date(text: str) -> date | None:
    text = text.strip()
    if not text:
        return None
    # "2026年9月12日" is unambiguous once we read the 年/月/日 markers ourselves - dateutil's
    # fuzzy mode drops those CJK characters and is left guessing day-vs-month order from three
    # bare numbers, which silently swaps them for any day <= 12 (e.g. "9月12日" -> Dec 9th).
    m = CJK_DATE_RE.search(text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    try:
        return dateparser.parse(text, fuzzy=True).date()
    except (ValueError, OverflowError):
        return None


def parse_price(row) -> tuple[str | None, str | None, str | None]:
    """Returns (discount_pct, original_price, final_price); all None means unknown/TBD."""
    # Steam's markup here has changed over time (it's no longer a plain ".search_price"
    # element) - the price now lives in ".search_price_discount_combined", and is only
    # populated once Steam actually has a price for the app (still empty for most
    # not-yet-released games, which is a real "unknown", not a scraping failure).
    container = row.select_one(".search_price_discount_combined")
    if not container:
        return None, None, None
    final_el = container.select_one(".discount_final_price")
    if not final_el:
        return None, None, None
    final_text = final_el.get_text(strip=True)
    pct_el = container.select_one(".discount_pct")
    orig_el = container.select_one(".discount_original_price")
    if pct_el and orig_el:
        return pct_el.get_text(strip=True), orig_el.get_text(strip=True), final_text
    return None, None, final_text


def parse_rows(html_text: str, status: str) -> Iterable[Game]:
    soup = BeautifulSoup(html_text, "html.parser")
    for row in soup.select("a.search_result_row"):
        appid = row.get("data-ds-appid")
        if not appid:
            continue  # bundles / packages have no single appid
        name_el = row.select_one(".title")
        release_el = row.select_one(".search_released")
        img_el = row.select_one("img")
        release_date = parse_release_date(release_el.get_text() if release_el else "")
        if release_date is None:
            continue
        pct, original, final = parse_price(row)
        yield Game(
            appid=appid.split(",")[0],
            name=name_el.get_text(strip=True) if name_el else "Unknown",
            url=row.get("href", "").split("?")[0],
            release_date=release_date,
            image=img_el.get("src", "") if img_el else "",
            status=status,
            price_pct=pct,
            price_original=original,
            price_final=final,
        )


def _collect(start: date, end: date, language: str, country: str, max_pages: int, coming_soon: bool) -> list[Game]:
    """Page through Steam search results, keeping rows whose release date falls in [start, end].

    coming_soon=False walks already-released games newest-first (Released_DESC) and stops
    once dates fall below `start`. coming_soon=True walks not-yet-released games
    soonest-first (Released_ASC) and stops once dates rise above `end`.
    """
    games: list[Game] = []
    streak = 0
    count = 50
    for page in range(max_pages):
        page_start = page * count
        log.debug("Fetching %s page %d (start=%d)", "upcoming" if coming_soon else "live", page, page_start)
        data = fetch_page(page_start, count, language, country, coming_soon)
        html_text = data.get("results_html", "")
        if not html_text or "search_result_row" not in html_text:
            break
        rows = list(parse_rows(html_text, "upcoming" if coming_soon else "live"))
        if not rows:
            break
        for game in rows:
            d = game.release_date
            if start <= d <= end:
                games.append(game)
                streak = 0
                continue
            if coming_soon:
                if d < start:
                    continue  # backlog before our window; keep scanning ascending
                streak += 1  # d > end, moved past the window
            else:
                if d > end:
                    continue  # rare future straggler ahead of a descending sort
                streak += 1  # d < start, moved past the window
        if streak >= STALE_STREAK:
            break
        time.sleep(0.5)  # be polite to Steam's servers
    return games


def find_todays_releases(target: date, language: str, country: str, max_pages: int) -> list[Game]:
    live = _collect(target, target, language, country, max_pages, coming_soon=False)
    upcoming = _collect(target, target, language, country, max_pages, coming_soon=True)
    seen = {g.appid for g in live}
    return live + [g for g in upcoming if g.appid not in seen]


def find_backfill_releases(days: int, language: str, country: str, max_pages: int) -> list[Game]:
    end = date.today()
    start = end - timedelta(days=days - 1)
    return _collect(start, end, language, country, max_pages, coming_soon=False)


def send_discord(webhook_url: str, today: date, games: list[Game], dry_run: bool, site_url: str) -> None:
    if not games:
        log.info("No new releases today - skipping Discord ping")
        return

    content = f"{today.isoformat()} 新遊戲來了 🫠\n{site_url}"
    if dry_run:
        log.info(content)
        for game in games:
            log.info(" - %s (%s) %s", game.name, game.release_date, game.url)
        return

    requests.post(webhook_url, json={"content": content}, timeout=20).raise_for_status()


# ---------------------------------------------------------------------------
# Static history site (docs/): index.html = latest day, dates/<date>.html per
# day, dates/index.html lists every day. Plain <a href> navigation everywhere
# so the browser's own back/forward buttons work with no JS routing.
# ---------------------------------------------------------------------------

STYLE_CSS = """:root { color-scheme: dark; }
* { box-sizing: border-box; }
body {
  margin: 0; padding: 0 16px 48px; background: #10141a; color: #e7ecf2;
  font-family: -apple-system, "Segoe UI", "Microsoft JhengHei", sans-serif;
}
.wrap { max-width: 900px; margin: 0 auto; }
.topbar {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  padding: 16px 0; flex-wrap: wrap; position: sticky; top: 0;
  background: rgba(16, 20, 26, 0.92); backdrop-filter: blur(6px); z-index: 10;
}
.brand { display: flex; align-items: center; text-decoration: none; }
.brand img { height: 44px; width: auto; display: block; }
.nav { display: flex; align-items: center; gap: 8px; font-size: 0.85rem; flex-wrap: wrap; }
.nav a, .nav .disabled {
  color: #9db4d1; text-decoration: none; padding: 6px 10px; border-radius: 6px;
  background: #171d26; border: 1px solid #232b37;
}
.nav a:hover { background: #1c2330; }
.nav .disabled { color: #4a5361; }
.meta { color: #8894a3; font-size: 0.8rem; margin-bottom: 20px; }
h1 { font-size: 1.3rem; margin: 4px 0 16px; }
h2.section { font-size: 0.85rem; color: #8894a3; margin: 24px 0 8px; text-transform: uppercase; letter-spacing: 0.04em; }
.card { background: #171d26; border: 1px solid #232b37; border-radius: 10px; overflow: visible; }
.row {
  display: flex; gap: 14px; padding: 12px 14px; align-items: center;
  border-bottom: 1px solid #1c2330;
}
.row:first-child { border-top-left-radius: 10px; border-top-right-radius: 10px; }
.row:last-child { border-bottom: none; border-bottom-left-radius: 10px; border-bottom-right-radius: 10px; }
.row:hover { background: #1c2330; position: relative; z-index: 20; }
.row .media { flex: none; display: block; position: relative; }
.row img.cap {
  width: 160px; height: 75px; object-fit: cover; border-radius: 6px; background: #232b37;
  transition: transform 0.18s ease, box-shadow 0.18s ease;
}
.row:hover img.cap {
  position: absolute; top: 50%; right: 100%; margin-right: 10px;
  transform: translateY(-50%) scale(2.1); transform-origin: right center;
  box-shadow: 0 12px 32px rgba(0, 0, 0, 0.6); z-index: 30;
}
.row .info { min-width: 0; flex: 1; }
.row .name {
  display: block; font-size: 1.15rem; font-weight: 600; line-height: 1.3;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  text-decoration: none; color: inherit;
}
.row .name:hover { text-decoration: underline; }
.row .date-line { color: #8894a3; font-size: 0.8rem; margin-top: 3px; }
.price-line { display: flex; align-items: stretch; margin-top: 4px; }
.disc-pct {
  background: #4c6b22; color: #a4d007; font-weight: 700; font-size: 0.78rem;
  padding: 4px 6px; border-radius: 2px 0 0 2px; display: flex; align-items: center;
}
.disc-prices {
  background: rgba(0, 0, 0, 0.5); display: flex; align-items: center; gap: 6px;
  padding: 4px 8px; border-radius: 0 2px 2px 0;
}
.disc-orig { color: #8894a3; text-decoration: line-through; font-size: 0.78rem; }
.disc-final { color: #a4d007; font-weight: 700; font-size: 0.9rem; }
.disc-final.plain { color: #e7ecf2; font-weight: 600; }
.disc-final.unknown { color: #8894a3; font-weight: 400; font-size: 0.85rem; }
.discount-end { color: #66c0f4; font-size: 0.78rem; margin-top: 3px; }
.open-modal-backdrop {
  display: none; position: fixed; inset: 0; background: rgba(0, 0, 0, 0.6);
  align-items: center; justify-content: center; z-index: 100; padding: 16px;
}
.open-modal {
  background: #171d26; border: 1px solid #232b37; border-radius: 12px; padding: 20px;
  width: min(320px, 100%); display: flex; flex-direction: column; gap: 14px;
}
.open-modal-title { font-size: 1rem; font-weight: 600; }
.open-modal-actions { display: flex; gap: 8px; }
.open-modal-actions button {
  flex: 1; padding: 10px; border-radius: 8px; border: 1px solid #2a3346;
  background: #1c2330; color: #e7ecf2; font-size: 0.9rem; cursor: pointer;
}
.open-modal-actions button:hover { background: #232b3d; border-color: #3a4460; }
.open-modal-remember { display: flex; align-items: center; gap: 6px; font-size: 0.85rem; color: #8894a3; }
.open-modal-close {
  align-self: flex-end; background: none; border: none; color: #6b7686;
  cursor: pointer; font-size: 0.8rem; padding: 0;
}
.open-modal-close:hover { color: #9db4d1; }
.tags { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 7px; }
.tag {
  font-size: 0.78rem; padding: 3px 10px; border-radius: 999px; background: #241a33;
  color: #b79aef; text-decoration: none;
}
.tag:hover { background: #3a2a4f; color: #d3c1fb; }
.tag-toggle { display: none; }
.tag-extra { display: none; }
.tag-toggle:checked ~ .tag-extra { display: contents; }
.tag-more {
  font-size: 0.78rem; padding: 3px 10px; border-radius: 999px; cursor: pointer;
  background: transparent; color: #6b7686; border: 1px dashed #3a4152;
}
.tag-more:hover { color: #9db4d1; border-color: #5a6478; }
.tag-more-close { display: none; }
.tag-toggle:checked ~ .tag-more-open { display: none; }
.tag-toggle:checked ~ .tag-more-close { display: inline-block; }
@media (max-width: 480px) {
  .row img.cap { width: 110px; height: 52px; }
  .row .name { font-size: 1rem; }
}
.badge { flex: none; font-size: 0.72rem; padding: 3px 8px; border-radius: 999px; white-space: nowrap; }
.badge.live { background: #16331f; color: #5fd58a; }
.badge.upcoming { background: #33291a; color: #e0b25f; }
.empty { color: #8894a3; padding: 40px 0; text-align: center; }
.datelist { display: flex; flex-direction: column; gap: 6px; }
.datelist a {
  display: flex; justify-content: space-between; padding: 10px 14px; background: #171d26;
  border: 1px solid #232b37; border-radius: 8px; text-decoration: none; color: inherit;
}
.datelist a:hover { background: #1c2330; }
.datelist .count { color: #8894a3; font-size: 0.85rem; }
.search-box {
  width: 100%; padding: 10px 14px; border-radius: 8px; border: 1px solid #2a3346;
  background: #171d26; color: #e7ecf2; font-size: 0.95rem; margin-bottom: 8px;
}
.search-box:focus { outline: none; border-color: #4a5b7a; }
"""

STYLE_HASH = hashlib.md5(STYLE_CSS.encode("utf-8")).hexdigest()[:8]

PAGE_SHELL = """<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<link rel="stylesheet" href="__ASSET_BASE__assets/style.css?v=__CSS_VER__">
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <a class="brand" href="__HOME_HREF__"><img src="__ASSET_BASE__assets/logo.png" alt="Steam 新遊戲紀錄"></a>
    <div class="nav">__NAV__</div>
  </div>
  <div class="meta">__META__</div>
  __BODY__
</div>

<div class="open-modal-backdrop" id="openModalBackdrop">
  <div class="open-modal">
    <div class="open-modal-title">要怎麼開啟這款遊戲？</div>
    <div class="open-modal-actions">
      <button type="button" data-choice="web">🌐 網頁開啟</button>
      <button type="button" data-choice="steam">💠 Steam 開啟</button>
    </div>
    <label class="open-modal-remember"><input type="checkbox" id="openModalRemember" checked> 記住我的選擇</label>
    <button type="button" class="open-modal-close" id="openModalCancel">取消</button>
  </div>
</div>
<script>
function imgFallback(el) {
  var fb = el.getAttribute("data-fallback");
  if (fb && el.src !== fb) {
    el.onerror = function () { this.style.visibility = "hidden"; this.onerror = null; };
    el.src = fb;
  } else {
    el.style.visibility = "hidden";
  }
}
(function () {
  var KEY = "steamOpenPref";
  function getPref() { try { return localStorage.getItem(KEY); } catch (e) { return null; } }
  function setPref(v) { try { if (v) { localStorage.setItem(KEY, v); } else { localStorage.removeItem(KEY); } } catch (e) {} }

  var backdrop = document.getElementById("openModalBackdrop");
  var remember = document.getElementById("openModalRemember");
  var pending = null;

  function closeModal() { backdrop.style.display = "none"; pending = null; }
  function openWith(choice) {
    if (!pending) return;
    var url = choice === "steam" ? pending.steam : pending.web;
    if (choice === "steam") { window.location.href = url; } else { window.open(url, "_blank", "noopener"); }
  }

  backdrop.addEventListener("click", function (e) { if (e.target === backdrop) closeModal(); });
  document.getElementById("openModalCancel").addEventListener("click", closeModal);
  backdrop.querySelectorAll("[data-choice]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var choice = btn.getAttribute("data-choice");
      if (remember.checked) setPref(choice);
      openWith(choice);
      closeModal();
    });
  });

  document.addEventListener("click", function (e) {
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var el = e.target.closest("[data-web][data-steam]");
    if (!el) return;
    e.preventDefault();
    pending = { web: el.getAttribute("data-web"), steam: el.getAttribute("data-steam") };
    var pref = getPref();
    if (pref) { openWith(pref); pending = null; return; }
    backdrop.style.display = "flex";
  });
})();
</script>
</body>
</html>
"""


def render_page(title: str, base: str, nav_html: str, meta: str, body_html: str) -> str:
    return (
        PAGE_SHELL.replace("__TITLE__", esc(title))
        .replace("__ASSET_BASE__", base)
        .replace("__CSS_VER__", STYLE_HASH)
        .replace("__HOME_HREF__", f"{base}index.html")
        .replace("__NAV__", nav_html)
        .replace("__META__", meta)
        .replace("__BODY__", body_html)
    )


_UNSAFE_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def tag_slug(tag: str) -> str:
    # Keep the human-readable (including CJK) tag text as the filename itself - GitHub
    # Pages/most static hosts URL-decode the request path to match the file on disk, so a
    # pre-percent-encoded filename (e.g. "%E4%B8%AD...html") never matches and 404s. Only
    # characters Windows can't put in a filename get swapped out.
    safe = _UNSAFE_FILENAME_CHARS.sub("-", tag).strip()
    return safe or "tag"


TAGS_VISIBLE = 6


def render_tags(appid: str, tags: list[str], base: str) -> str:
    if not tags:
        return ""

    def chip(t: str) -> str:
        return f'<a class="tag" href="{base}tags/{tag_slug(t)}.html">{esc(t)}</a>'

    visible = "".join(chip(t) for t in tags[:TAGS_VISIBLE])
    rest = tags[TAGS_VISIBLE:]
    if not rest:
        return f'<div class="tags">{visible}</div>'

    # Tags are already in Steam's own popularity order (scraped in DOM order from the
    # page's popular-tags widget), so the first N are already the "priority" ones.
    uid = f"tagexp-{esc(appid)}"
    extra = "".join(chip(t) for t in rest)
    return (
        f'<div class="tags">{visible}'
        f'<input type="checkbox" id="{uid}" class="tag-toggle">'
        f'<span class="tag-extra">{extra}</span>'
        f'<label for="{uid}" class="tag-more tag-more-open">+{len(rest)}</label>'
        f'<label for="{uid}" class="tag-more tag-more-close">收起</label>'
        f"</div>"
    )


def render_price(g: dict) -> str:
    final = g.get("price_final")
    if not final:
        return '<div class="price-line"><span class="disc-final unknown">價格未知</span></div>'

    final_class = "disc-final free" if final == "免費" else "disc-final"
    pct, original = g.get("price_pct"), g.get("price_original")
    if pct and original:
        html = (
            '<div class="price-line">'
            f'<span class="disc-pct">{esc(pct)}</span>'
            '<span class="disc-prices">'
            f'<span class="disc-orig">{esc(original)}</span>'
            f'<span class="{final_class}">{esc(final)}</span>'
            "</span></div>"
        )
    else:
        html = f'<div class="price-line"><span class="{final_class} plain">{esc(final)}</span></div>'

    end = g.get("discount_end")
    if end:
        html += f'<div class="discount-end">{esc(end)}</div>'
    return html


def render_row(g: dict, base: str, show_date: bool = False) -> str:
    if g["status"] == "live":
        badge = '<span class="badge live">✅ 已上架</span>'
    else:
        epoch = g.get("release_epoch")
        if epoch:
            dt = datetime.fromtimestamp(epoch).astimezone()
            label = dt.strftime("%m/%d %H:%M")
        else:
            label = "預計上架"
        badge = f'<span class="badge upcoming">⏳ {esc(label)}</span>'

    date_html = f'<div class="date-line">{esc(g["release_date"])}</div>' if show_date else ""
    tags_html = render_tags(g["appid"], g.get("tags", []), base)
    fallback_image = g.get("image") or ""
    zoom_image = g.get("header_image") or fallback_image
    web_url = esc(g["url"])
    steam_url = f"steam://store/{esc(g['appid'])}"
    open_attrs = f'data-web="{web_url}" data-steam="{steam_url}"'

    return (
        '<div class="row">'
        f'<a class="media" href="{web_url}" {open_attrs}>'
        f'<img class="cap" src="{esc(zoom_image)}" data-fallback="{esc(fallback_image)}" '
        f'onerror="imgFallback(this)" loading="lazy" alt=""></a>'
        '<div class="info">'
        f'<a class="name" href="{web_url}" {open_attrs}>{esc(g["name"])}</a>'
        f"{date_html}{render_price(g)}"
        f"{tags_html}</div>"
        f"{badge}</div>"
    )


def render_games_section(title: str, games: list[dict], base: str, show_date: bool = False) -> str:
    if not games:
        return ""
    rows = "".join(render_row(g, base, show_date) for g in games)
    return f'<h2 class="section">{esc(title)}</h2><div class="card">{rows}</div>'


def render_date_body(date_str: str, games: list[dict], base: str) -> str:
    live = sorted([g for g in games if g["status"] == "live"], key=lambda g: g["name"])
    upcoming = sorted(
        [g for g in games if g["status"] == "upcoming"],
        key=lambda g: (g.get("release_epoch") is None, g.get("release_epoch") or 0, g["name"]),
    )
    body = f"<h1>{esc(date_str)}</h1>"
    if not games:
        return body + '<div class="empty">當天沒有資料</div>'
    body += render_games_section("✅ 已上架", live, base)
    body += render_games_section("⏳ 預計上架", upcoming, base)
    return body


def build_nav(dates_desc: list[str], current: str, base: str) -> str:
    idx = dates_desc.index(current)
    older = dates_desc[idx + 1] if idx + 1 < len(dates_desc) else None
    newer = dates_desc[idx - 1] if idx > 0 else None

    def link(label: str, target: str | None) -> str:
        if target is None:
            return f'<span class="disabled">{esc(label)}</span>'
        return f'<a href="{base}dates/{target}.html">{esc(label)}</a>'

    return (
        link("← 前一天", older)
        + f'<a href="{base}dates/index.html">所有日期</a>'
        + link("後一天 →", newer)
    )


def generate_site(history: dict, docs_dir: Path, retention_days: int) -> None:
    by_date: dict[str, list[dict]] = {}
    for g in history["games"].values():
        by_date.setdefault(g["release_date"], []).append(g)
    dates_desc = sorted(by_date, reverse=True)
    counts = {d: len(by_date[d]) for d in dates_desc}
    generated_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
    meta = f"更新於 {esc(generated_at)} · 保留最近 {retention_days} 天"

    dates_dir = docs_dir / "dates"
    dates_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "assets").mkdir(parents=True, exist_ok=True)
    style_path = docs_dir / "assets" / "style.css"
    if not style_path.exists() or style_path.read_text(encoding="utf-8") != STYLE_CSS:
        style_path.write_text(STYLE_CSS, encoding="utf-8")

    for d in dates_desc:
        page = render_page(
            title=f"{d} 新遊戲 - Steam 新遊戲紀錄",
            base="../",
            nav_html=build_nav(dates_desc, d, "../"),
            meta=meta,
            body_html=render_date_body(d, by_date[d], "../"),
        )
        (dates_dir / f"{d}.html").write_text(page, encoding="utf-8")

    dates_index_rows = (
        "".join(
            f'<a href="../dates/{d}.html"><span>{esc(d)}</span><span class="count">{counts[d]} 款</span></a>'
            for d in dates_desc
        )
        if dates_desc
        else '<div class="empty">尚無資料</div>'
    )
    dates_index_page = render_page(
        title="所有日期 - Steam 新遊戲紀錄",
        base="../",
        nav_html='<a href="../index.html">← 回首頁</a>',
        meta=meta,
        body_html=f'<h1>所有日期</h1><div class="datelist">{dates_index_rows}</div>',
    )
    (dates_dir / "index.html").write_text(dates_index_page, encoding="utf-8")

    if dates_desc:
        latest = dates_desc[0]
        home_page = render_page(
            title="Steam 新遊戲紀錄",
            base="",
            nav_html=build_nav(dates_desc, latest, ""),
            meta=meta,
            body_html=render_date_body(latest, by_date[latest], ""),
        )
    else:
        home_page = render_page(
            title="Steam 新遊戲紀錄",
            base="",
            nav_html="",
            meta=meta,
            body_html='<h1>Steam 新遊戲紀錄</h1><div class="empty">尚無資料</div>',
        )
    (docs_dir / "index.html").write_text(home_page, encoding="utf-8")

    keep = {f"{d}.html" for d in dates_desc}
    for f in dates_dir.glob("*.html"):
        if f.name != "index.html" and f.name not in keep:
            f.unlink()

    tags_dir = docs_dir / "tags"
    tags_dir.mkdir(parents=True, exist_ok=True)
    tag_map: dict[str, list[dict]] = {}
    for g in history["games"].values():
        for t in g.get("tags", []):
            tag_map.setdefault(t, []).append(g)

    slug_to_tag: dict[str, str] = {}
    for tag, glist in tag_map.items():
        slug = tag_slug(tag)
        slug_to_tag[slug] = tag
        glist_sorted = sorted(glist, key=lambda g: g["release_date"], reverse=True)
        page = render_page(
            title=f"#{tag} - Steam 新遊戲紀錄",
            base="../",
            nav_html='<a href="../index.html">← 回首頁</a>',
            meta=meta,
            body_html=f'<h1>#{esc(tag)}</h1><div class="card">'
            + "".join(render_row(g, "../", show_date=True) for g in glist_sorted)
            + "</div>",
        )
        (tags_dir / f"{slug}.html").write_text(page, encoding="utf-8")

    for f in tags_dir.glob("*.html"):
        if f.stem not in slug_to_tag:
            f.unlink()

    all_games_sorted = sorted(
        history["games"].values(), key=lambda g: (g["release_date"], g["name"]), reverse=True
    )
    search_rows = "".join(render_row(g, "", show_date=True) for g in all_games_sorted)
    search_script = """<script>
(function () {
  var box = document.getElementById("searchBox");
  var rows = Array.prototype.slice.call(document.querySelectorAll("#searchResults .row"));
  var countEl = document.getElementById("searchCount");
  function apply() {
    var q = box.value.trim().toLowerCase();
    var shown = 0;
    rows.forEach(function (r) {
      var nameEl = r.querySelector(".name");
      var name = nameEl ? nameEl.textContent.toLowerCase() : "";
      var match = !q || name.indexOf(q) !== -1;
      r.style.display = match ? "" : "none";
      if (match) shown++;
    });
    countEl.textContent = q ? "符合 " + shown + " / " + rows.length + " 款" : "共 " + rows.length + " 款";
  }
  box.addEventListener("input", apply);
  box.focus();
})();
</script>"""
    search_body = (
        "<h1>搜尋遊戲</h1>"
        '<input type="text" id="searchBox" class="search-box" placeholder="輸入遊戲名稱關鍵字…" autocomplete="off">'
        f'<div class="meta" id="searchCount">共 {len(all_games_sorted)} 款</div>'
        f'<div class="card" id="searchResults">{search_rows}</div>'
        f"{search_script}"
    )
    search_page = render_page(
        title="搜尋 - Steam 新遊戲紀錄",
        base="",
        nav_html='<a href="index.html">← 回首頁</a>',
        meta=meta,
        body_html=search_body,
    )
    (docs_dir / "search.html").write_text(search_page, encoding="utf-8")


def verify_image(url: str | None, attempts: int = 2) -> bool:
    if not url:
        return False
    for i in range(attempts):
        try:
            resp = requests.head(url, headers=HEADERS, timeout=6, allow_redirects=True)
            if resp.status_code == 200:
                return True
        except requests.RequestException:
            pass
        if i + 1 < attempts:
            time.sleep(1)
    return False


def enrich_with_details(games: list[Game], language: str) -> None:
    for g in games:
        details = fetch_game_details(g.appid, language)
        if g.status == "upcoming":
            g.release_epoch = details.epoch
        g.tags = details.tags
        g.discount_end = details.discount_end
        # Verify the header image actually loads (with one retry) before trusting it - if
        # it doesn't, leave header_image unset so rendering falls back to the small capsule
        # image (always sourced straight from the search listing, effectively always good).
        if details.header_image and verify_image(details.header_image):
            g.header_image = details.header_image
        else:
            if details.header_image:
                log.debug("Header image failed to verify for %s, falling back to capsule", g.appid)
            g.header_image = None
        time.sleep(0.2)


def git_publish(base_dir: Path, docs_dir: Path, message: str) -> None:
    """Best-effort: commit and push the docs dir so GitHub Pages picks up the new site."""
    if not (base_dir / ".git").exists():
        return
    try:
        subprocess.run(["git", "-C", str(base_dir), "add", str(docs_dir)], check=True, capture_output=True)
        staged = subprocess.run(["git", "-C", str(base_dir), "diff", "--cached", "--quiet"])
        if staged.returncode == 0:
            log.info("No site changes to publish")
            return
        subprocess.run(["git", "-C", str(base_dir), "commit", "-m", message], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(base_dir), "push"], check=True, capture_output=True, timeout=60)
        log.info("Pushed site update to GitHub")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        stderr = e.stderr.decode(errors="ignore") if getattr(e, "stderr", None) else str(e)
        log.warning("git publish failed, site stayed local-only: %s", stderr.strip())


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=BASE_DIR / "config.json")
    parser.add_argument("--state", type=Path, default=BASE_DIR / "state.json")
    parser.add_argument("--history", type=Path, default=BASE_DIR / "history.json")
    parser.add_argument("--docs", type=Path, default=BASE_DIR / "docs")
    parser.add_argument("--dry-run", action="store_true", help="Print results instead of posting to Discord")
    parser.add_argument("--no-backfill", action="store_true", help="Skip automatic first-run backfill")
    parser.add_argument("--backfill", type=int, nargs="?", const=-1, help="Force a (re)backfill of N days")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    config = load_config(args.config)
    language = config.get("language", "english")
    country = config.get("country", "us")
    max_pages = int(config.get("max_pages", 10))
    retention_days = int(config.get("retention_days", DEFAULT_RETENTION_DAYS))
    backfill_days = int(config.get("backfill_days", DEFAULT_BACKFILL_DAYS))
    backfill_max_pages = int(config.get("backfill_max_pages", 40))
    today = date.today()

    history = load_history(args.history)
    should_backfill = args.backfill is not None or (not history["games"] and not args.no_backfill)
    if should_backfill:
        days = args.backfill if args.backfill and args.backfill > 0 else backfill_days
        log.info("Backfilling the last %d day(s) (fetching tags/prices per game, this takes a while)...", days)
        backfill_games = find_backfill_releases(days, language, country, backfill_max_pages)
        enrich_with_details(backfill_games, language)
        upsert_history(history, backfill_games)
        log.info("Backfill added/updated %d release(s)", len(backfill_games))

    games = find_todays_releases(target=today, language=language, country=country, max_pages=max_pages)
    enrich_with_details(games, language)
    upsert_history(history, games)
    if not args.dry_run or should_backfill:
        save_history(args.history, history, retention_days)
        generate_site(history, args.docs, retention_days)
        log.info("Site updated: %s", args.docs / "index.html")
        if not args.dry_run and config.get("git_auto_push", True):
            git_publish(BASE_DIR, args.docs, f"Update site {today.isoformat()}")

    state = load_state(args.state)
    notified = state["notified"]
    new_games = [g for g in games if g.appid not in notified]
    log.info("Found %d release(s) today, %d not yet notified", len(games), len(new_games))

    site_url = config.get("site_url") or f"file:///{(args.docs / 'index.html').resolve().as_posix()}"
    send_discord(config["webhook_url"], today, new_games, args.dry_run, site_url)

    if not args.dry_run:
        for game in new_games:
            notified[game.appid] = game.release_date.isoformat()
        save_state(args.state, state)


if __name__ == "__main__":
    main()
