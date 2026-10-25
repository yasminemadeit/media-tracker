#!/usr/bin/env python3
"""
Media Tracker scraper.

Pulls your Goodreads to-read shelf and your Letterboxd watchlist + liked lists,
enriches them with details (description, genres, runtime/pages, rating), and
writes data.js, which index.html loads. Details are cached in data.json, so
each run only fetches what is new.

Usage:
    pip install requests beautifulsoup4 curl_cffi
    python3 scraper.py
"""

import html as htmllib
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup

# ─── Configuration ───────────────────────────────────────────────────────────

CONFIG = {
    "goodreads_user_id": "131953210",
    "letterboxd_username": "iamonlyme",

    "max_list_pages": 0,          # 0 = every page of every liked list
    "film_enrich_per_run": 600,   # film pages to fetch for details per run
    "book_enrich_per_run": 300,   # book pages to fetch for genres/pages per run
    "request_delay": 0.4,         # seconds between requests
    "description_chars": 700,     # trim descriptions to keep the page light
}

CACHE_FILE = "data.json"
OUT_FILE = "data.js"
ERRORS = []

# ─── HTTP ────────────────────────────────────────────────────────────────────
# curl_cffi mimics a real Chrome browser, which gets past the bot protection
# Letterboxd uses. Falls back to plain requests if it isn't installed.

try:
    from curl_cffi import requests as http
    SESSION = http.Session(impersonate="chrome")
    CLIENT = "curl_cffi"
except ImportError:
    import requests as http
    SESSION = http.Session()
    SESSION.headers["User-Agent"] = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    )
    CLIENT = "requests"

_last = 0.0


def fetch(url, retries=3):
    global _last
    for attempt in range(retries):
        wait = CONFIG["request_delay"] - (time.time() - _last)
        if wait > 0:
            time.sleep(wait)
        _last = time.time()
        try:
            r = SESSION.get(url, timeout=30)
        except Exception as e:
            if attempt == retries - 1:
                print(f"    ! {url}: {e}")
                return None
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", 15)))
            continue
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            if attempt == retries - 1:
                print(f"    ! {url}: HTTP {r.status_code}")
                return r  # caller can inspect status
            time.sleep(2 ** attempt)
            continue
        return r
    return None


def ok(r):
    return r is not None and r.status_code < 400


def clean_text(s, limit=None):
    if not s:
        return ""
    s = re.sub(r"<br\s*/?>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = htmllib.unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n\n", s).strip()
    limit = limit or CONFIG["description_chars"]
    if len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0] + "…"
    return s


# ─── Cache ───────────────────────────────────────────────────────────────────

def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, encoding="utf-8") as f:
                c = json.load(f)
        except (OSError, json.JSONDecodeError):
            c = {}
    else:
        c = {}
    c.setdefault("films", {})        # slug -> details
    c.setdefault("book_details", {}) # goodreads id -> details
    c.setdefault("film_lists", {})   # slug -> {title, lists}
    c.setdefault("books", [])
    return c


def save_cache(c):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(c, f, ensure_ascii=False, separators=(",", ":"))


# ─── Goodreads ───────────────────────────────────────────────────────────────

STATUS_SHELVES = {"to-read", "currently-reading", "read", "want-to-read"}


def xt(el, path):
    found = el.find(path)
    return (found.text or "").strip() if found is not None and found.text else ""


def to_iso(rfc_date):
    try:
        return parsedate_to_datetime(rfc_date).date().isoformat()
    except Exception:
        return ""


def scrape_goodreads(user_id):
    print("[1/5] Goodreads to-read shelf")
    books, page = [], 1
    while True:
        url = (f"https://www.goodreads.com/review/list_rss/{user_id}"
               f"?shelf=to-read&page={page}")
        r = fetch(url)
        if not ok(r):
            if page == 1:
                ERRORS.append(f"Goodreads RSS failed (HTTP {getattr(r, 'status_code', '?')})")
            break
        try:
            items = ET.fromstring(r.content).findall(".//item")
        except ET.ParseError:
            break
        if not items:
            break
        for it in items:
            title = xt(it, "title")
            if not title:
                continue
            pages = xt(it, "book/num_pages") or xt(it, "num_pages")
            rating = xt(it, "average_rating")
            published = xt(it, "book_published")
            shelves = [s.strip() for s in xt(it, "user_shelves").split(",")]
            books.append({
                "id": xt(it, "book_id"),
                "title": title,
                "author": xt(it, "author_name"),
                "pages": int(pages) if pages.isdigit() else 0,
                "rating": round(float(rating), 2) if rating else 0,
                "year": int(published) if published.isdigit() else 0,
                "added": to_iso(xt(it, "user_date_added")),
                "shelves": [s.replace("-", " ").title() for s in shelves
                            if s and s not in STATUS_SHELVES],
                "desc": clean_text(xt(it, "book_description")),
                "img": xt(it, "book_medium_image_url") or xt(it, "book_image_url"),
            })
        page += 1
    print(f"  {len(books)} books")
    return books


