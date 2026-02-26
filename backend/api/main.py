"""
OSINT News Monitor — FastAPI Backend
Serves events and tweets to the frontend map.
Runs background scraping via APScheduler.
"""
import os
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger
import json
import redis.asyncio as aioredis

from backend.database import init_db
from backend.scrapers.news_scraper import scrape_all_rss, scrape_gdelt
from backend.scrapers.twitter_scraper import scrape_all_osint_accounts

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "*").split(",")

RSS_INTERVAL    = int(os.getenv("RSS_POLL_INTERVAL", 120))
GDELT_INTERVAL  = int(os.getenv("GDELT_POLL_INTERVAL", 300))
TWITTER_INTERVAL = int(os.getenv("TWITTER_POLL_INTERVAL", 180))

redis_client: aioredis.Redis = None
scheduler = AsyncIOScheduler()

# ── In-memory store (Redis-backed) ───────────────────────────────────────────

async def store_events(events: list[dict], prefix: str):
    """Store events in Redis with 12-hour TTL."""
    if not events:
        return
    pipe = redis_client.pipeline()
    for e in events:
        key = f"{prefix}:{e['id']}"
        pipe.setex(key, 43200, json.dumps(e, default=str))
    await pipe.execute()
    logger.info(f"Stored {len(events)} items under prefix '{prefix}'")


async def get_all_events(prefix: str) -> list[dict]:
    """Retrieve all events for a prefix from Redis."""
    keys = await redis_client.keys(f"{prefix}:*")
    if not keys:
        return []
    values = await redis_client.mget(keys)
    result = []
    for v in values:
        if v:
            try:
                result.append(json.loads(v))
            except Exception:
                pass
    return result


# ── Scraping jobs ─────────────────────────────────────────────────────────────

async def run_rss_job():
    try:
        events = await scrape_all_rss()
        await store_events(events, "event")
    except Exception as e:
        logger.error(f"RSS job error: {e}")


async def run_gdelt_job():
    try:
        events = await scrape_gdelt()
        await store_events(events, "event")
    except Exception as e:
        logger.error(f"GDELT job error: {e}")


async def run_twitter_job():
    try:
        tweets = await scrape_all_osint_accounts()
        await store_events(tweets, "tweet")
    except Exception as e:
        logger.error(f"Twitter job error: {e}")


# ── App lifecycle ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client
    redis_client = await aioredis.from_url(REDIS_URL, decode_responses=True)
    await init_db()

    # Schedule jobs
    scheduler.add_job(run_rss_job,     "interval", seconds=RSS_INTERVAL,     id="rss")
    scheduler.add_job(run_gdelt_job,   "interval", seconds=GDELT_INTERVAL,   id="gdelt")
    scheduler.add_job(run_twitter_job, "interval", seconds=TWITTER_INTERVAL, id="twitter")
    scheduler.start()

    # Initial scrape on startup
    logger.info("Running initial scrape...")
    await asyncio.gather(run_rss_job(), run_gdelt_job(), run_twitter_job())

    yield

    scheduler.shutdown()
    await redis_client.aclose()


app = FastAPI(
    title="OSINT News Monitor API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── API Routes ────────────────────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}


@app.get("/api/events")
async def get_events(
    category: Optional[str] = Query(None),
    topic: Optional[str] = Query(None),
    min_severity: int = Query(1, ge=1, le=5),
    hours: int = Query(6, ge=1, le=48),
    breaking_only: bool = Query(False),
    source_type: Optional[str] = Query(None),
):
    """
    Return geo-tagged news events for the map.
    Filterable by category, topic, severity, recency, source type.
    Topics: war, protests, christian_persecution, terrorism, natural_disasters
    """
    events = await get_all_events("event")
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    filtered = []
    for e in events:
        if not e.get("lat") or not e.get("lon"):
            continue
        try:
            pub = datetime.fromisoformat(e["published_at"].replace("Z", "+00:00"))
            if pub.tzinfo is None:
                pub = pub.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if pub < cutoff:
            continue
        if e.get("severity", 1) < min_severity:
            continue
        if category and e.get("category") != category:
            continue
        if topic and topic not in (e.get("topics") or []):
            continue
        if breaking_only and not e.get("is_breaking"):
            continue
        if source_type and e.get("source_type") != source_type:
            continue
        filtered.append(e)

    filtered.sort(key=lambda x: x.get("published_at", ""), reverse=True)
    return {"count": len(filtered), "events": filtered[:500]}


@app.get("/api/tweets")
async def get_tweets(
    hours: int = Query(3, ge=1, le=24),
    account: Optional[str] = Query(None),
    geo_only: bool = Query(False),
):
    """Return OSINT tweets feed."""
    tweets = await get_all_tweets()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    filtered = []
    for t in tweets:
        try:
            pub = datetime.fromisoformat(t["published_at"].replace("Z", "+00:00"))
            if pub.tzinfo is None:
                pub = pub.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if pub < cutoff:
            continue
        if account and t.get("account") != account:
            continue
        if geo_only and (not t.get("lat") or not t.get("lon")):
            continue
        filtered.append(t)

    filtered.sort(key=lambda x: x.get("published_at", ""), reverse=True)
    return {"count": len(filtered), "tweets": filtered[:200]}


async def get_all_tweets():
    return await get_all_events("tweet")


@app.get("/api/stats")
async def get_stats():
    """Dashboard stats summary."""
    events = await get_all_events("event")
    tweets = await get_all_events("tweet")

    categories = {}
    for e in events:
        cat = e.get("category", "general")
        categories[cat] = categories.get(cat, 0) + 1

    return {
        "total_events": len(events),
        "total_tweets": len(tweets),
        "by_category": categories,
        "breaking_count": sum(1 for e in events if e.get("is_breaking")),
        "high_severity": sum(1 for e in events if e.get("severity", 1) >= 4),
        "accounts_monitored": len(tweets and set(t["account"] for t in tweets)),
    }


@app.post("/api/refresh")
async def trigger_refresh():
    """Manually trigger a scrape cycle."""
    asyncio.create_task(run_rss_job())
    asyncio.create_task(run_gdelt_job())
    asyncio.create_task(run_twitter_job())
    return {"status": "refresh triggered"}


@app.get("/api/accounts")
async def get_accounts():
    """List monitored OSINT accounts."""
    from backend.scrapers.twitter_scraper import OSINT_ACCOUNTS
    tweets = await get_all_tweets()
    account_counts = {}
    for t in tweets:
        acc = t.get("account", "")
        account_counts[acc] = account_counts.get(acc, 0) + 1

    return {
        "accounts": [
            {"handle": acc, "tweet_count": account_counts.get(acc, 0)}
            for acc in OSINT_ACCOUNTS
        ]
    }
