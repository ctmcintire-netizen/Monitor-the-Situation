"""
X / Twitter OSINT scraper
Primary:  Twitter API v2 (tweepy)
Fallback: Nitter instance scraping (BeautifulSoup)
"""
import os
import asyncio
import re
import hashlib
from datetime import datetime, timezone
from typing import Optional
import httpx
from bs4 import BeautifulSoup
from loguru import logger

from backend.processors.geo_tagger import resolve_geo, classify_category, classify_severity, is_breaking, classify_topics

TWITTER_BEARER = os.getenv("TWITTER_BEARER_TOKEN", "")
OSINT_ACCOUNTS = [a.strip() for a in os.getenv(
    "OSINT_ACCOUNTS",
    "sentdefender,IntelCrab,OSINTdefender,RALee85,GeoConfirmed,Conflicts,"
    "WarMonitors,Intel_Sky,Osinttechnical,Tendar,Archer83Actual,AA_Battlespace"
).split(",")]

NITTER_INSTANCES = [n.strip() for n in os.getenv(
    "NITTER_INSTANCES",
    "https://nitter.net,https://nitter.it,https://nitter.poast.org"
).split(",")]


def _tweet_id(account: str, text: str) -> str:
    return hashlib.sha256(f"{account}{text[:80]}".encode()).hexdigest()[:16]


def _extract_media_from_tweet(tweet_obj) -> list[str]:
    """Extract image/video URLs from tweepy tweet object."""
    urls = []
    if hasattr(tweet_obj, "attachments") and tweet_obj.attachments:
        # media keys resolved separately in the includes
        pass
    return urls


def _parse_tweet_text(account: str, text: str, published_at: str, 
                      media_urls: list, method: str) -> dict:
    """Convert raw tweet data to normalized OSINT tweet dict."""
    hashtags = re.findall(r'#(\w+)', text)
    geo = resolve_geo(text, hint_locations=hashtags)

    return {
        "id": _tweet_id(account, text),
        "account": account,
        "text": text,
        "url": f"https://x.com/{account}",
        "media_urls": media_urls,
        "hashtags": hashtags,
        "location_name": geo.get("location_name", ""),
        "lat": geo.get("lat"),
        "lon": geo.get("lon"),
        "category": classify_category(text),
        "topics": classify_topics(text),
        "severity": classify_severity(text),
        "is_breaking": is_breaking(text),
        "published_at": published_at,
        "source_method": method,
    }


# ── Twitter API v2 ─────────────────────────────────────────────────────────

async def fetch_twitter_api(account: str, client: httpx.AsyncClient) -> list[dict]:
    """Fetch latest tweets for an account via Twitter API v2."""
    if not TWITTER_BEARER:
        return []
    
    tweets = []
    headers = {"Authorization": f"Bearer {TWITTER_BEARER}"}
    
    try:
        # Get user ID
        resp = await client.get(
            f"https://api.twitter.com/2/users/by/username/{account}",
            headers=headers, timeout=10
        )
        if resp.status_code != 200:
            return []
        user_id = resp.json()["data"]["id"]

        # Get tweets
        resp = await client.get(
            f"https://api.twitter.com/2/users/{user_id}/tweets",
            headers=headers,
            params={
                "max_results": 10,
                "tweet.fields": "created_at,entities,attachments",
                "expansions": "attachments.media_keys",
                "media.fields": "url,preview_image_url,type",
            },
            timeout=10
        )
        if resp.status_code != 200:
            return []

        data = resp.json()
        media_map = {}
        for m in data.get("includes", {}).get("media", []):
            media_map[m["media_key"]] = m.get("url") or m.get("preview_image_url", "")

        for tweet in data.get("data", []):
            media_urls = []
            for mk in (tweet.get("attachments") or {}).get("media_keys", []):
                if mk in media_map and media_map[mk]:
                    media_urls.append(media_map[mk])

            tweets.append(_parse_tweet_text(
                account=account,
                text=tweet["text"],
                published_at=tweet.get("created_at", datetime.utcnow().isoformat()),
                media_urls=media_urls,
                method="twitter_api"
            ))

    except Exception as e:
        logger.warning(f"Twitter API failed for @{account}: {e}")

    return tweets


# ── Nitter Fallback ────────────────────────────────────────────────────────

async def fetch_nitter(account: str, client: httpx.AsyncClient) -> list[dict]:
    """Scrape tweets from a Nitter instance."""
    tweets = []
    
    for instance in NITTER_INSTANCES:
        try:
            url = f"{instance}/{account}"
            resp = await client.get(url, timeout=15, follow_redirects=True)
            if resp.status_code != 200:
                continue

            soup = BeautifulSoup(resp.text, "lxml")
            tweet_items = soup.select(".timeline-item")

            for item in tweet_items[:10]:
                # Text
                content_el = item.select_one(".tweet-content")
                if not content_el:
                    continue
                text = content_el.get_text(" ", strip=True)

                # Timestamp
                time_el = item.select_one(".tweet-date a")
                published_at = datetime.utcnow().isoformat()
                if time_el and time_el.get("title"):
                    try:
                        published_at = datetime.strptime(
                            time_el["title"], "%b %d, %Y · %I:%M %p %Z"
                        ).isoformat()
                    except Exception:
                        pass

                # Media
                media_urls = []
                for img in item.select(".attachment img"):
                    src = img.get("src", "")
                    if src:
                        media_urls.append(f"{instance}{src}" if src.startswith("/") else src)
                for video in item.select(".gif source, video source"):
                    src = video.get("src", "")
                    if src:
                        media_urls.append(f"{instance}{src}" if src.startswith("/") else src)

                tweets.append(_parse_tweet_text(
                    account=account,
                    text=text,
                    published_at=published_at,
                    media_urls=media_urls,
                    method="nitter"
                ))

            if tweets:
                break  # success — no need to try next instance

        except Exception as e:
            logger.warning(f"Nitter {instance} failed for @{account}: {e}")
            continue

    return tweets


# ── Main scraper ──────────────────────────────────────────────────────────

async def scrape_osint_account(account: str, client: httpx.AsyncClient) -> list[dict]:
    """Try Twitter API first, fall back to Nitter."""
    tweets = []
    
    if TWITTER_BEARER:
        tweets = await fetch_twitter_api(account, client)
    
    if not tweets:
        tweets = await fetch_nitter(account, client)
    
    logger.debug(f"@{account}: {len(tweets)} tweets fetched")
    return tweets


async def scrape_all_osint_accounts() -> list[dict]:
    """Scrape all configured OSINT accounts concurrently."""
    logger.info(f"Scraping {len(OSINT_ACCOUNTS)} OSINT accounts...")
    
    async with httpx.AsyncClient(
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        },
        follow_redirects=True
    ) as client:
        tasks = [scrape_osint_account(acc, client) for acc in OSINT_ACCOUNTS]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    all_tweets = []
    for r in results:
        if isinstance(r, list):
            all_tweets.extend(r)

    logger.info(f"OSINT scrape complete: {len(all_tweets)} tweets")
    return all_tweets