def enrich_book(book_id):
    """Genres and page count from the Goodreads book page."""
    r = fetch(f"https://www.goodreads.com/book/show/{book_id}")
    if not ok(r):
        return None
    t = r.text
    genres = []
    for g in re.findall(r'"__typename":"Genre","name":"([^"]+)"', t):
        if g not in genres:
            genres.append(g)
    if not genres:  # older page layout
        soup = BeautifulSoup(t, "html.parser")
        genres = [a.get_text(strip=True) for a in
                  soup.select('a[href*="/genres/"]')][:6]
    pm = re.search(r'"numPages":(\d+)', t) or re.search(r"(\d+)\s+pages", t)
    return {
        "genres": genres[:6],
        "pages": int(pm.group(1)) if pm else 0,
        "ok": True,
    }


# ─── Letterboxd ──────────────────────────────────────────────────────────────

def lb_page(url):
    r = fetch(url)
    if r is not None and r.status_code in (403, 503):
        ERRORS.append(f"Letterboxd blocked the request (HTTP {r.status_code}) using {CLIENT}")
        return None
    return BeautifulSoup(r.text, "html.parser") if ok(r) else None


def parse_posters(soup):
    """Film slugs + names from a list/watchlist page (old and new markup)."""
    out = []
    for el in soup.select("[data-item-slug], [data-film-slug], [data-target-link]"):
        slug = el.get("data-item-slug") or el.get("data-film-slug")
        if not slug:
            m = re.search(r"/film/([^/]+)/", el.get("data-target-link", "") + "/")
            slug = m.group(1) if m else None
        if not slug:
            continue
        name = el.get("data-item-name") or el.get("data-film-name") or ""
        if not name:
            img = el.find("img")
            name = img.get("alt", "") if img else ""
        out.append((slug, name))
    if not out:
        for a in soup.find_all("a", href=re.compile(r"^/film/[^/]+/?$")):
            slug = a["href"].strip("/").split("/")[-1]
            img = a.find("img")
            out.append((slug, img.get("alt", "") if img else a.get_text(strip=True)))
    seen, uniq = set(), []
    for s, n in out:
        if s not in seen:
            seen.add(s)
            uniq.append((s, n))
    return uniq


def scrape_film_list(base_url):
    films, page = [], 1
    while True:
        url = base_url if page == 1 else f"{base_url.rstrip('/')}/page/{page}/"
        soup = lb_page(url)
        if soup is None:
            break
        found = parse_posters(soup)
        if not found:
            break
        films.extend(found)
        if not soup.select_one("a.next"):
            break
        page += 1
        if CONFIG["max_list_pages"] and page > CONFIG["max_list_pages"]:
            break
    return films


def scrape_liked_lists(user):
    print("[3/5] Letterboxd liked lists")
    lists, page = [], 1
    while True:
        url = f"https://letterboxd.com/{user}/likes/lists/"
        if page > 1:
            url += f"page/{page}/"
        soup = lb_page(url)
        if soup is None:
            break
        before = len(lists)
        for a in soup.find_all("a", href=re.compile(r"^/[^/]+/list/[^/]+/?$")):
            name = a.get_text(" ", strip=True)
            link = urljoin("https://letterboxd.com", a["href"])
            if name and link not in [l[1] for l in lists]:
                lists.append((name, link))
        if len(lists) == before or not soup.select_one("a.next"):
            break
        page += 1
    print(f"  {len(lists)} lists")
    return lists


def ld_json(soup):
    for s in soup.find_all("script", type="application/ld+json"):
        raw = (s.string or "").strip()
        raw = re.sub(r"^/\*\s*<!\[CDATA\[\s*\*/", "", raw)
        raw = re.sub(r"/\*\s*\]\]>\s*\*/$", "", raw).strip()
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, list):
            data = data[0] if data else {}
        if isinstance(data, dict):
            return data
    return {}


