#!/usr/bin/env python3
"""
Media Tracker — Scrapes Goodreads & Letterboxd, generates a static HTML finder.

Pulls your Goodreads to-read shelf and Letterboxd watchlist + liked lists,
then generates a single HTML page with search links to streaming sites and
Z-Library. Designed to run locally or via GitHub Actions on a daily cron.

Usage:
    python3 scraper.py              # scrape everything, generate index.html
    python3 scraper.py --no-enrich  # skip film metadata enrichment (fast mode)

Requirements: pip install requests beautifulsoup4
"""

import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import quote, urljoin

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Missing dependencies. Run:\n  pip install requests beautifulsoup4")
    sys.exit(1)

# ─── Configuration ───────────────────────────────────────────────────────────

CONFIG = {
    # Your profile IDs
    "goodreads_user_id": "131953210",
    "letterboxd_username": "iamonlyme",

    # Z-Library domain (change when it rotates)
    "zlib_domain": "1lib.sk",

    # Scraping limits
    "max_list_pages": 0,        # 0 = all pages (use e.g. 3 to cap per-list)
    "enrich_batch_size": 300,   # film detail pages to fetch per run
    "request_delay": 0.35,      # seconds between requests (be polite)

    # Output
    "output_file": "index.html",
    "cache_file": "data.json",
}

# ─── HTTP helpers ────────────────────────────────────────────────────────────

_session = requests.Session()
_session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
})
_last_req = 0.0


def fetch(url, retries=3):
    """Rate-limited GET with retry on transient errors."""
    global _last_req
    for attempt in range(retries):
        elapsed = time.time() - _last_req
        if elapsed < CONFIG["request_delay"]:
            time.sleep(CONFIG["request_delay"] - elapsed)
        _last_req = time.time()
        try:
            r = _session.get(url, timeout=30)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 10))
                print(f"    Rate limited, waiting {wait}s...")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                print(f"    Failed: {url} — {e}")
                return None
    return None


# ─── Cache ───────────────────────────────────────────────────────────────────

def load_cache():
    path = CONFIG["cache_file"]
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {"films": {}, "books": [], "meta": {}}


