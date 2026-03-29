"""
venezuela_news_fetcher.py

Fetches RSS headlines from Venezuelan independent news sources, translates
Spanish content to English, categorizes stories, and writes output to
docs/venezuela_news.json.

Categories: Diplomacy, Military, Energy, Economy, Local Events
Max 20 stories per category, no story older than 7 days.
Replaces oldest entries when new stories are found.
No API keys required — uses deep-translator (Google Translate free tier).

SOURCE NOTES:
  All five requested sources are confirmed active with public RSS feeds.
  They are WordPress-based outlets that publish their feeds at /feed.

  IMPORTANT CONTEXT: All five outlets are BLOCKED inside Venezuela by the
  Maduro government (documented by VE Sin Filtro / Freedom House). However,
  they are operated from international hosting (many editors work in exile)
  and their feeds are freely accessible from outside Venezuela — including
  from GitHub Actions runners (US-based), which is exactly where this
  script runs. This is not a scraping obstacle; it actually underscores
  why these are the right sources to monitor.

  - El Nacional       → https://www.elnacional.com  (elnacional.com/feed)
  - Efecto Cocuyo     → https://efectococuyo.com    (efectococuyo.com/feed)
  - El Pitazo         → https://elpitazo.net         (elpitazo.net/feed)
  - TalCual           → https://talcualdigital.com   (talcualdigital.com/feed)
  - Runrun.es         → https://runrun.es             (runrun.es/feed)

  El Nacional is now hosted at elnacional.com (note: the canonical domain
  redirected from el-nacional.com; both are tried below).
"""

import json
import re
import time
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

import feedparser
import requests
from deep_translator import GoogleTranslator

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

COUNTRY = "venezuela"
OUTPUT_DIR = Path("docs")
OUTPUT_FILE = OUTPUT_DIR / f"{COUNTRY}_news.json"

MAX_PER_CATEGORY = 20
MAX_AGE_DAYS = 7

CATEGORIES = ["Diplomacy", "Military", "Energy", "Economy", "Local Events"]

# ---------------------------------------------------------------------------
# RSS Sources
# ---------------------------------------------------------------------------

RSS_SOURCES = [
    {
        "name": "El Nacional",
        "urls": [
            "https://www.elnacional.com/feed/",
            "https://www.el-nacional.com/feed/",          # legacy domain fallback
            "https://elnacional.com/venezuela/politica/feed/",
            "https://elnacional.com/economia/feed/",
        ],
        "lang": "es",
    },
    {
        "name": "Efecto Cocuyo",
        "urls": [
            "https://efectococuyo.com/feed/",
            "https://efectococuyo.com/category/politica/feed/",
            "https://efectococuyo.com/category/economia/feed/",
        ],
        "lang": "es",
    },
    {
        "name": "El Pitazo",
        "urls": [
            "https://elpitazo.net/feed/",
            "https://elpitazo.net/category/politica/feed/",
            "https://elpitazo.net/category/economia/feed/",
            "https://elpitazo.net/category/regiones/feed/",
        ],
        "lang": "es",
    },
    {
        "name": "TalCual",
        "urls": [
            "https://talcualdigital.com/feed/",
            "https://talcualdigital.com/category/politica/feed/",
            "https://talcualdigital.com/category/economia/feed/",
        ],
        "lang": "es",
    },
    {
        "name": "Runrun.es",
        "urls": [
            "https://runrun.es/feed/",
            "https://runrun.es/category/politica/feed/",
            "https://runrun.es/category/economia/feed/",
        ],
        "lang": "es",
    },
]

# ---------------------------------------------------------------------------
# Category keyword rules (applied after translation to English)
# ---------------------------------------------------------------------------