def enrich_film(slug):
    soup = lb_page(f"https://letterboxd.com/film/{slug}/")
    if soup is None:
        return None
    d = {"ok": True}
    og = soup.find("meta", property="og:title")
    if og:
        m = re.match(r"(.+?)\s*\((\d{4})\)\s*$", og.get("content", ""))
        if m:
            d["title"], d["year"] = m.group(1), int(m.group(2))
        else:
            d["title"] = og.get("content", "")
    desc = soup.find("meta", property="og:description") or soup.find("meta", attrs={"name": "description"})
    if desc:
        d["desc"] = clean_text(desc.get("content", ""))

    ld = ld_json(soup)
    g = ld.get("genre")
    if g:
        d["genres"] = g if isinstance(g, list) else [g]
    dirs = ld.get("director")
    if dirs:
        dirs = dirs if isinstance(dirs, list) else [dirs]
        d["director"] = ", ".join(x.get("name", "") for x in dirs if isinstance(x, dict))
    ar = ld.get("aggregateRating") or {}
    try:
        d["rating"] = round(float(ar.get("ratingValue")), 2)
    except (TypeError, ValueError):
        pass
    if "year" not in d:
        rel = ld.get("releasedEvent") or []
        if isinstance(rel, list) and rel and isinstance(rel[0], dict):
            ym = re.match(r"(\d{4})", str(rel[0].get("startDate", "")))
            if ym:
                d["year"] = int(ym.group(1))
    countries = ld.get("countryOfOrigin") or []
    if isinstance(countries, list):
        d["countries"] = [c.get("name") for c in countries if isinstance(c, dict) and c.get("name")]

    foot = soup.select_one("p.text-footer")
    m = re.search(r"(\d+)\s*mins?", foot.get_text(" ") if foot else soup.get_text(" "))
    if m:
        d["runtime"] = int(m.group(1))
    if "genres" not in d:
        d["genres"] = [a.get_text(strip=True) for a in
                       soup.select('a[href*="/films/genre/"]')][:5]
    return d


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    print(f"HTTP client: {CLIENT}")
    cache = load_cache()

    # Books
    books = scrape_goodreads(CONFIG["goodreads_user_id"]) or cache["books"]
    cache["books"] = books

    # Films and which lists they're on
    user = CONFIG["letterboxd_username"]
    print("[2/5] Letterboxd watchlist")
    watch = scrape_film_list(f"https://letterboxd.com/{user}/watchlist/")
    print(f"  {len(watch)} films")
    liked = scrape_liked_lists(user)

    print("[4/5] Films in each liked list")
    film_lists = {}
    for slug, name in watch:
        film_lists.setdefault(slug, {"title": name, "lists": []})["lists"].append("Watchlist")
    for i, (lname, lurl) in enumerate(liked, 1):
        films = scrape_film_list(lurl)
        print(f"  [{i}/{len(liked)}] {lname[:55]} — {len(films)}")
        for slug, name in films:
            entry = film_lists.setdefault(slug, {"title": name, "lists": []})
            if lname not in entry["lists"]:
                entry["lists"].append(lname)

    if film_lists:
        cache["film_lists"] = film_lists
    else:
        print("  ! No films found this run; keeping the previous list")
        film_lists = cache["film_lists"]

    # Details, a batch per run (watchlist first)
    print("[5/5] Fetching details")
    watch_slugs = [s for s, _ in watch]
    todo = [s for s in watch_slugs + list(film_lists) if s not in cache["films"]]
    todo = list(dict.fromkeys(todo))[: CONFIG["film_enrich_per_run"]]
    for i, slug in enumerate(todo, 1):
        d = enrich_film(slug)
        if d:
            cache["films"][slug] = d
        if i % 50 == 0:
            print(f"  films {i}/{len(todo)}")
            save_cache(cache)

    btodo = [b["id"] for b in books if b["id"] and b["id"] not in cache["book_details"]]
    btodo = btodo[: CONFIG["book_enrich_per_run"]]
    for i, bid in enumerate(btodo, 1):
        d = enrich_book(bid)
        if d:
            cache["book_details"][bid] = d
        if i % 50 == 0:
            print(f"  books {i}/{len(btodo)}")
            save_cache(cache)

    # Build output
    films_out = []
    for slug, info in film_lists.items():
        d = cache["films"].get(slug, {})
        name = info["title"] or ""
        ym = re.match(r"(.+?)\s*\((\d{4})\)$", name)
        films_out.append({
            "id": slug,
            "title": d.get("title") or (ym.group(1) if ym else name) or slug.replace("-", " ").title(),
            "year": d.get("year") or (int(ym.group(2)) if ym else 0),
            "director": d.get("director", ""),
            "runtime": d.get("runtime", 0),
            "rating": d.get("rating", 0),
            "genres": d.get("genres", []),
            "countries": d.get("countries", []),
            "desc": d.get("desc", ""),
            "lists": info["lists"],
        })

    books_out = []
    for b in books:
        d = cache["book_details"].get(b["id"], {})
        books_out.append({**b,
                          "genres": d.get("genres") or b["shelves"],
                          "pages": b["pages"] or d.get("pages", 0)})

    meta = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "films_pending": sum(1 for s in film_lists if s not in cache["films"]),
        "books_pending": sum(1 for b in books if b["id"] not in cache["book_details"]),
        "errors": sorted(set(ERRORS)),
    }
    cache["meta"] = meta
    save_cache(cache)

    payload = {"meta": meta, "films": films_out, "books": books_out}
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write("window.DATA=")
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        f.write(";\n")

    print(f"\nWrote {OUT_FILE}: {len(films_out)} films, {len(books_out)} books")
    print(f"  still to fetch details: {meta['films_pending']} films, {meta['books_pending']} books")
    for e in meta["errors"]:
        print(f"  ! {e}")


if __name__ == "__main__":
    main()