def save_cache(cache):
    with open(CONFIG["cache_file"], "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)


# ─── Goodreads scraper ──────────────────────────────────────────────────────

STATUS_SHELVES = {
    "to-read", "currently-reading", "read", "want-to-read",
    "favorites", "owned", "books-i-own", "to-buy",
}


def scrape_goodreads(user_id):
    """Scrape the Goodreads to-read shelf via RSS."""
    print("[1/5] Fetching Goodreads to-read shelf...")
    books = []
    page = 1

    while True:
        url = (
            f"https://www.goodreads.com/review/list_rss/{user_id}"
            f"?shelf=to-read&page={page}&per_page=200"
        )
        r = fetch(url)
        if not r:
            break

        try:
            root = ET.fromstring(r.content)
        except ET.ParseError:
            print(f"    XML parse error on page {page}")
            break

        items = root.findall(".//item")
        if not items:
            break

        for item in items:
            title = _xml_text(item, "title")
            if not title:
                continue

            author = _xml_text(item, "author_name")
            pages_str = _xml_text(item, "num_pages")
            avg_rating = _xml_text(item, "average_rating")
            book_id = _xml_text(item, "book_id")
            shelves = _xml_text(item, "user_shelves")
            image = (
                _xml_text(item, "book_large_image_url")
                or _xml_text(item, "book_medium_image_url")
                or _xml_text(item, "book_image_url")
            )

            # Parse shelves into genres (skip status shelves)
            genres = []
            if shelves:
                for s in shelves.split(","):
                    s = s.strip().lower()
                    if s and s not in STATUS_SHELVES:
                        genres.append(s.replace("-", " ").title())

            books.append({
                "title": title.strip(),
                "author": (author or "").strip(),
                "pages": int(pages_str) if pages_str and pages_str.strip().isdigit() else 0,
                "rating": round(float(avg_rating), 2) if avg_rating else 0,
                "goodreads_id": book_id or "",
                "genres": genres[:5],
                "image": image or "",
            })

        page += 1

    print(f"  Found {len(books)} books")
    return books


def _xml_text(parent, tag):
    el = parent.find(tag)
    if el is not None and el.text:
        return el.text.strip()
    return ""


# ─── Letterboxd scrapers ────────────────────────────────────────────────────

def scrape_letterboxd_watchlist(username):
    """Scrape all pages of a user's Letterboxd watchlist."""
    print("[2/5] Fetching Letterboxd watchlist...")
    base = f"https://letterboxd.com/{username}/watchlist/"
    films = _scrape_letterboxd_film_list(base)
    print(f"  Found {len(films)} films in watchlist")
    return films


def scrape_letterboxd_liked_lists(username):
    """Scrape the index of liked lists (all pages)."""
    print("[3/5] Fetching liked lists index...")
    lists = []
    page = 1

    while True:
        url = f"https://letterboxd.com/{username}/likes/lists/"
        if page > 1:
            url += f"page/{page}/"
        r = fetch(url)
        if not r:
            break

        soup = BeautifulSoup(r.text, "html.parser")

        # Find list links — they point to /username/list/slug/
        found_any = False
        for a in soup.find_all("a", href=re.compile(r"/list/[^/]+/?$")):
            href = a.get("href", "")
            name = a.get_text(strip=True)
            if not name or len(name) < 2:
                continue
            full_url = urljoin("https://letterboxd.com", href)
            if full_url not in [l[1] for l in lists]:
                lists.append((name, full_url))
                found_any = True

        if not found_any:
            break

        # Check for next page
        next_link = soup.find("a", class_="next")
        if not next_link:
            break
        page += 1

    print(f"  Found {len(lists)} liked lists")
    return lists


def scrape_letterboxd_list_films(list_name, list_url):
    """Scrape all (or capped) pages of one Letterboxd list."""
    films = _scrape_letterboxd_film_list(list_url)
    return films


def _scrape_letterboxd_film_list(base_url):
    """Generic: scrape film slugs + titles from any Letterboxd list/watchlist."""
    films = []
    seen = set()
    page = 1
    max_pages = CONFIG["max_list_pages"]

    while True:
        url = base_url if page == 1 else f"{base_url.rstrip('/')}/page/{page}/"
        r = fetch(url)
        if not r:
            break

        soup = BeautifulSoup(r.text, "html.parser")

        page_films = []
        # Strategy 1: poster containers with data-film-slug
        for el in soup.find_all(attrs={"data-film-slug": True}):
            slug = el["data-film-slug"]
            if slug not in seen:
                seen.add(slug)
                # Try to get title from alt text of img inside
                img = el.find("img")
                title = img.get("alt", "") if img else ""
                page_films.append((slug, title))

        # Strategy 2: links matching /film/SLUG/
        if not page_films:
            for a in soup.find_all("a", href=re.compile(r"/film/[^/]+/?$")):
                href = a["href"]
                slug = re.search(r"/film/([^/]+)", href).group(1)
                if slug not in seen:
                    seen.add(slug)
                    img = a.find("img")
                    title = img.get("alt", "") if img else a.get_text(strip=True)
                    page_films.append((slug, title))

        if not page_films:
            break

        films.extend(page_films)

        # Pagination
        next_link = soup.find("a", class_="next")
        if not next_link:
            break
        page += 1
        if max_pages and page > max_pages:
            break

    return films


def enrich_film_metadata(slug, cache):
    """Fetch full metadata for one film from its Letterboxd page."""
    if slug in cache and cache[slug].get("enriched"):
        return cache[slug]

    url = f"https://letterboxd.com/film/{slug}/"
    r = fetch(url)
    if not r:
        return cache.get(slug, {})

    soup = BeautifulSoup(r.text, "html.parser")
    meta = cache.get(slug, {})

    # OG title — usually "Title (Year)"
    og = soup.find("meta", property="og:title")
    if og:
        og_content = og.get("content", "")
        m = re.match(r"(.+?)\s*\((\d{4})\)", og_content)
        if m:
            meta["title"] = m.group(1).strip()
            meta["year"] = int(m.group(2))
        else:
            meta.setdefault("title", og_content.strip())

    # JSON-LD structured data
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            ld = json.loads(script.string)
        except (json.JSONDecodeError, TypeError):
            continue

        if isinstance(ld, list):
            ld = ld[0] if ld else {}
        if ld.get("@type") not in ("Movie", "Film", "TVSeries"):
            continue

        if "name" in ld:
            meta.setdefault("title", ld["name"])

        if "genre" in ld:
            g = ld["genre"]
            meta["genres"] = g if isinstance(g, list) else [g]

        if "director" in ld:
            dirs = ld["director"]
            if isinstance(dirs, list):
                meta["director"] = ", ".join(
                    d.get("name", "") for d in dirs if isinstance(d, dict)
                )
            elif isinstance(dirs, dict):
                meta["director"] = dirs.get("name", "")

        if "duration" in ld:
            dur = ld["duration"]
            dm = re.search(r"PT(?:(\d+)H)?(?:(\d+)M)?", str(dur))
            if dm:
                meta["runtime"] = int(dm.group(1) or 0) * 60 + int(dm.group(2) or 0)

        if "aggregateRating" in ld:
            ar = ld["aggregateRating"]
            if isinstance(ar, dict) and "ratingValue" in ar:
                try:
                    meta["rating"] = round(float(ar["ratingValue"]), 2)
                except (ValueError, TypeError):
                    pass

        if "dateCreated" in ld:
            ym = re.match(r"(\d{4})", str(ld["dateCreated"]))
            if ym:
                meta.setdefault("year", int(ym.group(1)))

        if "countryOfOrigin" in ld:
            co = ld["countryOfOrigin"]
            if isinstance(co, list):
                meta["countries"] = [
                    c.get("name", "") for c in co if isinstance(c, dict)
                ]

    # Fallback: parse the page text for runtime if not found
    if "runtime" not in meta:
        text = soup.get_text(" ", strip=True)
        rm = re.search(r"(\d{2,3})\s*mins?", text)
        if rm:
            meta["runtime"] = int(rm.group(1))

    # Fallback: director from page
    if "director" not in meta:
        dir_el = soup.find("a", href=re.compile(r"/director/"))
        if dir_el:
            meta["director"] = dir_el.get_text(strip=True)

    # Fallback: genres from page tab links
    if "genres" not in meta:
        genre_links = soup.find_all("a", href=re.compile(r"/films/genre/"))
        if genre_links:
            meta["genres"] = [g.get_text(strip=True) for g in genre_links[:5]]

    # Extract year from slug as last resort
    if "year" not in meta:
        ym = re.search(r"-(\d{4})$", slug)
        if ym:
            meta["year"] = int(ym.group(1))

    meta["enriched"] = True
    meta["slug"] = slug
    return meta


# ─── Main orchestration ─────────────────────────────────────────────────────

def run_scraper():
    cache = load_cache()
    film_cache = cache.get("films", {})
    no_enrich = "--no-enrich" in sys.argv

    # 1. Goodreads
    books = scrape_goodreads(CONFIG["goodreads_user_id"])

    # 2. Letterboxd watchlist
    watchlist_films = scrape_letterboxd_watchlist(CONFIG["letterboxd_username"])

    # 3. Liked lists index
    liked_lists = scrape_letterboxd_liked_lists(CONFIG["letterboxd_username"])

    # 4. Scrape films from each liked list
    print("[4/5] Scraping liked list films...")
    all_films = {}  # slug → {title, lists: [...]}

    # Add watchlist films
    for slug, title in watchlist_films:
        if slug not in all_films:
            all_films[slug] = {"title": title, "lists": []}
        if "Watchlist" not in all_films[slug]["lists"]:
            all_films[slug]["lists"].append("Watchlist")

    # Add liked list films
    for i, (list_name, list_url) in enumerate(liked_lists, 1):
        short_name = list_name[:60] + ("..." if len(list_name) > 60 else "")
        print(f"  [{i}/{len(liked_lists)}] {short_name}", end="", flush=True)
        films = scrape_letterboxd_list_films(list_name, list_url)
        print(f" ({len(films)} films)")

        for slug, title in films:
            if slug not in all_films:
                all_films[slug] = {"title": title, "lists": []}
            if list_name not in all_films[slug]["lists"]:
                all_films[slug]["lists"].append(list_name)

    total = len(all_films)
    print(f"  Total: {total} unique films")

    # 5. Enrich metadata
    if no_enrich:
        print("[5/5] Skipping metadata enrichment (--no-enrich)")
    else:
        print("[5/5] Enriching film metadata...")
        # Prioritize: watchlist first, then un-enriched films
        slugs_to_enrich = []
        watchlist_slugs = {s for s, _ in watchlist_films}

        # Watchlist films first
        for slug in watchlist_slugs:
            if slug not in film_cache or not film_cache[slug].get("enriched"):
                slugs_to_enrich.append(slug)

        # Then remaining films
        for slug in all_films:
            if slug not in watchlist_slugs:
                if slug not in film_cache or not film_cache[slug].get("enriched"):
                    slugs_to_enrich.append(slug)

        batch = slugs_to_enrich[: CONFIG["enrich_batch_size"]]
        remaining = len(slugs_to_enrich) - len(batch)

        for i, slug in enumerate(batch, 1):
            title_preview = all_films[slug]["title"] or slug
            print(f"  [{i}/{len(batch)}] {title_preview[:50]}", end="", flush=True)
            meta = enrich_film_metadata(slug, film_cache)
            film_cache[slug] = meta
            print(" ✓")

        if remaining > 0:
            print(f"  ({remaining} films will be enriched on the next run)")

    # Merge data: combine scraped list info with cached metadata
    films_out = []
    for slug, info in all_films.items():
        meta = film_cache.get(slug, {})
        films_out.append({
            "title": meta.get("title") or info["title"] or slug.replace("-", " ").title(),
            "year": meta.get("year", 0),
            "director": meta.get("director", ""),
            "runtime": meta.get("runtime", 0),
            "rating": meta.get("rating", 0),
            "genres": meta.get("genres", []),
            "slug": slug,
            "lists": info["lists"],
            "enriched": meta.get("enriched", False),
        })

    # Save cache
    cache["films"] = film_cache
    cache["books"] = books
    cache["meta"] = {
        "last_updated": datetime.now().isoformat(),
        "total_films": len(films_out),
        "total_books": len(books),
        "enriched_films": sum(1 for f in films_out if f["enriched"]),
    }
    save_cache(cache)

    # Generate HTML
    html = generate_html(films_out, books)
    with open(CONFIG["output_file"], "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\nDone! Generated {CONFIG['output_file']}")
    print(f"  {len(films_out)} films, {len(books)} books")
    enriched = sum(1 for f in films_out if f["enriched"])
    print(f"  {enriched}/{len(films_out)} films have full metadata")


# ─── HTML generator ──────────────────────────────────────────────────────────

def generate_html(films, books):
    zlib = CONFIG["zlib_domain"]
    timestamp = datetime.now().strftime("%b %d, %Y %I:%M %p")
    films_json = json.dumps(films, ensure_ascii=False, separators=(",", ":"))
    books_json = json.dumps(books, ensure_ascii=False, separators=(",", ":"))

    # Collect unique values for dropdowns
    all_film_genres = sorted({g for f in films for g in f["genres"]})
    all_film_lists = sorted({l for f in films for l in f["lists"]})
    all_book_genres = sorted({g for b in books for g in b["genres"]})

    film_genre_opts = "".join(f'<option value="{g}">{g}</option>' for g in all_film_genres)
    film_list_opts = "".join(f'<option value="{_esc(l)}">{_esc(l[:45])}</option>' for l in all_film_lists)
    book_genre_opts = "".join(f'<option value="{g}">{g}</option>' for g in all_book_genres)

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Media Tracker</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
:root{{
  --bg:#0c0c0c;--s1:#161616;--s2:#1e1e1e;--s3:#282828;
  --bd:#2a2a2a;--tx:#e4e4e4;--t2:#8a8a8a;--t3:#5a5a5a;
  --ac:#d4a647;--ac2:#b8912e;
  --r1:#e05555;--r2:#9b6abf;--r3:#3cc47a;--r4:#4a9ede;--r5:#d4a647;
}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,sans-serif;background:var(--bg);color:var(--tx);min-height:100vh}}
a{{color:inherit;text-decoration:none}}

