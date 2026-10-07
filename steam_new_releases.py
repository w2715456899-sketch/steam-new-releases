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
from datetime import date, datetime, timedelta, timezone
from html import escape as esc
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

import requests

BASE_DIR = Path(__file__).resolve().parent
STEAM_API_BASE = "https://api.steampowered.com"
ASSET_BASE = "https://shared.fastly.steamstatic.com/store_item_assets/"
ITAD_API_BASE = "https://api.isthereanydeal.com"
ITAD_STEAM_SHOP_ID = 61  # confirmed live via GET /service/shops/v1 (no auth needed)
HISTORICAL_LOW_MIN_AGE_DAYS = 30  # launch discounts usually run ~2 weeks; skip those
HISTORICAL_LOW_MIN_REVIEWS = 1000
HISTORICAL_LOW_MAX_PAGES = 10  # x200 per page - safety cap, normally ~2 pages
HEADERS = {"User-Agent": "steam-new-releases-bot/2.0"}
DEFAULT_RETENTION_DAYS = 30
DEFAULT_BACKFILL_DAYS = 30
LOCAL_TZ = ZoneInfo("Asia/Taipei")  # Both the query window and the date each item gets filed
# under use this timezone, so the site is internally consistent with itself (and with the
# countdown time shown for upcoming games). Steam's own store page dates use Pacific time
# instead, so this site's date for a given game can differ by a day from what its Steam page
# says for anything released in the ~15-16h gap between the two timezones' day boundaries -
# a known, accepted trade-off in favor of everything on this site agreeing with itself.
MAX_QUERY_PAGES = 50  # safety cap (5000 items) so a pagination bug can't loop forever
SITE_NAME = "Steam美食家"  # the site's own name: page <title>s, topbar brand, README
OG_SITE_NAME = "Steam新遊戲通知"  # shown instead of SITE_NAME specifically in link-preview
# cards (Discord/LINE/etc, via og:site_name and og:title) - a deliberately different name
# from the site's own, chosen by the user for how a shared link should introduce itself.
WEEKDAY_ZH = ["一", "二", "三", "四", "五", "六", "日"]
# Contents of every wishlist button: an inline SVG star (the ☆/★ glyphs sit off-center on
# iOS Safari; an SVG doesn't) plus both labels. .filled toggles the star's fill and which
# label shows via CSS (see .wish-star.filled), so JS only ever flips that one class.
WISH_BTN_HTML = (
    '<svg viewBox="0 0 24 24" aria-hidden="true">'
    '<path d="M12 17.27L18.18 21l-1.64-7.03L22 9.24l-7.19-.61L12 2 9.19 8.63 2 9.24l5.46 4.73L5.82 21z"/>'
    "</svg>"
    '<span class="w-off">願望</span><span class="w-on">已加入</span>'
)

log = logging.getLogger("steam_new_releases")


def local_today() -> date:
    # Plain date.today() reads the machine's own system clock/timezone - fine on this
    # Taipei-configured Windows box, but GitHub Actions runners default to UTC, which is
    # up to 8 hours behind Taipei's calendar date (00:00-08:00 Taipei is still "yesterday"
    # in UTC). Always resolving "today" through LOCAL_TZ keeps this consistent regardless
    # of which machine runs the script.
    return datetime.now(tz=LOCAL_TZ).date()


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
    discount_end: int | None = field(default=None)  # unix epoch, or None if not currently discounted
    is_adult: bool = field(default=False)  # Steam's own "Adult Only Sexual Content" descriptor (id 3)
    review_score: int | None = field(default=None)  # Steam's 1-9 bucket, or None if too few reviews yet
    review_score_label: str | None = field(default=None)  # e.g. "壓倒性好評", already localized by the API
    review_percent: int | None = field(default=None)  # 0-100
    review_count: int | None = field(default=None)


def load_config(path: Path) -> dict:
    if not path.exists():
        log.error("Config file not found: %s (copy config.example.json to config.json first)", path)
        sys.exit(1)
    config = json.loads(path.read_text(encoding="utf-8"))
    if not config.get("webhook_url"):
        log.error("config.json is missing 'webhook_url'")
        sys.exit(1)
    if not config.get("steam_api_key"):
        log.error("config.json is missing 'steam_api_key' (get one at https://steamcommunity.com/dev/apikey)")
        sys.exit(1)
    return config


