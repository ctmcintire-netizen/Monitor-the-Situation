"""
Geo-tagging engine: extracts locations from text and resolves to lat/lon.
Uses spaCy NER with a geopy fallback, plus a fast regex pass for coordinates.
"""
import re
import hashlib
import asyncio
from functools import lru_cache
from typing import Optional
from geopy.geocoders import Nominatim
from geopy.extra.rate_limiter import RateLimiter
from loguru import logger

# ── Coordinate patterns ──────────────────────────────────────────────────────
_DECIMAL_COORD = re.compile(
    r'(-?\d{1,3}\.\d+)\s*[,/]\s*(-?\d{1,3}\.\d+)'
)
_DMS_COORD = re.compile(
    r'(\d{1,3})°(\d{1,2})′([NS])\s+(\d{1,3})°(\d{1,2})′([EW])'
)

# ── Category keywords ────────────────────────────────────────────────────────
CATEGORY_KEYWORDS = {
    "conflict":  ["attack", "airstrike", "explosion", "missile", "troops", "battle",
                  "war", "military", "drone", "shelling", "killed", "wounded", "gun",
                  "bomb", "forces", "offensive", "ceasefire", "strike"],
    "disaster":  ["earthquake", "flood", "hurricane", "tornado", "wildfire", "tsunami",
                  "eruption", "cyclone", "storm", "landslide", "drought", "fire"],
    "politics":  ["election", "coup", "protest", "president", "minister", "government",
                  "parliament", "sanctions", "treaty", "vote", "arrested"],
    "breaking":  ["breaking", "urgent", "alert", "developing", "just in", "update"],
}

# ── Topic taxonomy (more granular, layered on top of category) ────────────────
TOPIC_KEYWORDS = {
    "war": [
        "war", "warfare", "invasion", "offensive", "airstrike", "shelling", "artillery",
        "troops", "military operation", "front line", "frontline", "combat", "battle",
        "siege", "ceasefire", "missile strike", "drone strike", "armed forces",
        "warzone", "war zone", "casualties", "soldiers killed", "naval", "ground forces",
        "occupation", "liberated", "captured territory", "counter-offensive"
    ],
    "protests": [
        "protest", "riot", "demonstration", "unrest", "uprising", "marching", "marchers",
        "demonstrators", "crowd", "clashes with police", "tear gas", "water cannon",
        "civil unrest", "strike", "walkout", "blockade", "occupation", "mob",
        "looting", "burning", "barricade", "crackdown", "dispersed", "detained protesters",
        "activists arrested", "rallied", "rally", "picket", "insurrection"
    ],
    "christian_persecution": [
        "christian", "christianity", "church", "cathedral", "pastor", "priest", "bishop",
        "missionary", "cross", "bible", "congregation", "evangelist", "christian minority",
        "religious persecution", "faith", "worship", "sunday service", "christian convert",
        "church attack", "church burned", "church destroyed", "blasphemy", "apostasy",
        "religious freedom", "christian arrested", "christian killed", "anti-christian",
        "christian community", "diocese", "monastery", "convent", "christian school"
    ],
    "terrorism": [
        "terrorist", "terrorism", "terror attack", "suicide bomber", "suicide bombing",
        "ied", "improvised explosive", "jihadist", "isis", "isil", "al-qaeda", "al qaeda",
        "boko haram", "al-shabaab", "taliban", "lone wolf", "radicalized", "extremist",
        "car bomb", "vehicle attack", "knife attack", "mass shooting", "hostage",
        "kidnapping", "abduction", "ransom", "beheading", "massacre", "gunman",
        "active shooter", "claimed responsibility", "terror cell", "bomb threat",
        "explosive device", "detonated", "attack on civilians"
    ],
    "natural_disasters": [
        "earthquake", "magnitude", "tremor", "aftershock", "seismic",
        "flood", "flooding", "flash flood", "dam breach", "levee",
        "hurricane", "typhoon", "cyclone", "tropical storm", "category",
        "tornado", "twister", "funnel cloud",
        "wildfire", "forest fire", "bushfire", "blaze",
        "tsunami", "tidal wave",
        "volcanic eruption", "volcano", "lava", "ash cloud",
        "landslide", "mudslide", "avalanche",
        "drought", "famine", "heatwave", "heat wave",
        "storm surge", "blizzard", "extreme weather", "disaster zone",
        "emergency declared", "natural disaster", "evacuated", "death toll"
    ],
}

TOPIC_DISPLAY = {
    "war":                  "⚔ War",
    "protests":             "✊ Protests & Riots",
    "christian_persecution":"✝ Christian Persecution",
    "terrorism":            "💥 Terrorism",
    "natural_disasters":    "🌊 Natural Disasters",
}


