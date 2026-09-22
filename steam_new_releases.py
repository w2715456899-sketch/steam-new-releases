"""Fetch today's newly released Steam games, post them to Discord, and build a browsable history site."""
from __future__ import annotations

import argparse
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
from urllib.parse import quote

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
TAG_LIMIT = 6

log = logging.getLogger("steam_new_releases")


@dataclass
class Game:
    appid: str
    name: str
    url: str
    release_date: date
    price_text: str
    image: str
    status: str  # "live" (already on the store) or "upcoming" (scheduled, may still slip)
    release_epoch: int | None = field(default=None)  # exact unlock time, only meaningful for "upcoming"
    tags: list[str] = field(default_factory=list)


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
            "price_text": g.price_text,
            "image": g.image,
            "status": g.status,
            "release_epoch": g.release_epoch if g.release_epoch is not None else existing.get("release_epoch"),
            "tags": g.tags if g.tags else existing.get("tags", []),
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
    cookies = {"Steam_Language": language}
    resp = requests.get(SEARCH_URL, params=params, headers=HEADERS, cookies=cookies, timeout=20)
    resp.raise_for_status()
    return resp.json()


def fetch_game_details(appid: str, language: str) -> tuple[int | None, list[str]]:
    """Best-effort scrape of the exact unlock timestamp and popular tags from the app page.

    The search listing only gives a day-level date and no tags. Individual app pages
    embed an absolute unix-epoch release time inside a JSON blob used by an unrelated
    widget (not a documented API), so the epoch part is fragile and silently returns
    None if Steam changes that markup - callers must treat it as optional.
    """
    cookies = {**AGE_GATE_COOKIES, "Steam_Language": language}
    try:
        resp = requests.get(
            APP_PAGE_URL.format(appid=appid), params={"l": language}, headers=HEADERS, cookies=cookies, timeout=15
        )
        resp.raise_for_status()
    except requests.RequestException:
        return None, []

    html_text = resp.text
    epoch = None
    pattern = (
        r"release_date&quot;:&quot;(\d+)&quot;,&quot;appname&quot;:&quot;.*?&quot;,"
        rf"&quot;steamworks_appid&quot;:{re.escape(appid)}\b"
    )
    match = re.search(pattern, html_text)
    if match:
        epoch = int(match.group(1))

    tags: list[str] = []
    soup = BeautifulSoup(html_text, "html.parser")
    tag_block = soup.select_one(".glance_tags.popular_tags")
    if tag_block:
        tags = [a.get_text(strip=True) for a in tag_block.select("a.app_tag")][:TAG_LIMIT]
    return epoch, tags


def parse_release_date(text: str) -> date | None:
    text = text.strip()
    if not text:
        return None
    try:
        return dateparser.parse(text, dayfirst=True, fuzzy=True).date()
    except (ValueError, OverflowError):
        return None


def parse_rows(html_text: str, status: str) -> Iterable[Game]:
    soup = BeautifulSoup(html_text, "html.parser")
    for row in soup.select("a.search_result_row"):
        appid = row.get("data-ds-appid")
        if not appid:
            continue  # bundles / packages have no single appid
        name_el = row.select_one(".title")
        release_el = row.select_one(".search_released")
        price_el = row.select_one(".search_price")
        img_el = row.select_one("img")
        release_date = parse_release_date(release_el.get_text() if release_el else "")
        if release_date is None:
            continue
        yield Game(
            appid=appid.split(",")[0],
            name=name_el.get_text(strip=True) if name_el else "Unknown",
            url=row.get("href", "").split("?")[0],
            release_date=release_date,
            price_text=re.sub(r"\s+", " ", price_el.get_text(" ", strip=True)) if price_el else "",
            image=img_el.get("src", "") if img_el else "",
            status=status,
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
.card { background: #171d26; border: 1px solid #232b37; border-radius: 10px; overflow: hidden; }
.row {
  display: flex; gap: 12px; padding: 10px 14px; align-items: center;
  border-bottom: 1px solid #1c2330;
}
.row:last-child { border-bottom: none; }
.row:hover { background: #1c2330; }
.row .media { flex: none; display: block; }
.row img.cap { width: 92px; height: 43px; object-fit: cover; border-radius: 4px; background: #232b37; }
.row .info { min-width: 0; flex: 1; }
.row .name {
  display: block; font-size: 0.95rem; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; text-decoration: none; color: inherit;
}
.row .name:hover { text-decoration: underline; }
.row .price { color: #8894a3; font-size: 0.8rem; margin-top: 2px; }
.tags { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 5px; }
.tag {
  font-size: 0.68rem; padding: 1px 7px; border-radius: 999px; background: #20293a;
  color: #9db4d1; text-decoration: none;
}
.tag:hover { background: #2a3550; color: #c7d6ec; }
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
"""

PAGE_SHELL = """<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<link rel="stylesheet" href="__ASSET_BASE__assets/style.css">
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
</body>
</html>
"""


def render_page(title: str, base: str, nav_html: str, meta: str, body_html: str) -> str:
    return (
        PAGE_SHELL.replace("__TITLE__", esc(title))
        .replace("__ASSET_BASE__", base)
        .replace("__HOME_HREF__", f"{base}index.html")
        .replace("__NAV__", nav_html)
        .replace("__META__", meta)
        .replace("__BODY__", body_html)
    )


def tag_slug(tag: str) -> str:
    return quote(tag, safe="")


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

    price = esc(g.get("price_text") or "價格未知")
    sub = f'{esc(g["release_date"])} · {price}' if show_date else price
    tag_links = "".join(
        f'<a class="tag" href="{base}tags/{tag_slug(t)}.html">{esc(t)}</a>' for t in g.get("tags", [])
    )
    tags_html = f'<div class="tags">{tag_links}</div>' if tag_links else ""

    return (
        '<div class="row">'
        f'<a class="media" href="{esc(g["url"])}" target="_blank" rel="noopener">'
        f'<img class="cap" src="{esc(g.get("image") or "")}" loading="lazy" alt=""></a>'
        '<div class="info">'
        f'<a class="name" href="{esc(g["url"])}" target="_blank" rel="noopener">{esc(g["name"])}</a>'
        f'<div class="price">{sub}</div>'
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
        log.info("Backfilling the last %d day(s) of already-released games (no tags/exact time)...", days)
        backfill_games = find_backfill_releases(days, language, country, backfill_max_pages)
        upsert_history(history, backfill_games)
        log.info("Backfill added/updated %d release(s)", len(backfill_games))

    games = find_todays_releases(target=today, language=language, country=country, max_pages=max_pages)
    for g in games:
        epoch, tags = fetch_game_details(g.appid, language)
        if g.status == "upcoming":
            g.release_epoch = epoch
        g.tags = tags
        time.sleep(0.3)

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