/* Header */
.hdr{{padding:28px 16px 0;text-align:center}}
.hdr h1{{font-size:22px;font-weight:700;letter-spacing:2px;color:var(--ac)}}
.hdr .sub{{font-size:12px;color:var(--t3);margin-top:4px}}

/* Tabs */
.tabs{{display:flex;justify-content:center;gap:4px;padding:20px 16px 0}}
.tab{{padding:8px 28px;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer;
  background:var(--s1);color:var(--t2);border:1px solid var(--bd);transition:.15s}}
.tab:hover{{color:var(--tx);border-color:var(--t3)}}
.tab.on{{background:var(--s2);color:var(--ac);border-color:var(--ac2)}}

/* Controls */
.wrap{{max-width:880px;margin:0 auto;padding:0 16px}}
.ctrl{{display:flex;gap:8px;flex-wrap:wrap;padding:16px 0 8px}}
.ctrl input,.ctrl select{{
  background:var(--s1);border:1px solid var(--bd);color:var(--tx);
  padding:8px 12px;border-radius:6px;font-size:13px;outline:none;transition:.15s}}
.ctrl input:focus,.ctrl select:focus{{border-color:var(--ac)}}
.ctrl input{{flex:1;min-width:160px}}
.ctrl select{{min-width:130px;max-width:200px}}
.info{{display:flex;justify-content:space-between;align-items:center;padding:4px 0 10px;font-size:12px;color:var(--t3)}}

