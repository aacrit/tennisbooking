# McFetridge Tennis Court Monitor

Monitors tennis court availability at McFetridge Sports Center (Chicago Park District) and sends WhatsApp notifications when slots open up.

## Quick Start

```bash
# 1. Copy and fill in environment variables
cp .env.example .env
# Edit .env with your Gmail App Password and other settings

# 2. Run with Docker
docker compose up -d

# 3. Open dashboard
open http://localhost:8080
```

## Architecture

Single Python process: FastAPI + APScheduler + Playwright (headless Chromium)

```
Scheduler → Playwright scraper → Filter slots → WhatsApp notifier (Green API)
                                       ↓
                                   SQLite DB
                                       ↓
                              FastAPI dashboard
```

## Project Structure

```
main.py              - Entry point, scheduler setup, scan orchestration
config.py            - Pydantic Settings (all config via env vars / .env)
db.py                - SQLite schema + async queries (aiosqlite)
scraper/checker.py   - Playwright browser automation (XHR interception + DOM scraping)
scraper/parser.py    - Time parsing, slot filtering (weekday 6PM+, weekends all)
notifications/whatsapp.py - WhatsApp via Green API (free tier)
notifications/emailer.py - Gmail SMTP email with HTML templates (disabled)
web/app.py           - FastAPI routes (dashboard, API, scan trigger)
web/templates/       - Jinja2 HTML templates
web/static/          - CSS
```

## Key Commands

```bash
# Local development
pip install -r requirements.txt
playwright install chromium
python main.py

# Docker
docker compose up --build
docker compose logs -f

# Fly.io deployment
fly launch --no-deploy
fly secrets set SMTP_USERNAME=you@gmail.com SMTP_PASSWORD=xxxx FROM_EMAIL=you@gmail.com
fly volumes create tennis_data --region ord --size 1
fly deploy
```

## Configuration

All settings via environment variables (see `.env.example`):

| Variable | Default | Description |
|----------|---------|-------------|
| `GREEN_API_INSTANCE_ID` | — | Green API instance ID |
| `GREEN_API_TOKEN` | — | Green API token |
| `WHATSAPP_CHAT_ID` | — | Recipient phone as `1XXXXXXXXXX@c.us` |
| `WEEKDAY_EARLIEST_HOUR` | 18 | Only slots >= this hour on weekdays |
| `DAYS_AHEAD` | 6 | How many days to look ahead |
| `PEAK_INTERVAL_MINUTES` | 5 | Scan interval 6:50-8AM CT |
| `NORMAL_INTERVAL_MINUTES` | 45 | Scan interval 8AM-midnight CT |
| `DEBUG_HEADED` | false | Run browser visibly for debugging |

## Scan Schedule (Central Time)

- **6:50 AM - 8:00 AM**: Every 5 min (new slots drop at 7 AM)
- **8:00 AM - 11:59 PM**: Every 45 min (catch cancellations)
- **Midnight - 6:00 AM**: No scanning

## Scraper Notes

The booking portal (ActiveNet by ACTIVE Network) is a React SPA with no public API. The scraper uses:
1. **XHR interception** (primary): Captures API responses as the SPA loads availability
2. **DOM scraping** (fallback): Reads rendered slot elements from the page
3. **Text extraction** (last resort): Regex-based time extraction from page text

If the scraper stops finding data (site changed), check `scraper/checker.py` selectors. Run with `DEBUG_HEADED=true` to see the browser in action.

## WhatsApp Notifications

Uses Green API (green-api.com) free Developer plan to send WhatsApp messages when new slots open. Only `changes.opened` slots (new since last scan) trigger a message. No database needed — dedup is handled by the scan-to-scan diff in `scan_to_json.py`.

Setup: Sign up at green-api.com, create a free instance, scan QR to link WhatsApp, then set `GREEN_API_INSTANCE_ID`, `GREEN_API_TOKEN`, and `WHATSAPP_CHAT_ID` as GitHub Actions secrets.