def classify_topics(text: str) -> list[str]:
    """Return all matching topics for a piece of text (can match multiple)."""
    text_lower = text.lower()
    matched = []
    for topic, keywords in TOPIC_KEYWORDS.items():
        for kw in keywords:
            if kw in text_lower:
                matched.append(topic)
                break
    return matched

SEVERITY_KEYWORDS = {
    5: ["mass casualty", "nuclear", "catastrophic", "major offensive", "capital seized"],
    4: ["dozens killed", "city under attack", "state of emergency", "coup"],
    3: ["casualties", "explosion", "airstrike", "protest", "arrested"],
    2: ["clashes", "tensions", "evacuation", "warning"],
    1: [],
}

geolocator = Nominatim(user_agent="osint-news-monitor/1.0")
geocode = RateLimiter(geolocator.geocode, min_delay_seconds=1, max_retries=2)


def extract_decimal_coords(text: str) -> Optional[tuple[float, float]]:
    m = _DECIMAL_COORD.search(text)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return lat, lon
    return None


def extract_dms_coords(text: str) -> Optional[tuple[float, float]]:
    m = _DMS_COORD.search(text)
    if not m:
        return None
    lat = int(m.group(1)) + int(m.group(2)) / 60
    if m.group(3) == 'S':
        lat = -lat
    lon = int(m.group(4)) + int(m.group(5)) / 60
    if m.group(6) == 'W':
        lon = -lon
    return lat, lon


@lru_cache(maxsize=2048)
def geocode_cached(location: str) -> Optional[tuple[float, float, str]]:
    """Returns (lat, lon, display_name) or None. Cached to avoid hammering Nominatim."""
    try:
        result = geocode(location, exactly_one=True, timeout=5)
        if result:
            return result.latitude, result.longitude, result.address
    except Exception as e:
        logger.warning(f"Geocode failed for '{location}': {e}")
    return None


def extract_locations_spacy(text: str) -> list[str]:
    """Extract GPE/LOC entities. Lazy-load spaCy to avoid startup delay."""
    try:
        import spacy
        nlp = _get_nlp()
        doc = nlp(text[:1000])  # limit for speed
        return list({ent.text for ent in doc.ents if ent.label_ in ("GPE", "LOC")})
    except Exception:
        return []


_nlp_model = None

def _get_nlp():
    global _nlp_model
    if _nlp_model is None:
        import spacy
        try:
            _nlp_model = spacy.load("en_core_web_sm")
        except OSError:
            from spacy.cli import download
            download("en_core_web_sm")
            _nlp_model = spacy.load("en_core_web_sm")
    return _nlp_model


def classify_category(text: str) -> str:
    text_lower = text.lower()
    # Check breaking first
    for kw in CATEGORY_KEYWORDS["breaking"]:
        if kw in text_lower:
            # Still check for more specific category
            for cat in ["conflict", "disaster", "politics"]:
                for kw2 in CATEGORY_KEYWORDS[cat]:
                    if kw2 in text_lower:
                        return cat
            return "breaking"
    for cat in ["conflict", "disaster", "politics"]:
        for kw in CATEGORY_KEYWORDS[cat]:
            if kw in text_lower:
                return cat
    return "general"


def classify_severity(text: str) -> int:
    text_lower = text.lower()
    for level in [5, 4, 3, 2]:
        for kw in SEVERITY_KEYWORDS[level]:
            if kw in text_lower:
                return level
    return 1


def is_breaking(text: str) -> bool:
    text_lower = text.lower()
    return any(kw in text_lower for kw in CATEGORY_KEYWORDS["breaking"])


def resolve_geo(text: str, hint_locations: list[str] = None) -> dict:
    """
    Full geo-resolution pipeline:
    1. Try to extract raw coordinates from text
    2. Try spaCy NER → geocode
    3. Try hint_locations
    Returns dict with lat, lon, location_name, country_code or empty dict.
    """
    # Step 1: raw coords
    coords = extract_decimal_coords(text) or extract_dms_coords(text)
    if coords:
        return {"lat": coords[0], "lon": coords[1], "location_name": "", "country_code": ""}

    # Step 2: spaCy
    locations = extract_locations_spacy(text)
    if hint_locations:
        locations = hint_locations + locations

    for loc in locations:
        result = geocode_cached(loc)
        if result:
            lat, lon, display = result
            parts = display.split(",")
            country = parts[-1].strip() if parts else ""
            return {
                "lat": lat,
                "lon": lon,
                "location_name": loc,
                "country_code": country[:2].upper() if len(country) >= 2 else "",
            }

    return {}


def make_event_id(url: str, title: str) -> str:
    return hashlib.sha256(f"{url}{title}".encode()).hexdigest()[:16]