CATEGORY_KEYWORDS = {
    "Diplomacy": [
        "diplomacy", "diplomatic", "foreign minister", "ambassador", "embassy",
        "treaty", "bilateral", "multilateral", "united nations", "un ",
        "g20", "summit", "foreign affairs", "foreign policy", "sanctions",
        "maduro", "recognition", "international", "state visit", "relations",
        "ally", "allies", "consulate", "visa", "negotiations", "dialogue",
        "cuba", "russia", "china", "iran", "colombia relations",
        "united states", "usaid", "oas", "celac", "mercosur",
        "secretary of state", "chancellery", "cancilleria",
    ],
    "Military": [
        "military", "armed forces", "army", "navy", "air force", "defense",
        "fanb", "national bolivarian armed forces", "colectivo", "colectivos",
        "paramilitary", "weapons", "war", "conflict", "troops", "soldier",
        "general", "security forces", "sebin", "dgcim", "police",
        "operation", "combat", "guerrilla", "eln", "farc", "drug trafficking",
        "crime", "gang", "prison", "inpec", "border security",
        "national guard", "guardia nacional", "detention", "arrest",
        "missile", "arms", "munitions",
    ],
    "Energy": [
        "energy", "oil", "gas", "petroleum", "pdvsa", "refinery",
        "electricity", "power", "blackout", "corpoelec", "outage",
        "fuel", "gasoline", "gasoil", "natural gas", "pipeline",
        "renewable", "solar", "hydroelectric", "guri", "power plant",
        "carbon", "emissions", "climate", "barrel", "production",
        "chevron", "oil export", "oil price", "petrostate",
        "energy crisis", "power cut", "electric service",
    ],
    "Economy": [
        "economy", "economic", "gdp", "inflation", "interest rate",
        "central bank", "bcv", "finance", "budget", "fiscal", "tax",
        "trade", "exports", "imports", "investment", "market",
        "bolivar", "dollar", "dollarization", "currency", "exchange rate",
        "unemployment", "jobs", "industry", "agriculture",
        "food", "shortages", "hyperinflation", "recession", "growth",
        "minister of finance", "treasury", "debt", "imf", "world bank",
        "remittances", "diaspora economy", "poverty", "salaries", "wages",
        "bonos", "petro", "crypto", "commerce",
    ],
    "Local Events": [
        "caracas", "maracaibo", "valencia", "barquisimeto", "maracay",
        "ciudad guayana", "barcelona", "maturin", "cumaná", "merida",
        "state", "municipality", "local", "governor", "mayor",
        "alcalde", "gobernador", "community", "neighborhood",
        "flood", "landslide", "earthquake", "fire", "drought",
        "protest", "strike", "election", "education", "health",
        "hospital", "infrastructure", "transport", "road", "bridge",
        "water", "aqueduct", "service", "culture", "festival",
        "social", "indigenous", "human rights", "migration", "exodus",
    ],
}


def classify(title: str, description: str) -> str:
    """Return the best matching category or 'Local Events' as fallback."""
    text = (title + " " + (description or "")).lower()
    scores = {cat: 0 for cat in CATEGORIES}
    for cat, keywords in CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw in text:
                scores[cat] += 1
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "Local Events"


# ---------------------------------------------------------------------------
# Translation helper
# ---------------------------------------------------------------------------

def safe_translate(text: str, source_lang: str = "auto") -> str:
    """Translate text to English; return original on failure."""
    if not text or not text.strip():
        return text
    if source_lang == "en":
        return text
    # Skip if already looks English
    latin_common = re.compile(r"\b(the|and|is|in|of|to|a|for|on|that|with)\b", re.I)
    if len(latin_common.findall(text)) >= 3:
        return text
    try:
        translator = GoogleTranslator(source=source_lang, target="en")
        result = translator.translate(text[:4900])
        return result if result else text
    except Exception as exc:
        log.warning("Translation failed for '%s…': %s", text[:60], exc)
        return text


# ---------------------------------------------------------------------------
# Feed fetching
# ---------------------------------------------------------------------------

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; StrategaemdriveVenezuelaNewsBot/1.0; "
        "+https://stratagemdrive.github.io)"
    )
}


