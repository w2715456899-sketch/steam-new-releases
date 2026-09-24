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
HEADERS = {"User-Agent": "steam-new-releases-bot/2.0"}
STATE_RETENTION_DAYS = 14
DEFAULT_RETENTION_DAYS = 30
DEFAULT_BACKFILL_DAYS = 30
LOCAL_TZ = ZoneInfo("Asia/Taipei")  # Both the query window and the date each item gets filed
# under use this timezone, so the site is internally consistent with itself (and with the
# countdown time shown for upcoming games). Steam's own store page dates use Pacific time
# instead, so this site's date for a given game can differ by a day from what its Steam page
# says for anything released in the ~15-16h gap between the two timezones' day boundaries -
# a known, accepted trade-off in favor of everything on this site agreeing with itself.
MAX_QUERY_PAGES = 50  # safety cap (5000 items) so a pagination bug can't loop forever
WEEKDAY_ZH = ["一", "二", "三", "四", "五", "六", "日"]

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


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"notified": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict) -> None:
    cutoff = (local_today() - timedelta(days=STATE_RETENTION_DAYS)).isoformat()
    state["notified"] = {k: v for k, v in state["notified"].items() if v >= cutoff}
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


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
        history["games"][g.appid] = {
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


DISCORD_EMBED_BATCH = 10  # Discord hard-caps a single message at 10 embeds
DISCORD_COLOR_LIVE = 0x2ECC71
DISCORD_COLOR_UPCOMING = 0x5865F2


def send_discord(webhook_url: str, today: date, games: list[Game], dry_run: bool, site_url: str) -> None:
    if not games:
        log.info("No new releases today - skipping Discord ping")
        return

    content = f"{today.isoformat()} 新遊戲來了 🫠 共 {len(games)} 款\n{site_url}"
    if dry_run:
        log.info(content)
        for game in games:
            log.info(" - %s (%s) %s", game.name, game.release_date, game.url)
        return

    requests.post(webhook_url, json={"content": content}, timeout=20).raise_for_status()

    for i in range(0, len(games), DISCORD_EMBED_BATCH):
        batch = games[i : i + DISCORD_EMBED_BATCH]
        embeds = []
        for g in batch:
            status_label = "已上架" if g.status == "live" else "預計上架"
            price = g.price_final or "價格未知"
            embed = {
                "title": g.name[:256],
                "url": g.url,
                "description": f"{status_label} · {price}",
                "color": DISCORD_COLOR_LIVE if g.status == "live" else DISCORD_COLOR_UPCOMING,
            }
            image = g.header_image or g.image
            if image:
                embed["thumbnail"] = {"url": image}
            embeds.append(embed)
        requests.post(webhook_url, json={"embeds": embeds}, timeout=20).raise_for_status()
        time.sleep(0.3)


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
.nav { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.nav-arrow {
  width: 34px; height: 34px; border-radius: 50%; display: flex; align-items: center; justify-content: center;
  background: #171d26; border: 1px solid #232b37; color: #9db4d1; text-decoration: none; font-size: 1rem;
}
.nav-arrow:hover { background: #1c2330; color: #e7ecf2; border-color: #3a4152; }
.nav-arrow.disabled { color: #3f4756; }
.nav-mid {
  color: #b79aef; text-decoration: none; font-size: 0.85rem; font-weight: 600;
  padding: 8px 16px; border-radius: 999px; background: #241a33; border: 1px solid #3a2a4f;
}
.nav-mid:hover { background: #2d2140; }
.drawer-toggle {
  position: fixed; left: 0; top: 50%; transform: translateY(-50%);
  width: 26px; height: 56px; border-radius: 0 10px 10px 0;
  background: #171d26; border: 1px solid #232b37; border-left: none;
  color: #9db4d1; font-size: 1.1rem; cursor: pointer; z-index: 60; padding: 0;
  display: flex; align-items: center; justify-content: center;
}
.drawer-toggle:hover { background: #1c2330; color: #e7ecf2; }
.drawer-backdrop {
  position: fixed; inset: 0; background: rgba(8, 10, 14, 0.6); z-index: 65; display: none;
}
.drawer-backdrop.open { display: block; }
.drawer {
  position: fixed; left: 0; top: 0; bottom: 0; width: min(260px, 80vw);
  background: #171d26; border-right: 1px solid #232b37; z-index: 70;
  transform: translateX(-100%); transition: transform 0.2s ease;
  padding-top: env(safe-area-inset-top, 0px); padding-bottom: env(safe-area-inset-bottom, 0px);
  display: flex; flex-direction: column;
}
.drawer.open { transform: translateX(0); }
.drawer-head { display: flex; align-items: center; justify-content: space-between; padding: 18px 10px 8px 18px; }
.drawer-title { font-size: 0.78rem; color: #6b7686; text-transform: uppercase; letter-spacing: 0.06em; }
.drawer-close {
  width: 28px; height: 28px; border-radius: 8px; background: none; border: none;
  color: #6b7686; font-size: 1rem; cursor: pointer; line-height: 1;
}
.drawer-close:hover { background: #232b3d; color: #e7ecf2; }
.drawer-item {
  display: flex; align-items: center; gap: 8px; padding: 12px 18px; color: #e7ecf2;
  text-decoration: none; font-size: 0.95rem;
}
.drawer-item:hover { background: #1c2330; }
.toast {
  position: fixed; left: 50%; bottom: calc(24px + env(safe-area-inset-bottom, 0px));
  transform: translateX(-50%) translateY(12px); background: #1c2330; color: #e7ecf2;
  border: 1px solid #2a3346; padding: 10px 18px; border-radius: 999px; font-size: 0.85rem;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4); opacity: 0; transition: opacity 0.2s ease, transform 0.2s ease;
  z-index: 200; pointer-events: none; max-width: calc(100vw - 32px); text-align: center;
}
.toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }
.meta { color: #8894a3; font-size: 0.8rem; margin-bottom: 20px; }
h1 { font-size: 1.3rem; margin: 0; }
.date-heading {
  display: flex; align-items: flex-start; justify-content: space-between; gap: 12px;
  flex-wrap: wrap; margin: 4px 0 16px;
}
.date-hero { padding-left: 14px; border-left: 3px solid #7c5cbf; }
.date-hero-eyebrow {
  font-size: 0.72rem; color: #b79aef; text-transform: uppercase; letter-spacing: 0.1em; font-weight: 700;
}
.date-hero-date { font-size: 1.6rem; font-weight: 700; margin: 2px 0 0; line-height: 1.25; }
.date-hero-date .wd { font-size: 1rem; font-weight: 400; color: #8894a3; margin-left: 8px; }
.date-hero-count { font-size: 0.82rem; color: #8894a3; margin-top: 2px; }
.adult-toggle {
  display: flex; align-items: center; gap: 6px; font-size: 0.82rem; color: #9db4d1;
  cursor: pointer; user-select: none;
}
.adult-toggle-input { accent-color: #7c5cbf; cursor: pointer; }
.hidden-count { color: #6b7686; }
body.show-adult .hidden-count { display: none; }
.row[data-adult="1"] { display: none; }
body.show-adult .row[data-adult="1"] { display: flex; }
h2.section { font-size: 0.85rem; color: #8894a3; margin: 24px 0 8px; text-transform: uppercase; letter-spacing: 0.04em; }
.card { background: #171d26; border: 1px solid #232b37; border-radius: 10px; overflow: visible; }
.row {
  display: flex; gap: 14px; padding: 12px 14px; align-items: center;
  border-bottom: 1px solid #1c2330; position: relative;
}
.row:first-child { border-top-left-radius: 10px; border-top-right-radius: 10px; }
.row:last-child { border-bottom: none; border-bottom-left-radius: 10px; border-bottom-right-radius: 10px; }
.row:hover { background: #1c2330; z-index: 20; }
.wish-star {
  position: absolute; top: 8px; right: 8px; z-index: 5;
  width: 30px; height: 30px; border-radius: 50%; border: 1px solid #2a3346;
  background: rgba(16, 20, 26, 0.75); backdrop-filter: blur(2px);
  color: #9db4d1; font-size: 1.05rem; line-height: 1; cursor: pointer;
  display: flex; align-items: center; justify-content: center; padding: 0;
}
.wish-star:hover { border-color: #7c5cbf; color: #d3c1fb; }
.wish-star.filled { color: #f0c419; border-color: #a3841a; background: rgba(48, 40, 10, 0.55); }
.row .media { flex: none; display: block; position: relative; }
.row img.cap {
  width: 160px; height: 75px; object-fit: cover; border-radius: 6px; background: #232b37;
  transition: transform 0.18s ease, box-shadow 0.18s ease; transform-origin: right center;
}
@media (hover: hover) and (pointer: fine) {
  .row:hover img.cap {
    transform: scale(2.1);
    box-shadow: 0 12px 32px rgba(0, 0, 0, 0.6);
    position: relative; z-index: 30;
  }
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
.review-line { font-size: 0.78rem; margin-top: 4px; }
.review-score { font-weight: 600; }
.review-score.pos { color: #5fd58a; }
.review-score.mixed { color: #d4b95f; }
.review-score.neg { color: #e2685f; }
.review-count { color: #6b7686; margin-left: 5px; }
.open-modal-backdrop {
  display: none; position: fixed; inset: 0; background: rgba(8, 10, 14, 0.72);
  backdrop-filter: blur(3px); align-items: center; justify-content: center; z-index: 100; padding: 16px;
}
.open-modal {
  position: relative; background: linear-gradient(180deg, #1c2330, #171d26);
  border: 1px solid #2a3346; border-radius: 16px; padding: 30px 22px 22px;
  width: min(340px, 100%); display: flex; flex-direction: column; gap: 18px;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.55); animation: openModalIn 0.15s ease;
}
@keyframes openModalIn {
  from { opacity: 0; transform: translateY(8px) scale(0.97); }
  to { opacity: 1; transform: none; }
}
.open-modal-x {
  position: absolute; top: 10px; right: 10px; width: 28px; height: 28px; border-radius: 8px;
  background: none; border: none; color: #6b7686; font-size: 1rem; cursor: pointer; line-height: 1;
}
.open-modal-x:hover { background: #232b3d; color: #e7ecf2; }
.open-modal-title { font-size: 1.05rem; font-weight: 600; text-align: center; }
.open-modal-options { display: flex; gap: 12px; }
.open-modal-option {
  flex: 1; display: flex; flex-direction: column; align-items: center; gap: 8px;
  padding: 18px 10px; border-radius: 12px; border: 1px solid #2a3346; background: #10141a;
  color: #e7ecf2; cursor: pointer; transition: border-color 0.15s, transform 0.15s, background 0.15s;
}
.open-modal-option:hover { transform: translateY(-2px); }
.open-modal-option[data-choice="web"]:hover { border-color: #4c6b22; background: rgba(76, 107, 34, 0.12); }
.open-modal-option[data-choice="steam"]:hover { border-color: #66c0f4; background: rgba(102, 192, 244, 0.1); }
.open-modal-icon {
  width: 1.9rem; height: 1.9rem; font-size: 1.9rem; line-height: 1; color: #9db4d1;
  display: flex; align-items: center; justify-content: center;
}
.open-modal-icon svg { width: 100%; height: 100%; display: block; }
.open-modal-option[data-choice="web"]:hover .open-modal-icon { color: #a4d007; }
.open-modal-option[data-choice="steam"]:hover .open-modal-icon { color: #66c0f4; }
.open-modal-label { font-size: 0.85rem; color: #9db4d1; }
.open-modal-remember { display: flex; align-items: center; justify-content: space-between; font-size: 0.85rem; color: #8894a3; }
.switch { position: relative; width: 36px; height: 20px; display: inline-block; cursor: pointer; }
.switch input { position: absolute; opacity: 0; width: 100%; height: 100%; margin: 0; cursor: pointer; }
.switch-track { position: absolute; inset: 0; background: #2a3346; border-radius: 999px; transition: background 0.15s; }
.switch-track::after {
  content: ""; position: absolute; top: 2px; left: 2px; width: 16px; height: 16px;
  background: #e7ecf2; border-radius: 50%; transition: transform 0.15s;
}
.switch input:checked ~ .switch-track { background: #4c6b22; }
.switch input:checked ~ .switch-track::after { transform: translateX(16px); }
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
@media (max-width: 600px) {
  .row { flex-wrap: wrap; padding: 12px; }
  .row .media { flex: 1 1 100%; order: 1; }
  .row img.cap { width: 100%; height: auto; aspect-ratio: 16 / 7; }
  .row .badge { order: 2; margin: 8px 0 0 auto; }
  .row .info { flex: 1 1 100%; order: 3; margin-top: 8px; }
  .row .name { white-space: normal; overflow: visible; text-overflow: clip; font-size: 1.05rem; }
  .date-grid { grid-template-columns: repeat(auto-fill, minmax(120px, 1fr)); }
  .open-modal-options { flex-direction: column; }
}
.badge { flex: none; font-size: 0.92rem; font-weight: 600; padding: 6px 12px; border-radius: 999px; white-space: nowrap; }
.badge.live { background: #16331f; color: #5fd58a; }
.badge.upcoming { background: #33291a; color: #e0b25f; }
.empty { color: #8894a3; padding: 40px 0; text-align: center; }
.date-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 12px; }
.date-card {
  background: #171d26; border: 1px solid #232b37; border-radius: 12px; padding: 14px;
  display: flex; flex-direction: column; gap: 10px; text-decoration: none; color: inherit;
  transition: border-color 0.15s, transform 0.15s;
}
.date-card:hover { border-color: #4a5b7a; transform: translateY(-2px); }
.date-card.weekend { background: #1a1620; border-color: #2e2438; }
.date-card-top { display: flex; flex-direction: column; gap: 2px; }
.date-card-day { font-size: 1.6rem; font-weight: 700; line-height: 1; }
.date-card-md { font-size: 0.72rem; color: #8894a3; }
.date-card-count { font-size: 1rem; font-weight: 600; color: #5fd58a; }
.date-card-count span { font-size: 0.7rem; color: #8894a3; font-weight: 400; }
.date-month-divider {
  grid-column: 1 / -1; font-size: 0.85rem; color: #8894a3; font-weight: 600;
  margin-top: 10px; padding-top: 14px; border-top: 1px solid #232b37;
}
.date-month-divider:first-child { margin-top: 0; padding-top: 0; border-top: none; }
.search-box {
  width: 100%; padding: 10px 14px; border-radius: 8px; border: 1px solid #2a3346;
  background: #171d26; color: #e7ecf2; font-size: 0.95rem; margin-bottom: 8px;
}
.search-box:focus { outline: none; border-color: #4a5b7a; }
.global-search { margin-bottom: 20px; }
.global-search .search-box { margin-bottom: 0; }
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
<button type="button" class="drawer-toggle" id="drawerToggle" aria-label="選單">›</button>
<div class="drawer-backdrop" id="drawerBackdrop"></div>
<nav class="drawer" id="drawer">
  <div class="drawer-head">
    <span class="drawer-title">選單</span>
    <button type="button" class="drawer-close" id="drawerClose" aria-label="關閉">✕</button>
  </div>
  <a class="drawer-item" href="__ASSET_BASE__wishlist.html">★ 願望清單</a>
</nav>
<div class="wrap">
  <div class="topbar">
    <a class="brand" href="__HOME_HREF__"><img src="__ASSET_BASE__assets/logo.png" alt="Steam 新遊戲紀錄"></a>
    <div class="nav">__NAV__</div>
  </div>
  <div class="meta">__META__</div>
  <form class="global-search" action="__ASSET_BASE__search.html" method="get">
    <input type="text" name="q" id="globalSearch" class="search-box" placeholder="搜尋名稱…" autocomplete="off">
  </form>
  __BODY__
</div>

<div class="open-modal-backdrop" id="openModalBackdrop">
  <div class="open-modal">
    <button type="button" class="open-modal-x" id="openModalCancel" aria-label="關閉">✕</button>
    <div class="open-modal-title">要用什麼開啟？</div>
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
    <div class="open-modal-remember">
      <span>記住我的選擇</span>
      <label class="switch">
        <input type="checkbox" id="openModalRemember" checked>
        <span class="switch-track"></span>
      </label>
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
(function () {
  var toggle = document.getElementById("drawerToggle");
  var drawer = document.getElementById("drawer");
  var backdrop = document.getElementById("drawerBackdrop");
  function openDrawer() { drawer.classList.add("open"); backdrop.classList.add("open"); }
  function closeDrawer() { drawer.classList.remove("open"); backdrop.classList.remove("open"); }
  toggle.addEventListener("click", function () {
    if (drawer.classList.contains("open")) { closeDrawer(); } else { openDrawer(); }
  });
  backdrop.addEventListener("click", closeDrawer);
  document.getElementById("drawerClose").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") closeDrawer(); });
})();
(function () {
  // Wishlist lives entirely in localStorage (no account, no backend) - each star button
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
      btn.textContent = on ? "★" : "☆";
      btn.setAttribute("aria-label", on ? "移除願望清單" : "加入願望清單");
    });
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
    }
    syncStars();
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
) -> str:
    og_html = ""
    if canonical_url:
        og_html = (
            '<meta property="og:type" content="website">\n'
            '<meta property="og:site_name" content="Steam 新遊戲紀錄">\n'
            f'<meta property="og:title" content="{esc(title)}">\n'
            f'<meta property="og:description" content="{esc(og_description)}">\n'
            f'<meta property="og:url" content="{esc(canonical_url)}">\n'
            f'<meta property="og:image" content="{esc(og_image)}">\n'
            '<meta name="twitter:card" content="summary_large_image">'
        )
    return (
        PAGE_SHELL.replace("__TITLE__", esc(title))
        .replace("__ASSET_BASE__", base)
        .replace("__CSS_VER__", STYLE_HASH)
        .replace("__HOME_HREF__", f"{base}index.html")
        .replace("__NAV__", nav_html)
        .replace("__META__", meta)
        .replace("__BODY__", body_html)
        .replace("__OG__", og_html)
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
        dt = datetime.fromtimestamp(end, tz=LOCAL_TZ)
        html += f'<div class="discount-end">優惠至 {dt.strftime("%m/%d")} 截止</div>'
    return html


def render_review(g: dict) -> str:
    score, count = g.get("review_score"), g.get("review_count")
    if not score or not count:
        return ""  # too few reviews yet for Steam to have scored it - nothing worth showing
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


def render_row(g: dict, base: str, show_date: bool = False) -> str:
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
    tags_html = render_tags(g["appid"], g.get("tags", []), base)
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
    star_html = f'<button type="button" class="wish-star" {star_attrs} aria-label="加入願望清單">☆</button>'

    return (
        f'<div class="row"{adult_attr}>'
        f'<a class="media" href="{web_url}" {open_attrs}>'
        f'<img class="cap" src="{esc(zoom_image)}" data-fallback="{esc(fallback_image)}" '
        f'onerror="imgFallback(this)" loading="lazy" alt=""></a>'
        f"{star_html}"
        '<div class="info">'
        f'<a class="name" href="{web_url}" {open_attrs}>{esc(g["name"])}</a>'
        f"{date_html}{render_price(g)}{review_html}"
        f"{tags_html}</div>"
        f"{badge}</div>"
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
        '<div class="date-hero-eyebrow">NEW RELEASES</div>'
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


def generate_site(history: dict, docs_dir: Path, retention_days: int, today: date, site_url: str) -> None:
    by_date: dict[str, list[dict]] = {}
    for g in history["games"].values():
        by_date.setdefault(g["release_date"], []).append(g)
    today_str = today.isoformat()
    by_date.setdefault(today_str, [])  # today always gets a page, even before any data exists for it
    dates_desc = sorted(by_date, reverse=True)
    counts = {d: len(by_date[d]) for d in dates_desc}
    generated_at = datetime.now(tz=LOCAL_TZ).strftime("%Y-%m-%d %H:%M")
    meta = f"Last synced {esc(generated_at)} · Showing the last {retention_days} days"

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
    dates_index_page = render_page(
        title="所有日期 - Steam 新遊戲紀錄",
        base="../",
        nav_html='<a class="nav-mid" href="../index.html">← 回首頁</a>',
        meta=meta,
        body_html=f'<h1>所有日期</h1><div class="date-grid">{dates_index_rows}</div>',
        og_description=f"瀏覽最近 {retention_days} 天內每日上架的新遊戲",
        og_image=f"{site_url}assets/logo.png",
        canonical_url=f"{site_url}dates/index.html",
    )
    (dates_dir / "index.html").write_text(dates_index_page, encoding="utf-8")

    if dates_desc:
        # Home is always "today", not just whatever date happens to have the most recent
        # data - a stray game or two already filed under tomorrow (normal near midnight)
        # shouldn't make the homepage jump ahead of the actual current day.
        home_page = render_page(
            title="Steam 新遊戲紀錄",
            base="",
            nav_html=build_nav(dates_desc, today_str, ""),
            meta=meta,
            body_html=render_date_body(today_str, by_date[today_str], ""),
            og_description=f"{today_str} 新上架 Steam 遊戲，共 {counts[today_str]} 款",
            og_image=_hero_image(by_date[today_str], site_url),
            canonical_url=site_url,
        )
    else:
        home_page = render_page(
            title="Steam 新遊戲紀錄",
            base="",
            nav_html="",
            meta=meta,
            body_html='<h1>Steam 新遊戲紀錄</h1><div class="empty">尚無資料</div>',
            og_description="每日追蹤 Steam 新上架遊戲",
            og_image=f"{site_url}assets/logo.png",
            canonical_url=site_url,
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
            nav_html='<a class="nav-mid" href="../index.html">← 回首頁</a>',
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
    search_page = render_page(
        title="搜尋 - Steam 新遊戲紀錄",
        base="",
        nav_html='<a class="nav-mid" href="index.html">← 回首頁</a>',
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
      container.textContent = "還沒有加入任何願望清單，點遊戲卡片右上角的 ☆ 就可以加入";
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
        '<button type="button" class="wish-star filled" data-appid="' + esc(g.appid) +
        '" data-name="' + esc(g.name) + '" data-web="' + esc(g.web) + '" data-image="' + esc(g.image) +
        '" data-status="' + esc(g.status) + '" data-badge="' + esc(g.badge) + '" data-price="' + esc(g.price) +
        '" aria-label="移除願望清單">★</button>' +
        '<div class="info"><a class="name" href="' + esc(g.web) + '" ' + openAttrs + '>' + esc(g.name) + '</a>' +
        '<div class="price-line"><span class="disc-final plain">' + esc(g.price || "價格未知") + '</span></div></div>' +
        '<span class="' + badgeClass + '">' + badgeText + '</span>' +
        '</div>'
      );
    }).join("");
  }
  render();
  document.addEventListener("wishlistchange", render);
})();
</script>"""
    wishlist_body = '<h1>願望清單</h1><div class="card" id="wishlistList"></div>' + wishlist_script
    wishlist_page = render_page(
        title="願望清單 - Steam 新遊戲紀錄",
        base="",
        nav_html='<a class="nav-mid" href="index.html">← 回首頁</a>',
        meta=meta,
        body_html=wishlist_body,
        og_description="我收藏的 Steam 新遊戲願望清單",
        og_image=f"{site_url}assets/logo.png",
        canonical_url=f"{site_url}wishlist.html",
    )
    (docs_dir / "wishlist.html").write_text(wishlist_page, encoding="utf-8")


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
    parser.add_argument(
        "--skip-notify",
        action="store_true",
        help="Update data/site as normal but don't post to Discord or mark anything as notified "
        "(for frequent silent runs that just keep the site fresh; a later run without this flag "
        "sends everything that piled up since the last real notification)",
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

    if not args.dry_run or should_backfill:
        save_history(args.history, history, retention_days)
        generate_site(history, args.docs, retention_days, today, site_base)
        log.info("Site updated: %s", args.docs / "index.html")
        if not args.dry_run and config.get("git_auto_push", True):
            git_publish(BASE_DIR, args.docs, f"Update site {today.isoformat()}")

    state = load_state(args.state)
    notified = state["notified"]
    new_games = [g for g in games if g.appid not in notified]
    log.info("Found %d release(s) today, %d not yet notified", len(games), len(new_games))

    if args.skip_notify:
        log.info("--skip-notify: leaving Discord and state.json alone this run")
        return

    send_discord(config["webhook_url"], today, new_games, args.dry_run, site_url)

    if not args.dry_run:
        for game in new_games:
            notified[game.appid] = game.release_date.isoformat()
        save_state(args.state, state)


if __name__ == "__main__":
    main()
