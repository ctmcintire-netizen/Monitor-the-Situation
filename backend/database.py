from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy import Column, String, Float, DateTime, Text, JSON, Integer, Boolean
from datetime import datetime
import os

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://osint:osint@localhost:5432/osintmap")

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Event(Base):
    __tablename__ = "events"

    id = Column(String, primary_key=True)  # hash of url+title
    title = Column(String, nullable=False)
    summary = Column(Text)
    url = Column(String)
    source = Column(String)          # e.g. "reuters", "twitter:OSINTdefender"
    source_type = Column(String)     # "rss" | "gdelt" | "twitter" | "nitter"
    category = Column(String)        # "conflict" | "disaster" | "politics" | "breaking"
    lat = Column(Float)
    lon = Column(Float)
    location_name = Column(String)
    country_code = Column(String)
    severity = Column(Integer, default=1)   # 1-5
    media_urls = Column(JSON, default=list) # images/video links
    raw_tags = Column(JSON, default=list)
    published_at = Column(DateTime, default=datetime.utcnow)
    fetched_at = Column(DateTime, default=datetime.utcnow)
    is_breaking = Column(Boolean, default=False)


class OSINTTweet(Base):
    __tablename__ = "osint_tweets"

    id = Column(String, primary_key=True)
    account = Column(String)
    text = Column(Text)
    url = Column(String)
    media_urls = Column(JSON, default=list)
    lat = Column(Float)
    lon = Column(Float)
    location_name = Column(String)
    hashtags = Column(JSON, default=list)
    linked_event_id = Column(String)   # FK to events if matched
    published_at = Column(DateTime, default=datetime.utcnow)
    fetched_at = Column(DateTime, default=datetime.utcnow)
    source_method = Column(String)     # "twitter_api" | "nitter"


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