def fetch_feed(url: str) -> list:
    """Fetch and parse a single RSS/Atom feed URL; return raw entries."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        log.info("  Fetched %d entries from %s", len(feed.entries), url)
        return feed.entries
    except Exception as exc:
        log.warning("  Could not fetch %s: %s", url, exc)
        return []


def parse_published(entry) -> datetime | None:
    """Extract a timezone-aware published datetime from a feed entry."""
    for attr in ("published_parsed", "updated_parsed"):
        t = getattr(entry, attr, None)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    for attr in ("published", "updated"):
        s = getattr(entry, attr, None)
        if s:
            try:
                from email.utils import parsedate_to_datetime
                return parsedate_to_datetime(s)
            except Exception:
                pass
    return None


def entry_to_story(entry, source_name: str, source_lang: str) -> dict | None:
    """Convert a feed entry to a story dict; return None if unusable."""
    title_raw = getattr(entry, "title", "") or ""
    desc_raw = getattr(entry, "summary", "") or ""
    desc_clean = re.sub(r"<[^>]+>", " ", desc_raw).strip()
    url = getattr(entry, "link", "") or ""

    published_dt = parse_published(entry)
    if not published_dt:
        published_dt = datetime.now(timezone.utc)

    age = datetime.now(timezone.utc) - published_dt
    if age > timedelta(days=MAX_AGE_DAYS):
        return None

    title_en = safe_translate(title_raw, source_lang)
    desc_en = safe_translate(desc_clean[:300], source_lang) if desc_clean else ""

    category = classify(title_en, desc_en)

    return {
        "title": title_en.strip(),
        "source": source_name,
        "url": url.strip(),
        "published_date": published_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "category": category,
    }


# ---------------------------------------------------------------------------
# JSON store management
# ---------------------------------------------------------------------------

def load_existing() -> dict:
    if OUTPUT_FILE.exists():
        try:
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "stories" in data:
                return data
        except Exception as exc:
            log.warning("Could not load existing JSON: %s", exc)
    return {"stories": [], "last_updated": ""}


def save_output(data: dict) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log.info("Saved %d stories to %s", len(data["stories"]), OUTPUT_FILE)


# ---------------------------------------------------------------------------
# Merge logic
# ---------------------------------------------------------------------------

def merge_stories(existing_stories: list, new_stories: list) -> list:
    """
    Merge new stories into existing per category:
    - Drop stories older than MAX_AGE_DAYS
    - Deduplicate by URL
    - Keep up to MAX_PER_CATEGORY per category (newest first)
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)

    by_url: dict[str, dict] = {}
    for s in existing_stories:
        url = s.get("url", "")
        if url:
            by_url[url] = s
    for s in new_stories:
        url = s.get("url", "")
        if url:
            by_url[url] = s

    fresh = []
    for s in by_url.values():
        try:
            pub = datetime.strptime(
                s["published_date"], "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=timezone.utc)
            if pub >= cutoff:
                fresh.append(s)
        except Exception:
            pass

    by_cat: dict[str, list] = {cat: [] for cat in CATEGORIES}
    for s in fresh:
        cat = s.get("category", "Local Events")
        if cat not in by_cat:
            cat = "Local Events"
        by_cat[cat].append(s)

    result = []
    for cat in CATEGORIES:
        entries = sorted(
            by_cat[cat],
            key=lambda x: x.get("published_date", ""),
            reverse=True,
        )
        result.extend(entries[:MAX_PER_CATEGORY])

    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    log.info("=== Venezuela News Fetcher starting ===")
    existing_data = load_existing()
    existing_stories = existing_data.get("stories", [])

    all_new: list[dict] = []

    for source in RSS_SOURCES:
        source_name = source["name"]
        source_lang = source.get("lang", "auto")
        log.info("Processing source: %s (lang: %s)", source_name, source_lang)
        for url in source["urls"]:
            entries = fetch_feed(url)
            for entry in entries:
                story = entry_to_story(entry, source_name, source_lang)
                if story:
                    all_new.append(story)
            time.sleep(1)

    log.info("Collected %d candidate new stories", len(all_new))

    merged = merge_stories(existing_stories, all_new)

    for cat in CATEGORIES:
        count = sum(1 for s in merged if s.get("category") == cat)
        log.info("  %-15s: %d stories", cat, count)

    output = {
        "country": COUNTRY,
        "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stories": merged,
    }

    save_output(output)
    log.info("=== Done ===")


if __name__ == "__main__":
    main()