/* Cards */
.list{{display:flex;flex-direction:column;gap:6px;padding-bottom:40px}}
.card{{background:var(--s1);border:1px solid var(--bd);border-radius:8px;
  padding:12px 14px;display:flex;align-items:center;gap:14px;transition:.15s}}
.card:hover{{border-color:var(--ac);background:var(--s2)}}
.nfo{{flex:1;min-width:0;overflow:hidden}}
.nfo h3{{font-size:15px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.nfo .meta{{font-size:12px;color:var(--t2);margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.nfo .tags{{margin-top:4px;display:flex;gap:4px;flex-wrap:wrap}}
.tag{{font-size:10px;padding:2px 7px;border-radius:3px;background:rgba(212,166,71,.1);color:var(--ac)}}
.tag.g{{background:rgba(60,196,122,.1);color:var(--r3)}}
.btns{{display:flex;gap:5px;flex-shrink:0;flex-wrap:wrap;justify-content:flex-end}}
.btn{{display:inline-block;padding:5px 11px;border-radius:5px;font-size:11px;font-weight:600;
  color:#fff;white-space:nowrap;transition:.15s}}
.btn:hover{{opacity:.85;transform:translateY(-1px)}}
.b1{{background:var(--r1)}}.b2{{background:var(--r2)}}.b3{{background:var(--r3);color:#000}}
.b4{{background:var(--r4)}}.b5{{background:var(--r5);color:#000}}
.empty{{text-align:center;padding:48px 16px;color:var(--t3)}}

/* Responsive */
@media(max-width:600px){{
  .card{{flex-direction:column;align-items:flex-start}}
  .btns{{width:100%;justify-content:flex-start}}
  .ctrl{{flex-direction:column}}
  .ctrl input,.ctrl select{{width:100%;max-width:none}}
}}
</style>
</head>
<body>
<div class="hdr">
  <h1>MEDIA TRACKER</h1>
  <div class="sub">Updated {timestamp} &bull; {len(films)} films &bull; {len(books)} books</div>
</div>

<div class="tabs">
  <div class="tab on" onclick="switchTab('films')">Films</div>
  <div class="tab" onclick="switchTab('books')">Books</div>
</div>

<div class="wrap">
  <!-- Film controls -->
  <div id="fc" class="ctrl">
    <input type="text" id="fq" placeholder="Search films..." autocomplete="off">
    <select id="fg"><option value="">All Genres</option>{film_genre_opts}</select>
    <select id="fl"><option value="">All Lists</option>{film_list_opts}</select>
    <select id="fs">
      <option value="title">Sort: Title</option>
      <option value="rating">Sort: Rating</option>
      <option value="year">Sort: Year</option>
      <option value="runtime">Sort: Runtime</option>
    </select>
  </div>
  <!-- Book controls -->
  <div id="bc" class="ctrl" style="display:none">
    <input type="text" id="bq" placeholder="Search books..." autocomplete="off">
    <select id="bg"><option value="">All Genres</option>{book_genre_opts}</select>
    <select id="bs">
      <option value="title">Sort: Title</option>
      <option value="rating">Sort: Rating</option>
      <option value="pages">Sort: Pages</option>
    </select>
  </div>
  <div class="info"><span id="ct"></span><span id="zd" style="cursor:pointer" title="Click to change Z-Library domain">Z-Lib: {zlib}</span></div>
  <div class="list" id="out"></div>
</div>

<script>
const F={films_json};
const B={books_json};
let tab="films",zlib="{zlib}";

function switchTab(t){{
  tab=t;
  document.querySelectorAll(".tab").forEach((el,i)=>el.classList.toggle("on",i===(t==="films"?0:1)));
  document.getElementById("fc").style.display=t==="films"?"flex":"none";
  document.getElementById("bc").style.display=t==="books"?"flex":"none";
  render();
}}

function esc(s){{const d=document.createElement("div");d.textContent=s;return d.innerHTML}}

function surl(site,title){{
  const q=encodeURIComponent(title);
  if(site==="movy")return"https://www.movy.sx/search?q="+q;
  if(site==="mn")return"https://movienig.ht/search?q="+q;
  if(site==="rive")return"https://rivestream.ru/search?q="+q;
  if(site==="zlib")return"https://"+zlib+"/s/"+q+"?languages%5B0%5D=english&order=popular";
}}

function render(){{
  const out=document.getElementById("out");
  const ct=document.getElementById("ct");
  if(tab==="films"){{
    const q=document.getElementById("fq").value.toLowerCase();
    const g=document.getElementById("fg").value;
    const l=document.getElementById("fl").value;
    const s=document.getElementById("fs").value;
    let r=F.filter(f=>{{
      if(g&&(!f.genres||!f.genres.includes(g)))return false;
      if(l&&(!f.lists||!f.lists.includes(l)))return false;
      if(q){{const h=(f.title+" "+f.director+" "+(f.genres||[]).join(" ")+" "+f.year).toLowerCase();if(!h.includes(q))return false}}
      return true;
    }});
    r.sort((a,b)=>{{
      if(s==="rating")return(b.rating||0)-(a.rating||0);
      if(s==="year")return(b.year||0)-(a.year||0);
      if(s==="runtime")return(b.runtime||0)-(a.runtime||0);
      return a.title.localeCompare(b.title);
    }});
    ct.textContent=r.length+" of "+F.length+" films";
    if(!r.length){{out.innerHTML='<div class="empty">No films match.</div>';return}}
    out.innerHTML=r.map(f=>{{
      const parts=[f.director,f.genres&&f.genres[0],f.year||"",f.runtime?f.runtime+"m":""].filter(Boolean);
      const meta=parts.join(" \\u00b7 ");
      const stars=f.rating?"\\u2605 "+f.rating.toFixed(1):"";
      const ls=f.lists||[];
      return'<div class="card"><div class="nfo"><h3>'+esc(f.title)+(stars?' <span style="color:var(--ac);font-size:12px">'+stars+"</span>":"")+"</h3>"+
        (meta?'<div class="meta">'+esc(meta)+"</div>":"")+
        (ls.length?'<div class="tags">'+ls.slice(0,3).map(l=>'<span class="tag">'+esc(l.length>30?l.slice(0,28)+"…":l)+"</span>").join("")+(ls.length>3?'<span class="tag">+'+( ls.length-3)+"</span>":"")+"</div>":"")+
        '</div><div class="btns">'+
        '<a class="btn b1" href="'+surl("movy",f.title)+'" target="_blank" rel="noopener">movy</a>'+
        '<a class="btn b2" href="'+surl("mn",f.title)+'" target="_blank" rel="noopener">movienig.ht</a>'+
        '<a class="btn b3" href="'+surl("rive",f.title)+'" target="_blank" rel="noopener">rive</a>'+
        '<a class="btn b5" href="https://letterboxd.com/film/'+f.slug+'/" target="_blank" rel="noopener">LB</a>'+
        "</div></div>"
    }}).join("");
  }} else {{
    const q=document.getElementById("bq").value.toLowerCase();
    const g=document.getElementById("bg").value;
    const s=document.getElementById("bs").value;
    let r=B.filter(b=>{{
      if(g&&(!b.genres||!b.genres.includes(g)))return false;
      if(q){{const h=(b.title+" "+b.author+" "+(b.genres||[]).join(" ")).toLowerCase();if(!h.includes(q))return false}}
      return true;
    }});
    r.sort((a,b)=>{{
      if(s==="rating")return(b.rating||0)-(a.rating||0);
      if(s==="pages")return(b.pages||0)-(a.pages||0);
      return a.title.localeCompare(b.title);
    }});
    ct.textContent=r.length+" of "+B.length+" books";
    if(!r.length){{out.innerHTML='<div class="empty">No books match.</div>';return}}
    out.innerHTML=r.map(b=>{{
      const parts=[b.author,b.genres&&b.genres[0],b.pages?b.pages+"p":""].filter(Boolean);
      const meta=parts.join(" \\u00b7 ");
      const stars=b.rating?"\\u2605 "+b.rating.toFixed(1):"";
      return'<div class="card"><div class="nfo"><h3>'+esc(b.title)+(stars?' <span style="color:var(--r3);font-size:12px">'+stars+"</span>":"")+"</h3>"+
        (meta?'<div class="meta">'+esc(meta)+"</div>":"")+
        (b.genres&&b.genres.length?'<div class="tags">'+b.genres.slice(0,3).map(g=>'<span class="tag g">'+esc(g)+"</span>").join("")+"</div>":"")+
        '</div><div class="btns">'+
        '<a class="btn b4" href="'+surl("zlib",b.title+" "+b.author)+'" target="_blank" rel="noopener">Z-Library</a>'+
        (b.goodreads_id?'<a class="btn b5" href="https://www.goodreads.com/book/show/'+b.goodreads_id+'" target="_blank" rel="noopener">GR</a>':"")+
        "</div></div>"
    }}).join("");
  }}
}}

// Z-Lib domain swap
document.getElementById("zd").addEventListener("click",()=>{{
  const d=prompt("Enter Z-Library domain:",zlib);
  if(d&&d.trim()){{zlib=d.trim();document.getElementById("zd").textContent="Z-Lib: "+zlib;render()}}
}});

// Bind controls
["fq","fg","fl","fs","bq","bg","bs"].forEach(id=>{{
  const el=document.getElementById(id);
  el.addEventListener(el.tagName==="SELECT"?"change":"input",render);
}});

render();
</script>
</body>
</html>'''


def _esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# ─── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    run_scraper()
'''

