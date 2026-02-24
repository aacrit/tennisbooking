# McFetridge Tennis Court Monitor

Monitors tennis court availability at McFetridge Sports Center (Chicago Park District) and sends WhatsApp notifications when slots open up.

## Architecture

Single Python process: FastAPI + APScheduler + Playwright (headless Chromium) + lightweight API poller.

```
Scheduler ──┬── Playwright scraper (full scan) ──┐
            └── API poller (lightweight HTTP)  ───┤
                                                  ├── Filter slots → WhatsApp notifier (Green API)
                                                  ├── SQLite DB
                                                  └── FastAPI dashboard
```

Two deployment modes:
- **GitHub Pages** (`scan_to_json.py`): One-shot scan via GitHub Actions → writes `docs/data/status.json` → static dashboard
- **Self-hosted** (`main.py`): Always-on process with scheduler, SQLite, and richer dashboard (Docker / Fly.io)

## Project Structure

```
main.py                  - Entry point, scheduler setup, scan orchestration
scan_to_json.py          - GitHub Actions entry point (one-shot scan → JSON)
config.py                - Pydantic Settings (all config via env vars / .env)
db.py                    - SQLite schema + async queries (aiosqlite)
scraper/checker.py       - Playwright browser automation (XHR interception + DOM scraping)
scraper/parser.py        - Time parsing, slot filtering (weekday 6PM+, weekends all)
scraper/api_poller.py    - Lightweight HTTP polling (replays captured API endpoints)
notifications/whatsapp.py - WhatsApp via Green API (free tier)
notifications/emailer.py - Gmail SMTP (disabled)
web/app.py               - FastAPI routes (dashboard, API, scan trigger)
web/templates/           - Jinja2 HTML templates
web/static/              - CSS
docs/                    - GitHub Pages static dashboard
.github/workflows/scan.yml - Scheduled GitHub Actions scraper
```

## Quick Start

```bash
# Local development
pip install -r requirements.txt
playwright install chromium
cp .env.example .env   # Fill in ActiveNet credentials + Green API keys
python main.py

# Docker
docker compose up --build

# Fly.io
fly launch --no-deploy
fly secrets set ACTIVENET_USERNAME=x ACTIVENET_PASSWORD=x GREEN_API_INSTANCE_ID=x GREEN_API_TOKEN=x WHATSAPP_CHAT_ID=x
fly volumes create tennis_data --region ord --size 1
fly deploy
```

## Configuration

All settings via environment variables (see `.env.example`):

| Variable | Default | Description |
|----------|---------|-------------|
| `ACTIVENET_USERNAME` | — | ActiveNet login (required for real availability) |
| `ACTIVENET_PASSWORD` | — | ActiveNet password |
| `GREEN_API_INSTANCE_ID` | — | Green API instance ID (WhatsApp) |
| `GREEN_API_TOKEN` | — | Green API token (WhatsApp) |
| `WHATSAPP_CHAT_ID` | — | Recipient(s) as `1XXXXXXXXXX@c.us` (comma-separated) |
| `API_POLL_ENABLED` | true | Enable lightweight HTTP polling between full scans |
| `API_POLL_PEAK_SECONDS` | 15 | Burst poll interval during 6:55-7:10 AM CT |
| `NOTIFY_COOLDOWN_SECONDS` | 60 | Don't re-notify same slot within this window |
| `WEEKDAY_EARLIEST_HOUR` | 18 | Only show weekday slots at or after this hour |
| `DAYS_AHEAD` | 6 | How many days to look ahead |
| `DEBUG_HEADED` | false | Run browser visibly for debugging |

## Scan Schedule (Central Time)

### Full Playwright Scans
| Window | Frequency |
|--------|-----------|
| 6-8 AM (peak) | Every 5 min |
| 8 AM - Midnight | Every 10 min |
| Midnight - 6 AM | Every 1 hour |

### Lightweight API Polls
| Window | Frequency |
|--------|-----------|
| 6:55-7:10 AM (burst) | Every 15 sec |
| 6-8 AM (peak) | Every 30 sec |
| 8 AM - Midnight | Every 2 min |

New slots drop daily at 7 AM CT.

## Scraper Notes

The booking portal (ActiveNet by ACTIVE Network) is a React SPA. Login is required — anonymous users see courts as unavailable. The scraper uses:
1. **XHR interception** (primary): Captures API responses as the SPA loads
2. **DOM scraping** (fallback): Reads rendered slot elements
3. **Text extraction** (last resort): Regex-based time extraction

Only `Tennis Ct01` through `Tennis Ct06` are accepted. If the scraper stops finding data, check `scraper/checker.py` selectors. Run with `DEBUG_HEADED=true` to debug.

## WhatsApp Notifications

Uses Green API (green-api.com) free Developer plan. Only newly opened prime-time slots trigger a message (weekday 6PM+ or weekends). Dedup is handled by the `current_slots` table in SQLite — scan-to-scan diff detects changes.

Setup: Sign up at green-api.com, create a free instance, scan QR to link WhatsApp, set the three `GREEN_API_*` / `WHATSAPP_CHAT_ID` env vars.