def load_history(path: Path) -> dict:
    if not path.exists():
        return {"games": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def save_history(path: Path, history: dict, retention_days: int) -> None:
    cutoff = (local_today() - timedelta(days=retention_days)).isoformat()
    history["games"] = {aid: g for aid, g in history["games"].items() if g["release_date"] >= cutoff}
    path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")


def upsert_history(history: dict, games: Iterable[Game]) -> None:
    # Every field always comes straight from a fresh, successful API response (unlike the
    # old HTML-scraping version, there's no "maybe this selector didn't match" case to guard
    # against), so it's safe to just overwrite everything rather than sticky-merge with what
    # was there before.
    for g in games:
        history["games"][g.appid] = game_to_dict(g)


def game_to_dict(g: Game) -> dict:
    return {
        "appid": g.appid,
        "name": g.name,
        "url": g.url,
        "release_date": g.release_date.isoformat(),
        "image": g.image,
        "status": g.status,
        "price_pct": g.price_pct,
        "price_original": g.price_original,
        "price_final": g.price_final,
        "release_epoch": g.release_epoch,
        "tags": g.tags,
        "header_image": g.header_image,
        "discount_end": g.discount_end,
        "is_adult": g.is_adult,
        "review_score": g.review_score,
        "review_score_label": g.review_score_label,
        "review_percent": g.review_percent,
        "review_count": g.review_count,
    }


# ---------------------------------------------------------------------------
# Steam Web API (IStoreQueryService / IStoreBrowseService / IStoreService) -
# undocumented but real endpoints used by Steam's own store frontend. Unlike
# the public /search/results/ HTML page, these don't apply the account-level
# "Adult Only Sexual Content" search-visibility filter, and they return
# structured JSON (real epochs, real price fields) instead of scraped HTML.
# ---------------------------------------------------------------------------


STEAM_API_RETRIES = 3


def steam_api_get(interface: str, method: str, key: str, **params) -> dict:
    url = f"{STEAM_API_BASE}/{interface}/{method}/v1/"
    # The review-score refresh alone makes ~65 sequential calls every run - at that volume, a
    # single transient network hiccup or Steam 5xx (no retry) used to be enough to crash the
    # whole hourly job. Retries here turn "one bad request kills the run" into "just this one
    # request is a bit slower."
    last_exc: Exception | None = None
    for attempt in range(STEAM_API_RETRIES):
        try:
            resp = requests.get(url, params={"key": key, **params}, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            return resp.json()["response"]
        except (requests.RequestException, KeyError, ValueError) as exc:
            last_exc = exc
            if attempt < STEAM_API_RETRIES - 1:
                wait = 2**attempt
                log.warning("%s/%s request failed (%s), retrying in %ds...", interface, method, exc, wait)
                time.sleep(wait)
    raise last_exc


def fetch_tag_names(key: str, language: str) -> dict[int, str]:
    resp = steam_api_get("IStoreService", "GetTagList", key, language=language)
    return {t["tagid"]: t["name"] for t in resp.get("tags", [])}


def _asset_url(assets: dict, filename_key: str) -> str:
    fmt = assets.get("asset_url_format")
    filename = assets.get(filename_key)
    if not fmt or not filename:
        return ""
    return ASSET_BASE + fmt.replace("${FILENAME}", filename)


def _parse_reviews(item: dict) -> tuple[int | None, str | None, int | None, int | None]:
    # summary_filtered (not summary_language_specific) matches what Steam's own store page
    # shows by default - all-language review count, just excluding review-bombs/off-topic.
    reviews = (item.get("reviews") or {}).get("summary_filtered") or {}
    count = reviews.get("review_count") or None
    if not count:
        return None, None, None, None
    return reviews.get("review_score"), reviews.get("review_score_label"), reviews.get("percent_positive"), count


def _item_to_game(item: dict, tag_names: dict[int, str]) -> Game | None:
    if not item.get("success") or not item.get("visible", True):
        return None
    release = item.get("release") or {}
    epoch = release.get("steam_release_date")
    if not epoch:
        return None  # no confirmed date yet - nothing meaningful to show

    assets = item.get("assets") or {}
    purchase = item.get("best_purchase_option")
    price_pct = price_original = price_final = None
    discount_end = None
    if purchase:
        price_final = purchase.get("formatted_final_price")
        if purchase.get("discount_pct"):
            price_pct = f"-{purchase['discount_pct']}%"
            price_original = purchase.get("formatted_original_price")
        active = purchase.get("active_discounts") or []
        if active:
            discount_end = active[0].get("discount_end_date")
    elif item.get("is_free"):
        # Free games have no purchase option at all (nothing to buy), so this is the
        # only signal for them - without it they'd wrongly show as "price unknown".
        price_final = "免費"

    tagids = item.get("tagids") or []
    tags = [tag_names[t] for t in tagids if t in tag_names]
    appid = str(item["appid"])
    is_adult = 3 in (item.get("content_descriptorids") or [])  # 3 = AdultOnlySexualContent

    review_score, review_score_label, review_percent, review_count = _parse_reviews(item)

    return Game(
        appid=appid,
        name=item.get("name", "Unknown"),
        url=f"https://store.steampowered.com/app/{appid}/",
        release_date=datetime.fromtimestamp(epoch, tz=LOCAL_TZ).date(),
        image=_asset_url(assets, "small_capsule"),
        status="upcoming" if release.get("is_coming_soon") else "live",
        price_pct=price_pct,
        price_original=price_original,
        price_final=price_final,
        release_epoch=epoch,
        tags=tags,
        header_image=_asset_url(assets, "header"),
        discount_end=discount_end,
        is_adult=is_adult,
        review_score=review_score,
        review_score_label=review_score_label,
        review_percent=review_percent,
        review_count=review_count,
    )


def query_items_by_date_range(key: str, start_date: date, end_date: date, language: str, country: str) -> list[dict]:
    start_epoch = int(datetime.combine(start_date, datetime.min.time(), tzinfo=LOCAL_TZ).timestamp())
    end_epoch = int(datetime.combine(end_date + timedelta(days=1), datetime.min.time(), tzinfo=LOCAL_TZ).timestamp())
    items: list[dict] = []
    start = 0
    for _ in range(MAX_QUERY_PAGES):
        input_json = {
            "query": {
                "start": start,
                "count": 100,
                "filters": {
                    "type_filters": {"include_games": True},
                    "release_date_filter": {
                        "release_date_type": 1,
                        "start_date": start_epoch,
                        "end_date": end_epoch,
                    },
                },
            },
            "context": {"language": language, "country_code": country, "steam_realm": 1},
            "data_request": {
                "include_assets": True,
                "include_release": True,
                "include_basic_info": True,
                "include_best_purchase_option": True,
                "include_tag_count": 20,
                "include_reviews": True,
            },
        }
        resp = steam_api_get("IStoreQueryService", "Query", key, input_json=json.dumps(input_json))
        batch = resp.get("store_items", [])
        items.extend(batch)
        total = resp.get("metadata", {}).get("total_matching_records", len(items))
        start += len(batch)
        if not batch or start >= total:
            break
        time.sleep(0.3)
    return items


def find_releases_in_range(
    key: str, start_date: date, end_date: date, language: str, country: str, tag_names: dict[int, str]
) -> list[Game]:
    items = query_items_by_date_range(key, start_date, end_date, language, country)
    games = [g for item in items if (g := _item_to_game(item, tag_names))]
    return games


def refresh_stale_discounts(
    history: dict, key: str, language: str, country: str, tag_names: dict[int, str], now_epoch: int
) -> int:
    """Re-check any tracked game whose recorded discount_end has already passed.

    upsert_history() only ever refreshes "today's" games, so a discount found a week ago
    would otherwise show as active on the site forever, past its actual end date. Batches
    up to 50 appids per GetItems call instead of one request per game.
    """
    stale = [aid for aid, g in history["games"].items() if g.get("discount_end") and g["discount_end"] < now_epoch]
    if not stale:
        return 0

    refreshed = 0
    for i in range(0, len(stale), 50):
        batch_ids = stale[i : i + 50]
        input_json = {
            "ids": [{"appid": int(a)} for a in batch_ids],
            "context": {"language": language, "country_code": country, "steam_realm": 1},
            "data_request": {
                "include_assets": True,
                "include_release": True,
                "include_basic_info": True,
                "include_best_purchase_option": True,
                "include_tag_count": 20,
                "include_reviews": True,
            },
        }
        try:
            resp = steam_api_get("IStoreBrowseService", "GetItems", key, input_json=json.dumps(input_json))
        except (requests.RequestException, KeyError, ValueError) as exc:
            # This is a "keep existing entries fresh" pass, not the critical path (today's
            # fetch) - one batch failing even after steam_api_get's own retries shouldn't take
            # down site generation/notification for the whole run, so skip it and move on.
            log.warning("Batch refresh failed after retries, skipping this batch: %s", exc)
            continue
        for item in resp.get("store_items", []):
            g = _item_to_game(item, tag_names)
            existing = history["games"].get(str(item.get("appid")))
            if not g or not existing:
                continue
            existing["price_pct"] = g.price_pct
            existing["price_original"] = g.price_original
            existing["price_final"] = g.price_final
            existing["discount_end"] = g.discount_end
            existing["review_score"] = g.review_score
            existing["review_score_label"] = g.review_score_label
            existing["review_percent"] = g.review_percent
            existing["review_count"] = g.review_count
            if g.tags:
                existing["tags"] = g.tags
            if g.header_image:
                existing["header_image"] = g.header_image
            refreshed += 1
        time.sleep(0.3)
    return refreshed


def refresh_stale_upcoming(
    history: dict, key: str, language: str, country: str, tag_names: dict[int, str], now_epoch: int
) -> int:
    """Re-check any tracked "upcoming" game whose countdown has already passed.

    A delayed/postponed game loses its steam_release_date entirely on Steam's side (it falls
    back to a dateless "即將推出" state), which makes it fail the release_date_filter used by
    the normal daily/hourly fetch - it simply stops appearing in those results, so nothing
    else in this pipeline can ever notice or update it, and it'd sit on the site forever with
    a countdown that's already in the past. Looked up by ID instead, which doesn't depend on
    having a date to filter on. If it actually went live or got a new date, this also just
    updates it normally; if it's dateless now, the countdown is cleared so the badge falls
    back to a plain "預計上架" instead of a stale, already-elapsed time.
    """
    stale = [
        aid
        for aid, g in history["games"].items()
        if g.get("status") == "upcoming" and g.get("release_epoch") and g["release_epoch"] < now_epoch
    ]
    if not stale:
        return 0

    refreshed = 0
    for i in range(0, len(stale), 50):
        batch_ids = stale[i : i + 50]
        input_json = {
            "ids": [{"appid": int(a)} for a in batch_ids],
            "context": {"language": language, "country_code": country, "steam_realm": 1},
            "data_request": {
                "include_assets": True,
                "include_release": True,
                "include_basic_info": True,
                "include_best_purchase_option": True,
                "include_tag_count": 20,
                "include_reviews": True,
            },
        }
        try:
            resp = steam_api_get("IStoreBrowseService", "GetItems", key, input_json=json.dumps(input_json))
        except (requests.RequestException, KeyError, ValueError) as exc:
            # This is a "keep existing entries fresh" pass, not the critical path (today's
            # fetch) - one batch failing even after steam_api_get's own retries shouldn't take
            # down site generation/notification for the whole run, so skip it and move on.
            log.warning("Batch refresh failed after retries, skipping this batch: %s", exc)
            continue
        for item in resp.get("store_items", []):
            appid = str(item.get("appid"))
            existing = history["games"].get(appid)
            if not existing:
                continue
            g = _item_to_game(item, tag_names)
            if g:
                # went live, or got a fresh date (same field either way)
                existing.update(
                    {
                        "status": g.status,
                        "release_epoch": g.release_epoch,
                        "price_pct": g.price_pct,
                        "price_original": g.price_original,
                        "price_final": g.price_final,
                        "discount_end": g.discount_end,
                        "image": g.image or existing.get("image"),
                        "header_image": g.header_image or existing.get("header_image"),
                        "tags": g.tags or existing.get("tags", []),
                        "is_adult": g.is_adult,
                        "review_score": g.review_score,
                        "review_score_label": g.review_score_label,
                        "review_percent": g.review_percent,
                        "review_count": g.review_count,
                    }
                )
            elif not (item.get("release") or {}).get("steam_release_date"):
                # postponed with no replacement date - drop the stale countdown
                existing["release_epoch"] = None
            refreshed += 1
        time.sleep(0.3)
    return refreshed


def refresh_review_scores(history: dict, key: str, language: str, country: str) -> int:
    """Refresh Steam's review score/percent/count for every tracked game.

    Unlike price/discount or the upcoming countdown, a review score has no natural "this is
    now stale" signal to gate a refresh on - it just slowly accumulates as more people review
    a game, and a brand-new release often has none yet. So this scans the whole catalog every
    run rather than a filtered subset. That's cheap in practice: retention_days caps the
    catalog at roughly one month of releases, so the cost stays flat run to run instead of
    growing over time. Only touches the review_* fields - price/tags/etc are the other
    refreshers' job.
    """
    ids = list(history["games"].keys())
    if not ids:
        return 0

    refreshed = 0
    for i in range(0, len(ids), 50):
        batch_ids = ids[i : i + 50]
        input_json = {
            "ids": [{"appid": int(a)} for a in batch_ids],
            "context": {"language": language, "country_code": country, "steam_realm": 1},
            "data_request": {"include_reviews": True},
        }
        try:
            resp = steam_api_get("IStoreBrowseService", "GetItems", key, input_json=json.dumps(input_json))
        except (requests.RequestException, KeyError, ValueError) as exc:
            # This is a "keep existing entries fresh" pass, not the critical path (today's
            # fetch) - one batch failing even after steam_api_get's own retries shouldn't take
            # down site generation/notification for the whole run, so skip it and move on.
            log.warning("Batch refresh failed after retries, skipping this batch: %s", exc)
            continue
        for item in resp.get("store_items", []):
            existing = history["games"].get(str(item.get("appid")))
            if not existing:
                continue
            score, label, percent, count = _parse_reviews(item)
            existing["review_score"] = score
            existing["review_score_label"] = label
            existing["review_percent"] = percent
            existing["review_count"] = count
            refreshed += 1
        time.sleep(0.3)
    return refreshed


def fetch_historical_lows(
    itad_key: str, steam_key: str, language: str, country: str, tag_names: dict[int, str], today: date
) -> list[dict] | None:
    """Games currently on sale on Steam at a brand-new all-time low (ITAD flag "N" - the same
    thing SteamDB marks in blue), released at least HISTORICAL_LOW_MIN_AGE_DAYS ago, with at
    least HISTORICAL_LOW_MIN_REVIEWS Steam reviews.

    Independent of the 30-day new-release catalog on purpose: these are mostly older games.
    ITAD supplies which games qualify; Steam's own GetItems then supplies everything shown,
    so rows render in exactly the same Steam format as every other page. Returns None if the
    ITAD side fails, so the caller keeps last run's list rather than blanking the page.
    """
    headers = {"ITAD-API-Key": itad_key}
    released_before = (today - timedelta(days=HISTORICAL_LOW_MIN_AGE_DAYS)).isoformat()
    body = {
        "country": country.upper(),
        "shops": [ITAD_STEAM_SHOP_ID],
        "limit": 200,
        "offset": 0,
        "filter": {
            "flag": "N",
            "type": [1],
            # ITAD 500s on a null bound here, so the lower end is just "any time".
            "releaseDate": {"min": "1970-01-01", "max": released_before},
            # Both bounds are required - with only "min" ITAD silently ignores the filter.
            "steamCount": {"min": HISTORICAL_LOW_MIN_REVIEWS, "max": 10**9},
        },
    }
    itad_ids: list[str] = []
    try:
        for _ in range(HISTORICAL_LOW_MAX_PAGES):
            resp = requests.post(f"{ITAD_API_BASE}/deals/v2", headers=headers, json=body, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            itad_ids += [d["id"] for d in data.get("list", [])]
            if not data.get("hasMore"):
                break
            body["offset"] = data["nextOffset"]
            time.sleep(0.3)

        appids: list[str] = []
        for i in range(0, len(itad_ids), 200):
            resp = requests.post(
                f"{ITAD_API_BASE}/lookup/shop/{ITAD_STEAM_SHOP_ID}/id/v1",
                headers=headers,
                json=itad_ids[i : i + 200],
                timeout=60,
            )
            resp.raise_for_status()
            for shop_ids in resp.json().values():
                # Some ITAD "games" only exist on Steam as a package (sub/...) - no app page
                # to show, so those are skipped.
                app = next((s for s in shop_ids or [] if s.startswith("app/")), None)
                if app:
                    appids.append(app.split("/", 1)[1])
    except (requests.RequestException, KeyError, ValueError) as exc:
        log.warning("ITAD historical-low fetch failed, keeping last run's list: %s", exc)
        return None

    games: list[dict] = []
    for i in range(0, len(appids), 50):
        input_json = {
            "ids": [{"appid": int(a)} for a in appids[i : i + 50]],
            "context": {"language": language, "country_code": country, "steam_realm": 1},
            "data_request": {
                "include_assets": True,
                "include_release": True,
                "include_basic_info": True,
                "include_best_purchase_option": True,
                "include_tag_count": 20,
                "include_reviews": True,
            },
        }
        try:
            resp = steam_api_get("IStoreBrowseService", "GetItems", steam_key, input_json=json.dumps(input_json))
        except (requests.RequestException, KeyError, ValueError) as exc:
            log.warning("Steam details for a historical-low batch failed, skipping it: %s", exc)
            continue
        for item in resp.get("store_items", []):
            g = _item_to_game(item, tag_names)
            # Steam itself must agree the game is discounted right now - ITAD's data can lag
            # Steam by a little, and a full-price game must never show up as a "low".
            if g and g.status == "live" and g.price_pct:
                games.append(game_to_dict(g))
        time.sleep(0.3)
    return games


def send_discord(webhook_url: str, today: date, count: int, dry_run: bool, site_url: str) -> None:
    if not count:
        log.info("No releases on record for today - skipping Discord ping")
        return

    content = f"{today.isoformat()} 新遊戲更新了 共 {count} 款\n{site_url}"
    if dry_run:
        log.info(content)
        return

    requests.post(webhook_url, json={"content": content}, timeout=20).raise_for_status()


# ---------------------------------------------------------------------------
# Static history site (docs/): index.html = latest day, dates/<date>.html per
# day, dates/index.html lists every day. Plain <a href> navigation everywhere
# so the browser's own back/forward buttons work with no JS routing.
# ---------------------------------------------------------------------------

STYLE_CSS = """/* Retro look: the 2004-era Steam desktop client - olive window chrome, bevelled buttons,
   sunken panes, Tahoma/Verdana. Bevel = light top-left edge + dark bottom-right edge;
   sunken = the same two colors swapped. */
:root {
  color-scheme: dark;
  --desk: #2b3125; --win: #4c5844; --pane: #3e4637; --inset: #2f3529; --hover: #49533f;
  --hi: #8c9284; --lo: #282e22; --line: #353c2f;
  --text: #dee5d7; --mute: #a0aa95; --dim: #7d8673; --gold: #c4b550;
  --green: #a4d007; --green-bg: #4c6b22; --blue: #66c0f4; --warn: #e0b25f; --bad: #e0745a;
  --ui: Tahoma, Verdana, "Microsoft JhengHei", "PingFang TC", "Noto Sans TC", sans-serif;
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0; padding: 20px 16px 40px; background: var(--desk); color: var(--text);
  font-family: var(--ui); font-size: 13px; line-height: 1.45;
}
a { color: inherit; }
button { font-family: inherit; }
.bevel, .btn, .tab, .nav-arrow, .nav-mid, .wish-star, .filter-chip, .pager button, .date-card, .open-modal-option, .tag-more {
  border: 1px solid; border-color: var(--hi) var(--lo) var(--lo) var(--hi);
}
.sunk, .search-box, .card, .box {
  border: 1px solid; border-color: var(--lo) var(--hi) var(--hi) var(--lo);
}

/* ---- window chrome ---- */
.win {
  max-width: 1180px; margin: 0 auto; background: var(--win);
  border: 1px solid; border-color: var(--hi) var(--lo) var(--lo) var(--hi);
  box-shadow: 0 10px 40px rgba(0, 0, 0, 0.45);
  transform-origin: 50% 0; animation: winOpen 0.28s cubic-bezier(0.2, 0.9, 0.3, 1.1) both;
}
@keyframes winOpen { from { transform: scale(0.985) translateY(6px); opacity: 0.3; } }
.titlebar { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 6px 8px 6px 10px; }
.brand { display: flex; align-items: center; gap: 8px; text-decoration: none; min-width: 0; }
.brand img { height: 22px; width: auto; display: block; }
.brand b { font-size: 14px; letter-spacing: 0.02em; white-space: nowrap; }
.brand b em { color: var(--gold); font-style: normal; }
.winctl { display: flex; gap: 3px; }
.winctl i {
  width: 16px; height: 14px; font-style: normal; font-size: 10px; line-height: 12px; text-align: center;
  border: 1px solid; border-color: var(--hi) var(--lo) var(--lo) var(--hi);
}
.tabs {
  display: flex; flex-wrap: wrap; gap: 2px; padding: 4px 10px 0;
  position: sticky; top: 0; z-index: 40; background: var(--win);
}
.tab {
  color: var(--mute); text-decoration: none; padding: 6px 14px 5px; font-weight: 700; font-size: 12px;
  letter-spacing: 0.04em; border-color: transparent; border-bottom: 0; white-space: nowrap; transition: color 0.15s;
}
.tab:hover { color: var(--text); }
.tab.active { color: var(--gold); background: var(--pane); border-color: var(--hi) var(--lo) transparent var(--hi); }
.tab .cnt {
  display: inline-block; min-width: 18px; margin-left: 5px; padding: 0 4px; background: #958831; color: #fff;
  font-size: 10px; text-align: center; vertical-align: 1px;
}
.tab .cnt:empty { display: none; }
.tab .cnt.bump { animation: bump 0.45s ease-out; }
@keyframes bump { 30% { transform: scale(1.5); background: var(--gold); color: var(--lo); } }
.pane { background: var(--pane); padding: 12px; display: flex; flex-direction: column; gap: 12px; min-width: 0; }
.toolbar { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px 12px; }
.nav { display: flex; align-items: center; gap: 4px; flex-wrap: wrap; }
.nav-arrow, .nav-mid, .btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 4px;
  background: var(--win); color: var(--text); text-decoration: none; font-weight: 700; font-size: 12px;
  padding: 4px 12px; min-height: 26px; cursor: pointer; white-space: nowrap;
}
.nav-arrow:hover, .nav-mid:hover, .btn:hover { color: var(--gold); }
.nav-arrow:active, .nav-mid:active, .btn:active { border-color: var(--lo) var(--hi) var(--hi) var(--lo); }
.nav-arrow.disabled { color: var(--dim); cursor: default; }
.global-search { display: flex; gap: 4px; flex: 0 1 320px; min-width: 0; margin: 0; }
.search-box {
  flex: 1; min-width: 0; width: 100%; background: var(--inset); color: var(--text);
  font-family: inherit; font-size: 13px; padding: 4px 8px; min-height: 26px; border-radius: 0;
}
.search-box:focus { outline: 1px dotted var(--gold); outline-offset: 1px; }
.search-box::placeholder { color: var(--dim); }
a:focus-visible, button:focus-visible, label:focus-within { outline: 1px dotted var(--gold); outline-offset: 1px; }

/* segmented "loading" bar: fills once per page load, then folds away */
.loadbar {
  display: grid; grid-template-columns: repeat(24, 1fr); gap: 2px; padding: 2px; height: 12px; overflow: hidden;
  border: 1px solid; border-color: var(--lo) var(--hi) var(--hi) var(--lo); background: var(--inset);
  animation: lbFold 0.2s ease-in 0.7s forwards;
}
.loadbar i { background: var(--green); opacity: 0; animation: seg 0.02s linear forwards; animation-delay: calc(var(--s) * 18ms); }
@keyframes seg { to { opacity: 1; } }
@keyframes lbFold { to { height: 0; padding: 0; border-width: 0; opacity: 0; margin-bottom: -12px; } }

/* status bar + deals ticker */
.status { display: flex; align-items: center; gap: 12px; padding: 5px 10px; font-size: 11px; color: var(--mute); border-top: 1px solid var(--hi); }
.status .led { width: 7px; height: 7px; background: var(--green); flex: none; animation: led 2.4s steps(1) infinite; }
@keyframes led { 92% { background: var(--lo); } }
.status .meta { flex: none; white-space: nowrap; margin: 0; font-size: 11px; }
.ticker {
  flex: 1; min-width: 0; overflow: hidden; white-space: nowrap;
  -webkit-mask-image: linear-gradient(90deg, transparent, #000 4%, #000 96%, transparent);
  mask-image: linear-gradient(90deg, transparent, #000 4%, #000 96%, transparent);
}
.ticker span { display: inline-block; padding-left: 100%; animation: tick 45s linear infinite; }
.ticker:hover span { animation-play-state: paused; }
.ticker em { font-style: normal; color: var(--green); }
@keyframes tick { to { transform: translateX(-100%); } }

/* ---- headings ---- */
.meta { color: var(--mute); font-size: 12px; }
h1 { font-size: 16px; margin: 0; color: var(--gold); }
.date-heading { display: flex; align-items: flex-end; justify-content: space-between; gap: 8px 12px; flex-wrap: wrap; }
.date-hero { display: flex; align-items: baseline; flex-wrap: wrap; gap: 4px 12px; }
.date-hero-date { font-size: 20px; margin: 0; line-height: 1.2; }
.date-hero-date .wd { font-size: 13px; font-weight: 400; color: var(--mute); margin-left: 8px; }
.date-hero-count { font-size: 12px; color: var(--mute); }
.adult-toggle { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--mute); cursor: pointer; user-select: none; }
.adult-toggle-input { accent-color: var(--gold); cursor: pointer; margin: 0; }
.hidden-count { color: var(--dim); }
body.show-adult .hidden-count { display: none; }
.row[data-adult="1"] { display: none; }
body.show-adult .row[data-adult="1"] { display: grid; }
h2.section {
  font-size: 12px; color: var(--gold); margin: 0; padding: 6px 8px; background: var(--win);
  border: 1px solid; border-color: var(--hi) var(--lo) var(--lo) var(--hi); border-bottom: 0;
}
h2.section + .card { margin-bottom: 12px; }
.card { background: var(--inset); min-width: 0; }

/* ---- a game row ---- */
.row {
  display: grid; grid-template-columns: 128px minmax(0, 1fr) 190px 86px;
  grid-template-areas: "media info buy wish"; align-items: center; gap: 4px 12px;
  padding: 8px 10px; border-bottom: 1px solid var(--line); position: relative; transition: background 0.12s;
}
.row:last-child { border-bottom: 0; }
.row:hover { background: var(--hover); box-shadow: inset 3px 0 0 var(--gold); }
.row.enter { animation: rowIn 0.26s ease-out both; animation-delay: calc(var(--i) * 32ms + 260ms); }
@keyframes rowIn { from { transform: translateX(-10px); opacity: 0; } }
.row .media { grid-area: media; display: block; }
.row img.cap {
  display: block; width: 128px; height: 60px; object-fit: cover; background: var(--win);
  border: 1px solid var(--lo);
}
.row .info { grid-area: info; min-width: 0; }
.row .name { display: block; font-size: 14px; font-weight: 700; line-height: 1.3; text-decoration: none; overflow-wrap: anywhere; }
.row .name:hover { color: var(--gold); }
.row .date-line { color: var(--mute); font-size: 11px; margin-top: 2px; }
.row .buy { grid-area: buy; display: flex; flex-direction: column; align-items: flex-end; gap: 3px; text-align: right; }
.price-line { display: flex; align-items: stretch; font-variant-numeric: tabular-nums; }
.disc-pct { background: var(--green-bg); color: var(--green); font-weight: 700; font-size: 12px; padding: 2px 6px; display: flex; align-items: center; }
.disc-pct.hot { animation: hot 1.6s steps(2) infinite; }
@keyframes hot { 50% { color: var(--gold); } }
.disc-prices { background: rgba(0, 0, 0, 0.35); display: flex; align-items: center; gap: 6px; padding: 2px 7px; }
.disc-orig { color: var(--dim); text-decoration: line-through; font-size: 11px; }
.disc-final { color: var(--green); font-weight: 700; font-size: 13px; }
.disc-final.plain { color: var(--text); }
.disc-final.free { color: var(--green); }
.disc-final.unknown { color: var(--mute); font-weight: 400; font-size: 12px; }
.discount-end { color: var(--blue); font-size: 11px; }
.review-line { font-size: 12px; margin-top: 3px; }
.discount-end.placeholder, .review-line.placeholder { display: none; }
.review-score { font-weight: 700; }
.review-score.pos { color: var(--blue); }
.review-score.mixed { color: var(--warn); }
.review-score.neg { color: var(--bad); }
.review-count { color: var(--dim); margin-left: 5px; }
.badge { font-size: 11px; white-space: nowrap; }
.badge.live { color: var(--dim); }
.badge.upcoming { color: var(--warn); font-weight: 700; }
.wish-star {
  grid-area: wish; justify-self: end; display: inline-flex; align-items: center; gap: 4px; position: relative;
  background: var(--win); color: var(--text); font-size: 12px; font-weight: 700; padding: 3px 8px; min-height: 26px;
  cursor: pointer; white-space: nowrap;
}
.wish-star:hover { color: var(--gold); }
.wish-star:active, .wish-star.filled { border-color: var(--lo) var(--hi) var(--hi) var(--lo); }
.wish-star.filled { color: var(--gold); background: var(--inset); }
.wish-star svg { width: 13px; height: 13px; display: block; flex: none; }
.wish-star svg path { fill: none; stroke: currentColor; stroke-width: 2; stroke-linejoin: round; }
.wish-star.filled svg path { fill: currentColor; stroke: none; }
.wish-star .w-on, .wish-star.filled .w-off { display: none; }
.wish-star.filled .w-on { display: inline; }
.plus { position: absolute; left: 50%; top: -4px; color: var(--gold); font-weight: 700; pointer-events: none; animation: plus 0.7s ease-out forwards; }
@keyframes plus { from { transform: translate(-50%, 0); } to { transform: translate(-50%, -22px); opacity: 0; } }
.tags { display: flex; flex-wrap: wrap; gap: 3px; margin-top: 5px; }
.tag {
  font-size: 11px; padding: 1px 6px; background: var(--win); color: var(--text); text-decoration: none;
  border: 1px solid; border-color: var(--hi) var(--lo) var(--lo) var(--hi); line-height: 1.5;
}
.tag:hover { color: var(--gold); }
button.tag { font-family: inherit; cursor: pointer; }
.tag-toggle { display: none; }
.tag-extra { display: none; }
.tag-toggle:checked ~ .tag-extra { display: contents; }
.tag-more { font-size: 11px; padding: 1px 6px; cursor: pointer; background: var(--inset); color: var(--mute); line-height: 1.5; }
.tag-more:hover { color: var(--gold); }
.tag-more-close { display: none; }
.tag-toggle:checked ~ .tag-more-open { display: none; }
.tag-toggle:checked ~ .tag-more-close { display: inline-block; }

/* hover preview (mouse only) */
.peek {
  position: fixed; z-index: 90; width: 300px; padding: 6px; background: var(--win); pointer-events: none;
  border: 1px solid; border-color: var(--hi) var(--lo) var(--lo) var(--hi); box-shadow: 0 8px 24px rgba(0, 0, 0, 0.5);
  opacity: 0; transform: translateY(6px); transition: opacity 0.12s, transform 0.12s;
}
.peek.on { opacity: 1; transform: none; }
.peek img { display: block; width: 100%; aspect-ratio: 460 / 215; object-fit: cover; background: var(--inset); }
.peek .t { font-weight: 700; margin: 6px 2px 4px; }
.peek .review-line { margin: 0 2px 4px; }
.peek .tags { margin: 0 2px; }

/* ---- home: list + deals pane ---- */
.split { display: grid; grid-template-columns: minmax(0, 1fr) 280px; gap: 12px; align-items: start; }
.split > * { min-width: 0; }
.side { display: flex; flex-direction: column; gap: 12px; position: sticky; top: 44px; }
.boxhead {
  display: flex; justify-content: space-between; align-items: baseline; gap: 8px; padding: 6px 8px;
  background: var(--win); border-bottom: 1px solid var(--lo); color: var(--gold); font-weight: 700; font-size: 12px;
}
.boxhead a { color: var(--mute); font-weight: 400; text-decoration: none; }
.boxhead a:hover { color: var(--gold); }
.box { background: var(--inset); }
.spot { overflow: hidden; }
.spot-track { display: flex; transition: transform 0.45s cubic-bezier(0.6, 0, 0.2, 1); }
.deal { flex: 0 0 100%; padding: 8px; display: flex; flex-direction: column; gap: 5px; text-decoration: none; min-width: 0; }
.deal img { display: block; width: 100%; aspect-ratio: 460 / 215; object-fit: cover; border: 1px solid var(--lo); background: var(--win); }
.deal .r { display: flex; justify-content: space-between; align-items: center; gap: 6px; }
.deal b { font-size: 12px; overflow-wrap: anywhere; }
.deal:hover b { color: var(--gold); }
.deal .pct { color: var(--green); font-weight: 700; }
.deal s { color: var(--dim); font-size: 11px; }
.deal .sm { color: var(--mute); font-size: 11px; }
.spot-timer { height: 2px; background: var(--gold); transform-origin: left; transform: scaleX(0); }
.spot-timer.go { animation: timer 5s linear forwards; }
@keyframes timer { to { transform: scaleX(1); } }
.spot-dots { display: flex; gap: 4px; justify-content: center; padding: 0 8px 8px; }
.spot-dots button { width: 11px; height: 11px; padding: 0; background: var(--win); cursor: pointer; border: 1px solid; border-color: var(--hi) var(--lo) var(--lo) var(--hi); }
.spot-dots button[aria-current="true"] { background: var(--gold); }

/* ---- toast + "open with" dialog ---- */
.toast {
  position: fixed; left: 50%; bottom: calc(24px + env(safe-area-inset-bottom, 0px));
  transform: translateX(-50%) translateY(12px); background: var(--win); color: var(--text);
  border: 1px solid; border-color: var(--hi) var(--lo) var(--lo) var(--hi);
  padding: 8px 16px; font-size: 12px; box-shadow: 0 8px 24px rgba(0, 0, 0, 0.5);
  opacity: 0; transition: opacity 0.2s ease, transform 0.2s ease;
  z-index: 200; pointer-events: none; max-width: calc(100vw - 32px); text-align: center;
}
.toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }
.open-modal-backdrop { display: none; position: fixed; inset: 0; background: rgba(20, 24, 16, 0.6); align-items: center; justify-content: center; z-index: 100; padding: 16px; }
.open-modal {
  position: relative; background: var(--win); width: min(320px, 100%);
  border: 1px solid; border-color: var(--hi) var(--lo) var(--lo) var(--hi); box-shadow: 0 16px 48px rgba(0, 0, 0, 0.55);
  animation: openModalIn 0.15s ease;
}
@keyframes openModalIn { from { opacity: 0; transform: scale(0.97); } }
.open-modal-title { font-size: 13px; font-weight: 700; padding: 6px 34px 6px 10px; }
.open-modal-x {
  position: absolute; top: 5px; right: 6px; width: 18px; height: 16px; padding: 0; font-size: 10px; line-height: 1;
  background: var(--win); color: var(--text); cursor: pointer; border: 1px solid; border-color: var(--hi) var(--lo) var(--lo) var(--hi);
}
.open-modal-body { background: var(--pane); margin: 0 4px 4px; padding: 14px; display: flex; flex-direction: column; gap: 14px; }
.open-modal-options { display: flex; gap: 10px; }
.open-modal-option {
  flex: 1; display: flex; flex-direction: column; align-items: center; gap: 6px; padding: 14px 8px;
  background: var(--win); color: var(--text); cursor: pointer;
}
.open-modal-option:hover { color: var(--gold); }
.open-modal-option:active { border-color: var(--lo) var(--hi) var(--hi) var(--lo); }
.open-modal-icon { width: 28px; height: 28px; font-size: 26px; line-height: 1; display: flex; align-items: center; justify-content: center; }
.open-modal-icon svg { width: 100%; height: 100%; display: block; }
.open-modal-label { font-size: 12px; font-weight: 700; }
.open-modal-remember { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--mute); cursor: pointer; }
.open-modal-remember input { accent-color: var(--gold); margin: 0; }

/* ---- dates index ---- */
.date-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(120px, 1fr)); gap: 8px; }
.date-card {
  background: var(--win); padding: 10px; display: flex; flex-direction: column; gap: 6px;
  text-decoration: none; color: inherit;
}
.date-card:hover { color: var(--gold); }
.date-card:active { border-color: var(--lo) var(--hi) var(--hi) var(--lo); }
.date-card.weekend { background: #545a3e; }
.date-card-top { display: flex; flex-direction: column; gap: 2px; }
.date-card-day { font-size: 22px; font-weight: 700; line-height: 1; }
.date-card-md { font-size: 11px; color: var(--mute); }
.date-card-count { font-size: 14px; font-weight: 700; color: var(--green); }
.date-card-count span { font-size: 11px; color: var(--mute); font-weight: 400; }
.date-month-divider { grid-column: 1 / -1; font-size: 12px; color: var(--gold); font-weight: 700; margin-top: 8px; padding-top: 10px; border-top: 1px solid var(--line); }
.date-month-divider:first-child { margin-top: 0; padding-top: 0; border-top: none; }

/* ---- historical low: filters + pager ---- */
.filter-tags { display: flex; flex-wrap: wrap; gap: 4px; }
.filter-chip { font-size: 12px; padding: 3px 9px; cursor: pointer; background: var(--win); color: var(--text); }
.filter-chip:hover { color: var(--gold); }
.filter-chip .n { color: var(--mute); font-size: 11px; margin-left: 5px; }
.filter-chip.active { border-color: var(--lo) var(--hi) var(--hi) var(--lo); background: var(--inset); color: var(--gold); }
.filter-chip.more { background: var(--inset); color: var(--mute); }
.low-head { display: flex; align-items: baseline; gap: 10px; }
.low-count { color: var(--mute); font-size: 12px; }
.pager { display: flex; justify-content: center; align-items: center; gap: 4px; flex-wrap: wrap; }
.pager button { min-width: 30px; height: 28px; padding: 0 8px; cursor: pointer; background: var(--win); color: var(--text); font-size: 12px; font-weight: 700; }
.pager button:hover:not(:disabled) { color: var(--gold); }
.pager button.current { border-color: var(--lo) var(--hi) var(--hi) var(--lo); background: var(--inset); color: var(--gold); }
.pager button:disabled { color: var(--dim); cursor: default; }
.pager .gap { color: var(--dim); padding: 0 2px; }
.empty { color: var(--mute); padding: 32px 12px; text-align: center; }

/* ---- narrow screens: every row stacks so nothing needs a sideways swipe ---- */
@media (max-width: 1000px) {
  .split { grid-template-columns: minmax(0, 1fr); }
  .side { position: static; }
}
@media (max-width: 700px) {
  body { padding: 8px 6px 24px; font-size: 14px; }
  .winctl { display: none; }
  .tabs { padding: 4px 4px 0; }
  .tab { padding: 6px 9px 5px; }
  .pane { padding: 8px; }
  .toolbar { flex-direction: column; align-items: stretch; }
  .nav { justify-content: space-between; }
  .nav .nav-mid { flex: 1; }
  .global-search { flex: 1 1 auto; }
  .row {
    grid-template-columns: 112px minmax(0, 1fr) auto;
    grid-template-areas: "media info info" "buy buy wish";
    padding: 10px 8px; gap: 8px 10px; align-items: start;
  }
  .row img.cap { width: 112px; height: 52px; }
  .row .name { font-size: 14px; }
  .row .buy { flex-direction: row; flex-wrap: wrap; align-items: center; gap: 4px 10px; text-align: left; align-self: center; }
  .wish-star { align-self: center; }
  .status .meta { display: none; }
  .open-modal-options { flex-direction: column; }
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation: none !important; transition: none !important; }
  .loadbar { display: none; }
  .ticker span { padding-left: 0; }
}
"""

STYLE_HASH = hashlib.md5(STYLE_CSS.encode("utf-8")).hexdigest()[:8]

PAGE_SHELL = """<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<link rel="icon" href="__ASSET_BASE__assets/logo.png">
<link rel="stylesheet" href="__ASSET_BASE__assets/style.css?v=__CSS_VER__">
__OG__
</head>
<body>
<script>try { if (localStorage.getItem("showAdultContent") === "1") document.body.className = "show-adult"; } catch (e) {}</script>
<div class="win">
  <div class="titlebar">
    <a class="brand" href="__HOME_HREF__"><img src="__ASSET_BASE__assets/logo.png" alt=""><b>Steam<em>美食家</em></b></a>
    <div class="winctl" aria-hidden="true"><i>_</i><i>□</i><i>×</i></div>
  </div>
  <nav class="tabs" aria-label="分類">__TABS__</nav>
  <div class="pane">
    <div class="toolbar">
      <div class="nav">__NAV__</div>
      <form class="global-search" action="__ASSET_BASE__search.html" method="get" role="search">
        <input type="text" name="q" id="globalSearch" class="search-box" placeholder="搜尋遊戲名稱…" autocomplete="off" aria-label="搜尋遊戲名稱">
        <button type="submit" class="btn">搜尋</button>
      </form>
    </div>
    <div class="loadbar" aria-hidden="true">__LOADBAR__</div>
    __BODY__
  </div>
  <div class="status"><i class="led" aria-hidden="true"></i><span class="meta">__META__</span><div class="ticker"><span>__TICKER__</span></div></div>
</div>

<div class="peek" id="peek" aria-hidden="true"></div>
<div class="open-modal-backdrop" id="openModalBackdrop">
  <div class="open-modal" role="dialog" aria-labelledby="openModalTitle">
    <div class="open-modal-title" id="openModalTitle">要用什麼開啟？</div>
    <button type="button" class="open-modal-x" id="openModalCancel" aria-label="關閉">✕</button>
    <div class="open-modal-body">
      <div class="open-modal-options">
        <button type="button" class="open-modal-option" data-choice="web">
          <span class="open-modal-icon">🌐</span>
          <span class="open-modal-label">網頁</span>
        </button>
        <button type="button" class="open-modal-option" data-choice="steam">
          <span class="open-modal-icon"><svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M11.979 0C5.678 0 .511 4.86.022 11.037l6.432 2.658c.545-.371 1.203-.59 1.912-.59.063 0 .125.004.188.006l2.861-4.142V8.91c0-2.495 2.028-4.524 4.524-4.524 2.494 0 4.524 2.031 4.524 4.527s-2.03 4.525-4.524 4.525h-.105l-4.076 2.911c0 .052.004.105.004.159 0 1.875-1.515 3.396-3.39 3.396-1.635 0-3.016-1.173-3.331-2.727L.436 15.27C1.862 20.307 6.486 24 11.979 24c6.627 0 11.999-5.373 11.999-12S18.606 0 11.979 0zM7.54 18.21l-1.473-.61c.262.543.714.999 1.314 1.25 1.297.539 2.793-.076 3.332-1.375.263-.63.264-1.319.005-1.949s-.75-1.121-1.377-1.383c-.624-.26-1.29-.249-1.878-.03l1.523.63c.956.4 1.409 1.5 1.009 2.455-.397.957-1.497 1.41-2.454 1.012H7.54zm11.415-9.303c0-1.662-1.353-3.015-3.015-3.015-1.665 0-3.015 1.353-3.015 3.015 0 1.665 1.35 3.015 3.015 3.015 1.663 0 3.015-1.35 3.015-3.015zm-5.273-.005c0-1.252 1.013-2.266 2.265-2.266 1.249 0 2.266 1.014 2.266 2.266 0 1.251-1.017 2.265-2.266 2.265-1.253 0-2.265-1.014-2.265-2.265z"/></svg></span>
          <span class="open-modal-label">Steam</span>
        </button>
      </div>
      <label class="open-modal-remember"><input type="checkbox" id="openModalRemember" checked>記住我的選擇</label>
    </div>
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
function showToast(text) {
  var el = document.getElementById("wishToast");
  if (!el) {
    el = document.createElement("div");
    el.id = "wishToast";
    el.className = "toast";
    document.body.appendChild(el);
  }
  el.textContent = text;
  el.classList.remove("show");
  void el.offsetWidth; // restart the transition even if a toast is already showing
  el.classList.add("show");
  clearTimeout(el._hideTimer);
  el._hideTimer = setTimeout(function () { el.classList.remove("show"); }, 2200);
}
var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
(function () {
  // Rows slide in one after another on page load, like the old client filling its list.
  // Only the first screenful animates - search/tag pages hold thousands of rows.
  if (reduceMotion) return;
  var n = 0;
  document.querySelectorAll(".row").forEach(function (r) {
    if (n >= 24 || r.offsetParent === null) return;
    r.style.setProperty("--i", n++);
    r.classList.add("enter");
    r.addEventListener("animationend", function () { r.classList.remove("enter"); }, { once: true });
  });
})();
(function () {
  // Mouse-only hover preview: a bigger header image plus the row's review line and tags.
  if (!window.matchMedia("(hover: hover) and (pointer: fine)").matches) return;
  var peek = document.getElementById("peek"), cur = null;
  document.addEventListener("mousemove", function (e) {
    var row = e.target.closest(".row");
    if (!row || e.target.closest(".wish-star, .tags")) { peek.classList.remove("on"); cur = null; return; }
    if (row !== cur) {
      cur = row;
      var img = row.querySelector("img.cap"), name = row.querySelector(".name"), rev = row.querySelector(".review-line:not(.placeholder)");
      var tags = Array.prototype.slice.call(row.querySelectorAll(".tags .tag"), 0, 10);
      peek.innerHTML = "";
      if (img && img.style.visibility !== "hidden") { var i = document.createElement("img"); i.src = img.currentSrc || img.src; i.alt = ""; peek.appendChild(i); }
      var t = document.createElement("div"); t.className = "t"; t.textContent = name ? name.textContent : ""; peek.appendChild(t);
      if (rev) peek.appendChild(rev.cloneNode(true));
      if (tags.length) {
        var box = document.createElement("div"); box.className = "tags";
        tags.forEach(function (a) { var s = document.createElement("span"); s.className = "tag"; s.textContent = a.textContent; box.appendChild(s); });
        peek.appendChild(box);
      }
    }
    var x = e.clientX + 18, y = e.clientY + 14;
    if (x + 310 > window.innerWidth) x = e.clientX - 318;
    if (y + peek.offsetHeight + 10 > window.innerHeight) y = Math.max(8, e.clientY - peek.offsetHeight - 14);
    peek.style.left = x + "px"; peek.style.top = y + "px";
    peek.classList.add("on");
  });
  document.addEventListener("mouseleave", function () { peek.classList.remove("on"); cur = null; });
})();
(function () {
  // Wishlist lives entirely in localStorage (no account, no backend) - each wish button
  // carries enough data-* attributes to reconstruct its own row, so the wishlist page can
  // render fully client-side without ever needing to fetch history.json.
  var KEY = "wishlist";
  function getWishlist() {
    try { return JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (e) { return {}; }
  }
  function setWishlist(w) {
    try { localStorage.setItem(KEY, JSON.stringify(w)); } catch (e) {}
  }
  function syncStars() {
    var w = getWishlist();
    document.querySelectorAll(".wish-star").forEach(function (btn) {
      var on = !!w[btn.getAttribute("data-appid")];
      btn.classList.toggle("filled", on);
      btn.setAttribute("aria-label", on ? "移除願望清單" : "加入願望清單");
    });
    var cnt = document.getElementById("wishCount");
    if (cnt) cnt.textContent = Object.keys(w).length || "";
  }
  function bump() {
    var cnt = document.getElementById("wishCount");
    if (!cnt || reduceMotion) return;
    cnt.classList.remove("bump"); void cnt.offsetWidth; cnt.classList.add("bump");
  }
  document.addEventListener("click", function (e) {
    var btn = e.target.closest(".wish-star");
    if (!btn) return;
    e.preventDefault();
    var w = getWishlist();
    var appid = btn.getAttribute("data-appid");
    var name = btn.getAttribute("data-name");
    if (w[appid]) {
      delete w[appid];
      setWishlist(w);
      showToast("已將「" + name + "」移除願望清單");
    } else {
      w[appid] = {
        name: name,
        web: btn.getAttribute("data-web"),
        image: btn.getAttribute("data-image"),
        status: btn.getAttribute("data-status"),
        badge: btn.getAttribute("data-badge"),
        price: btn.getAttribute("data-price"),
        addedAt: Date.now()
      };
      setWishlist(w);
      showToast("已將「" + name + "」加入願望清單");
      if (!reduceMotion) {
        var p = document.createElement("span");
        p.className = "plus"; p.textContent = "+1";
        btn.appendChild(p);
        setTimeout(function () { p.remove(); }, 700);
      }
    }
    syncStars();
    bump();
    document.dispatchEvent(new Event("wishlistchange"));
  });
  syncStars();
})();
(function () {
  var KEY = "showAdultContent";
  var boxes = document.querySelectorAll(".adult-toggle-input");
  var showing = document.body.classList.contains("show-adult");
  boxes.forEach(function (cb) {
    cb.checked = showing;
    cb.addEventListener("change", function () {
      document.body.classList.toggle("show-adult", cb.checked);
      boxes.forEach(function (other) { other.checked = cb.checked; });
      try { localStorage.setItem(KEY, cb.checked ? "1" : "0"); } catch (e) {}
      document.dispatchEvent(new Event("adulttoggle"));
    });
  });
})();
(function () {
  // Historical-low spotlight on the home page: one deal at a time, advancing every 5s,
  // paused while the pointer is over it.
  var spot = document.getElementById("spot");
  if (!spot) return;
  var track = spot.querySelector(".spot-track"), timer = spot.querySelector(".spot-timer");
  var dots = Array.prototype.slice.call(spot.querySelectorAll(".spot-dots button"));
  var k = 0, iv = null;
  function go(n) {
    k = (n + dots.length) % dots.length;
    track.style.transform = "translateX(-" + (k * 100) + "%)";
    dots.forEach(function (d, i) { d.setAttribute("aria-current", i === k ? "true" : "false"); });
    timer.classList.remove("go"); void timer.offsetWidth;
    if (!reduceMotion) timer.classList.add("go");
  }
  function auto() { clearInterval(iv); if (!reduceMotion) iv = setInterval(function () { go(k + 1); }, 5000); }
  dots.forEach(function (d, i) { d.addEventListener("click", function () { go(i); auto(); }); });
  spot.addEventListener("mouseenter", function () { clearInterval(iv); timer.classList.remove("go"); });
  spot.addEventListener("mouseleave", function () { go(k); auto(); });
  go(0); auto();
})();
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
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") closeModal(); });
  backdrop.querySelectorAll("[data-choice]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var choice = btn.getAttribute("data-choice");
      if (remember.checked) setPref(choice);
      openWith(choice);
      closeModal();
    });
  });

  var isMobile = /Mobi|Android|iPhone|iPad|iPod/.test(navigator.userAgent);

  document.addEventListener("click", function (e) {
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var el = e.target.closest("[data-web][data-steam]");
    if (!el) return;
    e.preventDefault();
    if (isMobile) {
      // steam:// only means anything to the desktop client - it's not a scheme the mobile
      // Steam app registers, so trying it here just throws an "invalid URL" error. Mobile
      // app-opening works through Universal/App Links on the normal https:// URL instead,
      // handled entirely by the OS - so there's nothing for this "web or Steam" choice to
      // actually choose between on a phone, and asking is just a broken extra tap.
      window.location.href = el.getAttribute("data-web");
      return;
    }
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

def render_page(
    title: str,
    base: str,
    nav_html: str,
    meta: str,
    body_html: str,
    og_description: str = "",
    og_image: str = "",
    canonical_url: str = "",
    tab: str = "",
    ticker: str = "",
) -> str:
    og_html = ""
    if canonical_url:
        # These are deliberately different: og:site_name is the small line Discord/LINE
        # show above the link (OG_SITE_NAME), og:title is the bold clickable title below it
        # and matches the page's own title (SITE_NAME already baked into `title`).
        og_html = (
            '<meta property="og:type" content="website">\n'
            f'<meta property="og:site_name" content="{esc(OG_SITE_NAME)}">\n'
            f'<meta property="og:title" content="{esc(title)}">\n'
            f'<meta property="og:description" content="{esc(og_description)}">\n'
            f'<meta property="og:url" content="{esc(canonical_url)}">\n'
            f'<meta property="og:image" content="{esc(og_image)}">\n'
            '<meta name="twitter:card" content="summary_large_image">'
        )
    return (
        PAGE_SHELL.replace("__TITLE__", esc(title))
        .replace("__SITE_NAME__", esc(SITE_NAME))
        .replace("__ASSET_BASE__", base)
        .replace("__CSS_VER__", STYLE_HASH)
        .replace("__HOME_HREF__", f"{base}index.html")
        .replace("__NAV__", nav_html)
        .replace("__META__", meta)
        .replace("__BODY__", body_html)
        .replace("__OG__", og_html)
        .replace("__TABS__", render_tabs(tab, base))
        .replace("__LOADBAR__", LOADBAR_HTML)
        .replace("__TICKER__", ticker)
    )


# The window's tab strip replaces the old sidebar. `current` is one of the keys below
# (empty on pages that aren't a tab of their own, e.g. a tag page).
TABS = [
    ("home", "今日新作", "index.html"),
    ("dates", "所有日期", "dates/index.html"),
    ("low", "歷史新低", "historical-low.html"),
    ("wish", "願望清單", "wishlist.html"),
]
LOADBAR_HTML = "".join(f'<i style="--s:{i}"></i>' for i in range(24))


def render_tabs(current: str, base: str) -> str:
    parts = []
    for key, label, href in TABS:
        cls = "tab active" if key == current else "tab"
        aria = ' aria-current="page"' if key == current else ""
        extra = '<span class="cnt" id="wishCount"></span>' if key == "wish" else ""
        parts.append(f'<a class="{cls}" href="{base}{href}"{aria}>{label}{extra}</a>')
    return "".join(parts)


def render_ticker(low_games: list[dict]) -> str:
    # Status-bar ticker: the most-reviewed historical lows, same order as that page.
    items = [
        f'{esc(g["name"])} <em>{esc(g.get("price_pct") or "")}</em> {esc(g.get("price_final") or "")}'
        for g in low_games[:10]
    ]
    return "　　·　　".join(items)


def render_spotlight(low_games: list[dict], base: str) -> str:
    # Home page side pane: a few historical lows rotating one at a time (see the #spot
    # script in PAGE_SHELL).
    picks = [g for g in low_games if not g.get("is_adult")][:6]
    if not picks:
        return ""
    deals = []
    for g in picks:
        web_url = esc(g["url"])
        img = esc(g.get("header_image") or g.get("image") or "")
        end = g.get("discount_end")
        end_html = (
            f'<span>{datetime.fromtimestamp(end, tz=LOCAL_TZ).strftime("%m/%d")} 截止</span>' if end else ""
        )
        review = esc(g.get("review_score_label") or "")
        pct = g.get("review_percent")
        review_html = f"{review} {pct}%" if review and pct is not None else review
        first_tag = esc(g["tags"][0]) if g.get("tags") else ""
        deals.append(
            f'<a class="deal" href="{web_url}" data-web="{web_url}" data-steam="steam://store/{esc(g["appid"])}">'
            f'<img src="{img}" loading="lazy" alt="">'
            f'<span class="r"><b>{esc(g["name"])}</b><span class="pct">{esc(g.get("price_pct") or "")}</span></span>'
            f'<span class="r"><span class="sm">{review_html}</span>'
            f'<span>{esc(g.get("price_final") or "")} <s>{esc(g.get("price_original") or "")}</s></span></span>'
            f'<span class="r sm"><span>{first_tag}</span>{end_html}</span>'
            "</a>"
        )
    dots = "".join(
        f'<button type="button" aria-label="第 {i + 1} 款：{esc(g["name"])}"></button>' for i, g in enumerate(picks)
    )
    return (
        '<div class="box spot" id="spot">'
        f'<div class="boxhead">歷史新低<a href="{base}historical-low.html">查看全部 ›</a></div>'
        '<div class="spot-timer"></div>'
        f'<div class="spot-track">{"".join(deals)}</div>'
        f'<div class="spot-dots">{dots}</div>'
        "</div>"
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


def render_tags(appid: str, tags: list[str], base: str, links: bool = True) -> str:
    if not tags:
        return ""

    def chip(t: str) -> str:
        if not links:
            return f'<button type="button" class="tag" data-tag="{esc(t)}">{esc(t)}</button>'
        return f'<a class="tag" href="{base}tags/{tag_slug(t)}.html">{esc(t)}</a>'

    visible = "".join(chip(t) for t in tags[:TAGS_VISIBLE])
    rest = tags[TAGS_VISIBLE:]
    if not rest:
        return f'<div class="tags">{visible}</div>'

    # Tags come back from the API already sorted by weight (Steam's own popularity order),
    # so the first N are already the "priority" ones.
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


def _pct_value(pct: str) -> int:
    try:
        return int(str(pct).strip().rstrip("%"))
    except ValueError:
        return 0


def render_price(g: dict) -> str:
    final = g.get("price_final")
    if not final:
        return '<div class="price-line"><span class="disc-final unknown">價格未知</span></div>'

    final_class = "disc-final free" if final == "免費" else "disc-final"
    pct, original = g.get("price_pct"), g.get("price_original")
    if pct and original:
        html = (
            '<div class="price-line">'
            f'<span class="disc-pct{" hot" if _pct_value(pct) <= -50 else ""}">{esc(pct)}</span>'
            '<span class="disc-prices">'
            f'<span class="disc-orig">{esc(original)}</span>'
            f'<span class="{final_class}">{esc(final)}</span>'
            "</span></div>"
        )
    else:
        html = f'<div class="price-line"><span class="{final_class} plain">{esc(final)}</span></div>'

    end = g.get("discount_end")
    if end:
        dt = datetime.fromtimestamp(end, tz=LOCAL_TZ)
        html += f'<div class="discount-end">優惠至 {dt.strftime("%m/%d")} 截止</div>'
    else:
        # A game with no discount still reserves this line's height (invisible, not absent)
        # so every row is the same height whether or not that particular game has one -
        # otherwise a discounted/reviewed game's row visibly grows taller than a plain one's.
        html += '<div class="discount-end placeholder">&nbsp;</div>'
    return html


def render_review(g: dict) -> str:
    score, count = g.get("review_score"), g.get("review_count")
    if not score or not count:
        # Too few reviews yet for Steam to have scored it - keep the line's height reserved
        # (see render_price's discount-end placeholder for why) rather than omitting it.
        return '<div class="review-line placeholder">&nbsp;</div>'
    # Steam's 9 buckets aren't evenly split around "mixed" - only score 5 is actually
    # Mixed; 6 (Mostly Positive) and 4 (Mostly Negative) already lean to one side.
    tier = "pos" if score >= 6 else "neg" if score <= 4 else "mixed"
    label = g.get("review_score_label") or ""
    pct = g.get("review_percent")
    pct_html = f"（{pct}%）" if pct is not None else ""
    return (
        f'<div class="review-line"><span class="review-score {tier}">{esc(label)}{pct_html}</span>'
        f'<span class="review-count">{count:,} 篇評論</span></div>'
    )


def render_row(g: dict, base: str, show_date: bool = False, tag_links: bool = True) -> str:
    if g["status"] == "live":
        badge_label = "已上架"
        badge = f'<span class="badge live">{badge_label}</span>'
    else:
        epoch = g.get("release_epoch")
        if epoch:
            dt = datetime.fromtimestamp(epoch, tz=LOCAL_TZ)
            badge_label = dt.strftime("%m/%d %H:%M")
        else:
            badge_label = "預計上架"
        badge = f'<span class="badge upcoming">⏳ {esc(badge_label)}</span>'

    date_html = f'<div class="date-line">{esc(g["release_date"])}</div>' if show_date else ""
    review_html = render_review(g)
    tags_html = render_tags(g["appid"], g.get("tags", []), base, links=tag_links)
    fallback_image = g.get("image") or ""
    zoom_image = g.get("header_image") or fallback_image
    web_url = esc(g["url"])
    steam_url = f"steam://store/{esc(g['appid'])}"
    open_attrs = f'data-web="{web_url}" data-steam="{steam_url}"'
    adult_attr = ' data-adult="1"' if g.get("is_adult") else ""

    # data-* here carries everything the wishlist page needs to render this card entirely
    # client-side from localStorage - it has no access to history.json, so this is the only
    # copy of the game's display info it will ever have (a snapshot as of when starred).
    price_text = esc(g.get("price_final") or "價格未知")
    star_attrs = (
        f'data-appid="{esc(g["appid"])}" data-name="{esc(g["name"])}" data-web="{web_url}" '
        f'data-image="{esc(zoom_image)}" data-status="{esc(g["status"])}" '
        f'data-badge="{esc(badge_label)}" data-price="{price_text}"'
    )
    star_html = f'<button type="button" class="wish-star" {star_attrs} aria-label="加入願望清單">{WISH_BTN_HTML}</button>'

    return (
        f'<div class="row"{adult_attr}>'
        f'<a class="media" href="{web_url}" {open_attrs}>'
        f'<img class="cap" src="{esc(zoom_image)}" data-fallback="{esc(fallback_image)}" '
        f'onerror="imgFallback(this)" loading="lazy" alt=""></a>'
        '<div class="info">'
        f'<a class="name" href="{web_url}" {open_attrs}>{esc(g["name"])}</a>'
        f"{date_html}{review_html}{tags_html}</div>"
        f'<div class="buy">{render_price(g)}{badge}</div>'
        f"{star_html}</div>"
    )


def render_games_section(title: str, games: list[dict], base: str, show_date: bool = False) -> str:
    if not games:
        return ""
    rows = "".join(render_row(g, base, show_date) for g in games)
    return f'<h2 class="section">{esc(title)}</h2><div class="card">{rows}</div>'


def render_date_body(date_str: str, games: list[dict], base: str) -> str:
    # Most reviews first - a rough proxy for "what's actually catching people's attention",
    # not just an alphabetical wall of names. Games with no review_count yet (very common for
    # something that launched minutes ago) sort to the end via the 0 fallback, then by name.
    live = sorted(
        [g for g in games if g["status"] == "live"],
        key=lambda g: (-(g.get("review_count") or 0), g["name"]),
    )
    upcoming = sorted(
        [g for g in games if g["status"] == "upcoming"],
        key=lambda g: (g.get("release_epoch") is None, g.get("release_epoch") or 0, g["name"]),
    )
    adult_count = sum(1 for g in games if g.get("is_adult"))
    adult_toggle = (
        '<label class="adult-toggle">'
        '<input type="checkbox" class="adult-toggle-input">'
        "顯示成人內容"
        f'<span class="hidden-count">（已隱藏 {adult_count} 款）</span>'
        "</label>"
    ) if adult_count else ""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    hero = (
        '<div class="date-hero">'
        f'<h1 class="date-hero-date">{dt.month}月{dt.day}日'
        f'<span class="wd">星期{WEEKDAY_ZH[dt.weekday()]}</span></h1>'
        f'<div class="date-hero-count">{len(games)} 款新遊戲</div>'
        "</div>"
    )
    body = f'<div class="date-heading">{hero}{adult_toggle}</div>'
    if not games:
        return body + '<div class="empty">當天沒有資料</div>'
    body += render_games_section("已上架", live, base)
    body += render_games_section("⏳ 預計上架", upcoming, base)
    return body


def build_nav(dates_desc: list[str], current: str, base: str) -> str:
    idx = dates_desc.index(current)
    older = dates_desc[idx + 1] if idx + 1 < len(dates_desc) else None
    newer = dates_desc[idx - 1] if idx > 0 else None

    def arrow(symbol: str, label: str, target: str | None) -> str:
        if target is None:
            return f'<span class="nav-arrow disabled" aria-label="{esc(label)}">{symbol}</span>'
        return f'<a class="nav-arrow" href="{base}dates/{target}.html" aria-label="{esc(label)}">{symbol}</a>'

    return (
        arrow("←", "前一天", older)
        + f'<a class="nav-mid" href="{base}dates/index.html">所有日期</a>'
        + arrow("→", "後一天", newer)
    )


def _hero_image(games: list[dict], site_url: str) -> str:
    for g in games:
        img = g.get("header_image") or g.get("image")
        if img:
            return img
    return site_url + "assets/logo.png"


# Tag counts are built client-side from each row's own tag chips (rather than baked in at
# generation time) so they respect the adult-content toggle - hidden adult games shouldn't
# put "Hentai" in the top 7.
HISTORICAL_LOW_SCRIPT = """<script>
(function () {
  var PER_PAGE = 15, FIRST_TAGS = 7, TAG_STEP = 20;
  var rows = Array.prototype.slice.call(document.querySelectorAll("#lowResults .row"));
  rows.forEach(function (r) {
    r._tags = Array.prototype.map.call(r.querySelectorAll(".tags .tag"), function (a) { return a.textContent; });
  });
  var tagBar = document.getElementById("lowTags");
  var pager = document.getElementById("lowPager");
  var countEl = document.getElementById("lowCount");
  var emptyEl = document.getElementById("lowEmpty");
  var activeTag = null, page = 1, tagLimit = FIRST_TAGS;

  function eligible() {
    var showAdult = document.body.classList.contains("show-adult");
    return rows.filter(function (r) { return showAdult || r.getAttribute("data-adult") !== "1"; });
  }
  function chip(label, n, cls, onClick) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = "filter-chip" + (cls ? " " + cls : "");
    b.textContent = label;
    if (n !== null) {
      var s = document.createElement("span");
      s.className = "n";
      s.textContent = n;
      b.appendChild(s);
    }
    b.addEventListener("click", onClick);
    return b;
  }
  function renderTags(pool) {
    var counts = {};
    pool.forEach(function (r) { r._tags.forEach(function (t) { counts[t] = (counts[t] || 0) + 1; }); });
    var tags = Object.keys(counts).sort(function (a, b) { return counts[b] - counts[a] || a.localeCompare(b); });
    if (activeTag && !counts[activeTag]) activeTag = null;
    tagBar.innerHTML = "";
    var shown = tags.slice(0, tagLimit);
    if (activeTag && shown.indexOf(activeTag) === -1) shown.push(activeTag);
    shown.forEach(function (t) {
      tagBar.appendChild(chip(t, counts[t], t === activeTag ? "active" : "", function () {
        activeTag = activeTag === t ? null : t;
        page = 1;
        render();
      }));
    });
    if (tags.length > tagLimit) {
      tagBar.appendChild(chip("…", null, "more", function () { tagLimit += TAG_STEP; render(); }));
    } else if (tagLimit > FIRST_TAGS) {
      tagBar.appendChild(chip("收起", null, "more", function () { tagLimit = FIRST_TAGS; render(); }));
    }
  }
  function pageButton(label, target, cls, disabled) {
    var b = document.createElement("button");
    b.type = "button";
    b.textContent = label;
    if (cls) b.className = cls;
    b.disabled = !!disabled;
    b.addEventListener("click", function () {
      page = target;
      render();
      scrollToList();
    });
    return b;
  }
  function scrollToList() {
    var top = document.getElementById("lowTop").getBoundingClientRect().top + window.pageYOffset;
    var bar = document.querySelector(".topbar");
    window.scrollTo(0, top - (bar ? bar.offsetHeight : 0) - 12);
  }
  document.getElementById("lowResults").addEventListener("click", function (e) {
    var t = e.target.closest("button.tag[data-tag]");
    if (!t) return;
    activeTag = t.getAttribute("data-tag");
    page = 1;
    render();
    scrollToList();
  });
  function renderPager(pages) {
    pager.innerHTML = "";
    if (pages <= 1) return;
    pager.appendChild(pageButton("‹", page - 1, "", page === 1));
    var last = 0;
    for (var p = 1; p <= pages; p++) {
      if (p === 1 || p === pages || Math.abs(p - page) <= 1) {
        if (last && p - last > 1) {
          var gap = document.createElement("span");
          gap.className = "gap";
          gap.textContent = "…";
          pager.appendChild(gap);
        }
        pager.appendChild(pageButton(String(p), p, p === page ? "current" : "", false));
        last = p;
      }
    }
    pager.appendChild(pageButton("›", page + 1, "", page === pages));
  }
  function render() {
    var pool = eligible();
    renderTags(pool);
    var matched = activeTag ? pool.filter(function (r) { return r._tags.indexOf(activeTag) !== -1; }) : pool;
    var pages = Math.max(1, Math.ceil(matched.length / PER_PAGE));
    if (page > pages) page = pages;
    var start = (page - 1) * PER_PAGE;
    var visible = matched.slice(start, start + PER_PAGE);
    rows.forEach(function (r) { r.style.display = "none"; });
    visible.forEach(function (r) { r.style.display = ""; });
    countEl.textContent = (activeTag ? activeTag + "：" : "") + matched.length + " 款";
    emptyEl.style.display = matched.length ? "none" : "";
    document.getElementById("lowResults").style.display = matched.length ? "" : "none";
    renderPager(pages);
  }
  document.addEventListener("adulttoggle", function () { page = 1; render(); });
  render();
})();
</script>"""


def generate_site(history: dict, docs_dir: Path, retention_days: int, today: date, site_url: str) -> None:
    by_date: dict[str, list[dict]] = {}
    for g in history["games"].values():
        by_date.setdefault(g["release_date"], []).append(g)
    today_str = today.isoformat()
    by_date.setdefault(today_str, [])  # today always gets a page, even before any data exists for it
    dates_desc = sorted(by_date, reverse=True)
    counts = {d: len(by_date[d]) for d in dates_desc}
    generated_at = datetime.now(tz=LOCAL_TZ).strftime("%Y-%m-%d %H:%M")
    meta = f"已連線 · 上次同步 {esc(generated_at)} · 顯示最近 {retention_days} 天"
    low_games = sorted(
        history.get("historical_low", []),
        key=lambda g: (-(g.get("review_count") or 0), g["name"]),
    )
    ticker = render_ticker([g for g in low_games if not g.get("is_adult")])

    def page_html(**kw) -> str:  # every page shares the same status-bar ticker
        return render_page(ticker=ticker, **kw)

    dates_dir = docs_dir / "dates"
    dates_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "assets").mkdir(parents=True, exist_ok=True)
    style_path = docs_dir / "assets" / "style.css"
    if not style_path.exists() or style_path.read_text(encoding="utf-8") != STYLE_CSS:
        style_path.write_text(STYLE_CSS, encoding="utf-8")

    for d in dates_desc:
        page = page_html(
            title=f"{d} 新遊戲 - {SITE_NAME}",
            base="../",
            nav_html=build_nav(dates_desc, d, "../"),
            meta=meta,
            body_html=render_date_body(d, by_date[d], "../"),
            og_description=f"{d} 新上架 Steam 遊戲，共 {counts[d]} 款",
            og_image=_hero_image(by_date[d], site_url),
            canonical_url=f"{site_url}dates/{d}.html",
        )
        (dates_dir / f"{d}.html").write_text(page, encoding="utf-8")

    def date_card(d: str) -> str:
        dt = datetime.strptime(d, "%Y-%m-%d")
        weekend = " weekend" if dt.weekday() >= 5 else ""
        return (
            f'<a class="date-card{weekend}" href="../dates/{d}.html">'
            f'<div class="date-card-top"><span class="date-card-day">{dt.day}</span>'
            f'<span class="date-card-md">{dt.month}月 · 週{WEEKDAY_ZH[dt.weekday()]}</span></div>'
            f'<div class="date-card-count">{counts[d]} <span>款</span></div>'
            "</a>"
        )

    if dates_desc:
        parts = []
        current_month = None
        for d in dates_desc:
            dt = datetime.strptime(d, "%Y-%m-%d")
            month_key = (dt.year, dt.month)
            if month_key != current_month:
                current_month = month_key
                parts.append(f'<div class="date-month-divider">{dt.year} 年 {dt.month} 月</div>')
            parts.append(date_card(d))
        dates_index_rows = "".join(parts)
    else:
        dates_index_rows = '<div class="empty">尚無資料</div>'
    dates_index_page = page_html(
        title=f"所有日期 - {SITE_NAME}",
        base="../",
        nav_html="",
        meta=meta,
        body_html=f'<h1>所有日期</h1><div class="date-grid">{dates_index_rows}</div>',
        og_description=f"瀏覽最近 {retention_days} 天內每日上架的新遊戲",
        og_image=f"{site_url}assets/logo.png",
        canonical_url=f"{site_url}dates/index.html",
        tab="dates",
    )
    (dates_dir / "index.html").write_text(dates_index_page, encoding="utf-8")

    if dates_desc:
        # Home is always "today", not just whatever date happens to have the most recent
        # data - a stray game or two already filed under tomorrow (normal near midnight)
        # shouldn't make the homepage jump ahead of the actual current day.
        home_page = page_html(
            title=SITE_NAME,
            base="",
            nav_html=build_nav(dates_desc, today_str, ""),
            meta=meta,
            body_html=(
                f'<div class="split"><div class="main">{render_date_body(today_str, by_date[today_str], "")}</div>'
                f'<aside class="side">{render_spotlight(low_games, "")}</aside></div>'
            ),
            og_description=f"{today_str} 新上架 Steam 遊戲，共 {counts[today_str]} 款",
            og_image=_hero_image(by_date[today_str], site_url),
            canonical_url=site_url,
            tab="home",
        )
    else:
        home_page = page_html(
            title=SITE_NAME,
            base="",
            nav_html="",
            meta=meta,
            body_html=f'<h1>{esc(SITE_NAME)}</h1><div class="empty">尚無資料</div>',
            og_description="每日追蹤 Steam 新上架遊戲",
            og_image=f"{site_url}assets/logo.png",
            canonical_url=site_url,
            tab="home",
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
        page = page_html(
            title=f"#{tag} - {SITE_NAME}",
            base="../",
            nav_html="",
            meta=meta,
            body_html=f'<h1>#{esc(tag)}</h1><div class="card">'
            + "".join(render_row(g, "../", show_date=True) for g in glist_sorted)
            + "</div>",
            og_description=f"「{tag}」相關的 Steam 新遊戲，共 {len(glist_sorted)} 款",
            og_image=_hero_image(glist_sorted, site_url),
            canonical_url=f"{site_url}tags/{slug}.html",
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
  var box = document.getElementById("globalSearch");
  var form = box.closest("form");
  var rows = Array.prototype.slice.call(document.querySelectorAll("#searchResults .row"));
  var countEl = document.getElementById("searchCount");
  function apply() {
    var q = box.value.trim().toLowerCase();
    var showAdult = document.body.classList.contains("show-adult");
    var shown = 0;
    rows.forEach(function (r) {
      var nameEl = r.querySelector(".name");
      var name = nameEl ? nameEl.textContent.toLowerCase() : "";
      var nameMatch = !q || name.indexOf(q) !== -1;
      var adultOk = r.getAttribute("data-adult") !== "1" || showAdult;
      var match = nameMatch && adultOk;
      r.style.display = match ? "" : "none";
      if (match) shown++;
    });
    countEl.textContent = shown === rows.length ? "共 " + rows.length + " 款" : "符合 " + shown + " / " + rows.length + " 款";
  }
  // Arriving here via the search box on another page submits ?q=... as a normal GET -
  // pick that up and run the live filter instead of relying on another round trip.
  var params = new URLSearchParams(location.search);
  if (params.get("q")) box.value = params.get("q");
  form.addEventListener("submit", function (e) { e.preventDefault(); apply(); });
  box.addEventListener("input", apply);
  document.addEventListener("adulttoggle", apply);
  apply();
  box.focus();
  box.setSelectionRange(box.value.length, box.value.length);
})();
</script>"""
    search_body = (
        "<h1>搜尋遊戲</h1>"
        f'<div class="meta" id="searchCount">共 {len(all_games_sorted)} 款</div>'
        f'<div class="card" id="searchResults">{search_rows}</div>'
        f"{search_script}"
    )
    search_page = page_html(
        title=f"搜尋 - {SITE_NAME}",
        base="",
        nav_html="",
        meta=meta,
        body_html=search_body,
        og_description="搜尋所有已收錄的新上架 Steam 遊戲",
        og_image=f"{site_url}assets/logo.png",
        canonical_url=f"{site_url}search.html",
    )
    (docs_dir / "search.html").write_text(search_page, encoding="utf-8")

    # Entirely client-rendered: this page ships no game data of its own, it just reads
    # localStorage (written by the ☆ buttons on every other page) and builds rows from it.
    wishlist_script = """<script>
(function () {
  var container = document.getElementById("wishlistList");
  if (!container) return;
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function render() {
    var w;
    try { w = JSON.parse(localStorage.getItem("wishlist") || "{}"); } catch (e) { w = {}; }
    var entries = Object.keys(w).map(function (id) {
      var g = Object.assign({}, w[id]);
      g.appid = id;
      return g;
    });
    entries.sort(function (a, b) { return (b.addedAt || 0) - (a.addedAt || 0); });
    if (!entries.length) {
      container.className = "empty";
      container.textContent = "還沒有加入任何遊戲，按遊戲右邊的「☆ 願望」就可以加入";
      return;
    }
    container.className = "card";
    container.innerHTML = entries.map(function (g) {
      var badgeClass = g.status === "live" ? "badge live" : "badge upcoming";
      var badgeText = g.status === "live" ? esc(g.badge || "已上架") : ("⏳ " + esc(g.badge || ""));
      var steamUrl = "steam://store/" + encodeURIComponent(g.appid);
      var openAttrs = 'data-web="' + esc(g.web) + '" data-steam="' + esc(steamUrl) + '"';
      return (
        '<div class="row">' +
        '<a class="media" href="' + esc(g.web) + '" ' + openAttrs + '>' +
        '<img class="cap" src="' + esc(g.image) + '" loading="lazy" alt=""></a>' +
        '<div class="info"><a class="name" href="' + esc(g.web) + '" ' + openAttrs + '>' + esc(g.name) + '</a></div>' +
        '<div class="buy"><div class="price-line"><span class="disc-final plain">' + esc(g.price || "價格未知") + '</span></div>' +
        '<span class="' + badgeClass + '">' + badgeText + '</span></div>' +
        '<button type="button" class="wish-star filled" data-appid="' + esc(g.appid) +
        '" data-name="' + esc(g.name) + '" data-web="' + esc(g.web) + '" data-image="' + esc(g.image) +
        '" data-status="' + esc(g.status) + '" data-badge="' + esc(g.badge) + '" data-price="' + esc(g.price) +
        '" aria-label="移除願望清單">__WISH_BTN_HTML__</button>' +
        '</div>'
      );
    }).join("");
  }
  render();
  document.addEventListener("wishlistchange", render);
})();
</script>"""
    wishlist_script = wishlist_script.replace("__WISH_BTN_HTML__", WISH_BTN_HTML)
    wishlist_body = '<h1>願望清單</h1><div class="card" id="wishlistList"></div>' + wishlist_script
    wishlist_page = page_html(
        title=f"願望清單 - {SITE_NAME}",
        base="",
        nav_html="",
        meta=meta,
        body_html=wishlist_body,
        og_description="我收藏的 Steam 新遊戲願望清單",
        og_image=f"{site_url}assets/logo.png",
        canonical_url=f"{site_url}wishlist.html",
        tab="wish",
    )
    (docs_dir / "wishlist.html").write_text(wishlist_page, encoding="utf-8")

    if low_games:
        low_body = (
            '<div class="filter-tags" id="lowTags"></div>'
            '<div class="low-head" id="lowTop"><h1>歷史新低</h1><span class="low-count" id="lowCount"></span></div>'
            '<div class="card" id="lowResults">'
            + "".join(render_row(g, "", show_date=True, tag_links=False) for g in low_games)
            + '</div><div class="empty" id="lowEmpty" style="display:none">目前沒有遊戲處於歷史新低價</div>'
            '<div class="pager" id="lowPager"></div>'
            f"{HISTORICAL_LOW_SCRIPT}"
        )
    else:
        low_body = '<h1>歷史新低</h1><div class="empty">目前沒有遊戲處於歷史新低價</div>'
    low_page = page_html(
        title=f"歷史新低 - {SITE_NAME}",
        base="",
        nav_html="",
        meta=meta,
        body_html=low_body,
        og_description=f"目前有 {len(low_games)} 款遊戲在 Steam 創下歷史新低價",
        og_image=f"{site_url}assets/logo.png",
        canonical_url=f"{site_url}historical-low.html",
        tab="low",
    )
    (docs_dir / "historical-low.html").write_text(low_page, encoding="utf-8")


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
    parser.add_argument("--history", type=Path, default=BASE_DIR / "history.json")
    parser.add_argument("--docs", type=Path, default=BASE_DIR / "docs")
    parser.add_argument("--dry-run", action="store_true", help="Print results instead of posting to Discord")
    parser.add_argument("--no-backfill", action="store_true", help="Skip automatic first-run backfill")
    parser.add_argument("--backfill", type=int, nargs="?", const=-1, help="Force a (re)backfill of N days")
    parser.add_argument(
        "--skip-notify",
        action="store_true",
        help="Update data/site as normal but don't post to Discord (for frequent silent runs "
        "that just keep the site fresh)",
    )
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    config = load_config(args.config)
    api_key = config["steam_api_key"]
    language = config.get("language", "english")
    country = config.get("country", "us")
    retention_days = int(config.get("retention_days", DEFAULT_RETENTION_DAYS))
    backfill_days = int(config.get("backfill_days", DEFAULT_BACKFILL_DAYS))
    today = local_today()
    site_url = config.get("site_url") or f"file:///{(args.docs / 'index.html').resolve().as_posix()}"
    site_base = site_url if site_url.endswith("/") else site_url.rsplit("/", 1)[0] + "/"

    history = load_history(args.history)
    should_backfill = args.backfill is not None or (not history["games"] and not args.no_backfill)

    tag_names = fetch_tag_names(api_key, language)

    if should_backfill:
        days = args.backfill if args.backfill and args.backfill > 0 else backfill_days
        start = today - timedelta(days=days - 1)
        log.info("Backfilling %s to %s...", start.isoformat(), today.isoformat())
        backfill_games = find_releases_in_range(api_key, start, today, language, country, tag_names)
        upsert_history(history, backfill_games)
        log.info("Backfill added/updated %d release(s)", len(backfill_games))

    games = find_releases_in_range(api_key, today, today, language, country, tag_names)
    upsert_history(history, games)

    now_epoch = int(datetime.now(tz=timezone.utc).timestamp())
    refreshed = refresh_stale_discounts(history, api_key, language, country, tag_names, now_epoch)
    if refreshed:
        log.info("Refreshed %d game(s) whose discount had expired", refreshed)

    refreshed_upcoming = refresh_stale_upcoming(history, api_key, language, country, tag_names, now_epoch)
    if refreshed_upcoming:
        log.info("Refreshed %d game(s) whose upcoming countdown had already passed", refreshed_upcoming)

    refreshed_reviews = refresh_review_scores(history, api_key, language, country)
    if refreshed_reviews:
        log.info("Refreshed review scores for %d game(s)", refreshed_reviews)

    itad_key = config.get("itad_api_key", "")
    if itad_key:
        lows = fetch_historical_lows(itad_key, api_key, language, country, tag_names, today)
        if lows is not None:
            history["historical_low"] = lows
            log.info("Found %d game(s) at a new historical low", len(lows))

    if not args.dry_run or should_backfill:
        save_history(args.history, history, retention_days)
        generate_site(history, args.docs, retention_days, today, site_base)
        log.info("Site updated: %s", args.docs / "index.html")
        if not args.dry_run and config.get("git_auto_push", True):
            git_publish(BASE_DIR, args.docs, f"Update site {today.isoformat()}")

    # Count from history (accumulated over every hourly run today), not from this run's own
    # fresh fetch - a single fetch can miss a few games to the documented API pagination
    # flakiness, but history.json already carries forward whatever earlier runs today found.
    today_str = today.isoformat()
    today_count = sum(1 for g in history["games"].values() if g["release_date"] == today_str)
    log.info("%d release(s) on record for today", today_count)

    if args.skip_notify:
        log.info("--skip-notify: leaving Discord alone this run")
        return

    send_discord(config["webhook_url"], today, today_count, args.dry_run, site_url)


if __name__ == "__main__":
    main()
