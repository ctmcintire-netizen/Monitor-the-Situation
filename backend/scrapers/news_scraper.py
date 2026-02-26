"""
News scrapers: RSS feeds + GDELT real-time API
"""
import asyncio
import hashlib
from datetime import datetime, timezone
from typing import AsyncGenerator
import feedparser
import httpx
from loguru import logger

from backend.processors.geo_tagger import resolve_geo, classify_category, classify_severity, is_breaking, make_event_id, classify_topics

# ── RSS Feed Sources ─────────────────────────────────────────────────────────
RSS_FEEDS = [
    # Global wire services
    {"url": "https://feeds.bbci.co.uk/news/world/rss.xml",         "source": "BBC World"},
    {"url": "https://rss.reuters.com/reuters/worldNews",            "source": "Reuters"},
    {"url": "https://www.aljazeera.com/xml/rss/all.xml",           "source": "Al Jazeera"},
    {"url": "https://feeds.npr.org/1004/rss.xml",                   "source": "NPR World"},
    {"url": "https://www.theguardian.com/world/rss",               "source": "Guardian"},
    {"url": "https://feeds.skynews.com/feeds/rss/world.xml",       "source": "Sky News"},
    # Conflict / security focused
    {"url": "https://theintercept.com/feed/?rss",                  "source": "The Intercept"},
    {"url": "https://www.bellingcat.com/feed/",                    "source": "Bellingcat"},
    {"url": "https://www.rferl.org/api/zpqos-uyovem",             "source": "RFERL"},
    # Regional
    {"url": "https://english.alarabiya.net/rss.xml",               "source": "Al Arabiya"},
    {"url": "https://www.channelnewsasia.com/rssfeeds/8395986",    "source": "CNA Asia"},
    {"url": "https://reliefweb.int/updates/rss.xml",               "source": "ReliefWeb"},
    {"url": "https://www.presstv.ir/rss.xml",                      "source": "PressTV"},
    {"url": "https://tass.com/rss/v2.xml",                         "source": "TASS"},
]


async def fetch_rss_feed(feed_info: dict, client: httpx.AsyncClient) -> list[dict]:
    """Fetch and parse a single RSS feed, returning normalized event dicts."""
    events = []
    try:
        resp = await client.get(feed_info["url"], timeout=15)
        resp.raise_for_status()
        parsed = feedparser.parse(resp.text)

        for entry in parsed.entries[:20]:  # cap at 20 per feed
            title = getattr(entry, "title", "")
            summary = getattr(entry, "summary", "") or getattr(entry, "description", "")
            url = getattr(entry, "link", "")
            
            # Clean HTML from summary
            import re
            summary = re.sub(r'<[^>]+>', '', summary)[:500]

            published_at = datetime.now(timezone.utc)
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                published_at = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)

            full_text = f"{title} {summary}"
            geo = resolve_geo(full_text)

            if not geo:
                continue  # skip ungeo-taggable items for map purposes

            event_id = make_event_id(url, title)
            events.append({
                "id": event_id,
                "title": title,
                "summary": summary,
                "url": url,
                "source": feed_info["source"],
                "source_type": "rss",
                "category": classify_category(full_text),
                "topics": classify_topics(full_text),
                "severity": classify_severity(full_text),
                "is_breaking": is_breaking(full_text),
                "media_urls": [],
                "raw_tags": [],
                "published_at": published_at.isoformat(),
                **geo,
            })
    except Exception as e:
        logger.warning(f"RSS fetch failed for {feed_info['source']}: {e}")
    
    return events


async def scrape_all_rss() -> list[dict]:
    """Scrape all RSS feeds concurrently."""
    logger.info("Starting RSS scrape cycle...")
    async with httpx.AsyncClient(headers={"User-Agent": "OSINT-Monitor/1.0"}) as client:
        tasks = [fetch_rss_feed(feed, client) for feed in RSS_FEEDS]
        results = await asyncio.gather(*tasks, return_exceptions=True)
    
    all_events = []
    for r in results:
        if isinstance(r, list):
            all_events.extend(r)
    
    logger.info(f"RSS scrape complete: {len(all_events)} geolocatable events")
    return all_events


# ── GDELT Scraper ─────────────────────────────────────────────────────────────
GDELT_API = "https://api.gdeltproject.org/api/v2/doc/doc"

GDELT_THEMES = [
    "CRISISLEX_CRISISLEXREC",  # crisis / emergency
    "MILITARY_ATTACK",
    "WB_2065_NATURAL_DISASTER",
    "ELECTION_FRAUD",
    "PROTEST",
    "TAX_FNCACT_COUP",
]


async def scrape_gdelt() -> list[dict]:
    """Pull last 15 minutes of events from GDELT doc API."""
    logger.info("Fetching GDELT events...")
    events = []

    params = {
        "query": "crisis OR attack OR explosion OR earthquake OR flood OR shooting OR protest",
        "mode": "artlist",
        "maxrecords": 75,
        "timespan": "15min",
        "format": "json",
        "sort": "HybridRel",
    }

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(GDELT_API, params=params, timeout=20)
            data = resp.json()

        articles = data.get("articles", [])
        for art in articles:
            title = art.get("title", "")
            url = art.get("url", "")
            seendate = art.get("seendate", "")
            domain = art.get("domain", "")
            
            # GDELT provides socialimage sometimes
            media_urls = []
            if art.get("socialimage"):
                media_urls.append(art["socialimage"])

            full_text = f"{title} {art.get('seendescription', '')}"
            geo = resolve_geo(full_text)

            if not geo:
                # Try using the sourcecountry hint from GDELT
                country = art.get("sourcecountry", "")
                if country:
                    geo = resolve_geo(country)

            if not geo:
                continue

            try:
                published_at = datetime.strptime(seendate, "%Y%m%dT%H%M%SZ").isoformat()
            except Exception:
                published_at = datetime.utcnow().isoformat()

            event_id = make_event_id(url, title)
            events.append({
                "id": event_id,
                "title": title,
                "summary": art.get("seendescription", "")[:500],
                "url": url,
                "source": domain,
                "source_type": "gdelt",
                "category": classify_category(full_text),
                "topics": classify_topics(full_text),
                "severity": classify_severity(full_text),
                "is_breaking": is_breaking(full_text),
                "media_urls": media_urls,
                "raw_tags": art.get("themes", "").split(";") if art.get("themes") else [],
                "published_at": published_at,
                **geo,
            })

    except Exception as e:
        logger.error(f"GDELT scrape failed: {e}")

    logger.info(f"GDELT returned {len(events)} events")
    return events
