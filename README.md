# OSINT News Monitor — Global Intelligence Map

A real-time world map that aggregates breaking news from RSS feeds, GDELT, 
and OSINT X/Twitter accounts. Events are geo-tagged and displayed on an 
interactive dark-mode map with live filtering.

---

## Architecture

```
[RSS Feeds]  [GDELT API]  [X API / Nitter]
      ↓            ↓             ↓
   [FastAPI Backend + APScheduler]
              ↓
          [Redis]  ←→  [PostgreSQL]
              ↓
       [Nginx + Frontend]
```

---

## Quick Start (VPS with Docker)

### 1. Prerequisites

```bash
# On Ubuntu 22.04 / Debian 12
sudo apt update
sudo apt install -y docker.io docker-compose-plugin git curl
sudo systemctl enable --now docker
```

### 2. Clone / Upload project

```bash
git clone https://github.com/you/osint-map.git
cd osint-map

# OR upload the project zip and unzip it
unzip osint-map.zip
cd osint-map
```

### 3. Configure environment

```bash
cp backend/.env.example backend/.env
nano backend/.env
```

Edit the following values:
```env
# REQUIRED: change database password
DB_PASSWORD=your_strong_password_here

# OPTIONAL: add X API key for primary Twitter access
TWITTER_BEARER_TOKEN=your_bearer_token_here

# OPTIONAL: add your own OSINT accounts (comma-separated, no @ symbol)
OSINT_ACCOUNTS=sentdefender,IntelCrab,OSINTdefender,RALee85,GeoConfirmed,Conflicts

# Set your domain for CORS
CORS_ORIGINS=https://yourdomain.com,http://localhost:3000
```

### 4. Update frontend API URL

Open `frontend/index.html` and find this line near the top of the `<script>` section:

```javascript
const API_BASE = window.location.hostname === 'localhost' ...
```

This auto-detects local vs production. No changes needed if you use nginx on same domain.

### 5. Launch

```bash
docker compose up -d --build
```

This starts: PostgreSQL, Redis, FastAPI backend, Nginx frontend.

Check logs:
```bash
docker compose logs -f api      # backend logs
docker compose logs -f nginx    # frontend logs
```

### 6. SSL with Let's Encrypt (recommended)

```bash
sudo apt install certbot
sudo certbot certonly --standalone -d yourdomain.com

# Then copy the VPS nginx config
sudo cp deployment/nginx.conf /etc/nginx/sites-available/osint-map
sudo ln -s /etc/nginx/sites-available/osint-map /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

---

## Manual (No Docker) Setup

### Backend

```bash
cd backend
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python -m spacy download en_core_web_sm

# Copy and edit env
cp .env.example .env

# Start
uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload
```

### Frontend

Just serve the `frontend/` folder with any static file server:

```bash
# Python (quick test)
cd frontend && python -m http.server 3000

# Or copy to nginx webroot
sudo cp -r frontend/* /var/www/html/
```

---

## Adding Your X Accounts

Edit `backend/.env`:

```env
OSINT_ACCOUNTS=sentdefender,IntelCrab,OSINTdefender,RALee85,GeoConfirmed,\
Conflicts,WarMonitors,Intel_Sky,Osinttechnical,Tendar,Archer83Actual,\
AA_Battlespace,OSINT_Tactical,Hajii_MilSpec,MilOSINT
```

No @ symbol, comma-separated. Restart the backend:

```bash
docker compose restart api
```

---

## Getting a Twitter/X API Key (optional but recommended)

Nitter scraping works without keys but is less reliable.  
For more stable access:

1. Go to https://developer.x.com
2. Create a project → "Read-only" access is sufficient
3. Copy your **Bearer Token**
4. Add to `backend/.env` as `TWITTER_BEARER_TOKEN=...`

Free tier gives 500,000 tweet reads/month — enough for ~15 accounts polled every 3 minutes.

---

## API Reference

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/health` | GET | Health check |
| `/api/events` | GET | News events (filterable) |
| `/api/tweets` | GET | OSINT tweets |
| `/api/stats` | GET | Dashboard statistics |
| `/api/accounts` | GET | Monitored accounts + counts |
| `/api/refresh` | POST | Trigger manual scrape |

### Event filters

```
GET /api/events?category=conflict&min_severity=3&hours=6&breaking_only=true
```

---

## Customizing RSS Sources

Edit `backend/scrapers/news_scraper.py` — the `RSS_FEEDS` list at the top.  
Add any RSS feed URL with a `source` label. Geo-tagging is automatic.

---

## Troubleshooting

**Events not showing on map:**  
→ Most events need location info to be extracted. Check logs: `docker compose logs api`

**Nitter not working:**  
→ Nitter instances go offline frequently. Add more in `.env` NITTER_INSTANCES

**High memory usage:**  
→ Redis is capped at 256MB by default. Reduce event retention in `store_events()` TTL (currently 12h)

**Geo-tagging slow:**  
→ spaCy NER on startup is normal. Geocoding is rate-limited to 1 req/sec via Nominatim's ToS.

---

## Project Structure

```
osint-map/
├── backend/
│   ├── api/
│   │   └── main.py              # FastAPI app, routes, scheduler
│   ├── scrapers/
│   │   ├── news_scraper.py      # RSS + GDELT
│   │   └── twitter_scraper.py   # X API + Nitter fallback
│   ├── processors/
│   │   └── geo_tagger.py        # NER, geocoding, classification
│   ├── database.py              # SQLAlchemy models
│   ├── requirements.txt
│   ├── Dockerfile
│   └── .env.example
├── frontend/
│   └── index.html               # Full single-file map app
├── deployment/
│   ├── nginx.conf               # VPS nginx (with SSL)
│   └── nginx-docker.conf        # Docker nginx (no SSL)
├── docker-compose.yml
└── README.md
```
