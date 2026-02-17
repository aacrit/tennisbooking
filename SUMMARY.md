# McFetridge Tennis Court Monitor — System Overview

## What It Does

Automatically checks McFetridge Sports Center's online booking portal for available tennis courts and shows them on a live dashboard. Optionally sends email alerts when new slots open up.

**Live dashboard:** https://aacrit.github.io/tennisbooking/

---

## How It Works (Plain English)

The Chicago Park District uses a booking website (called ActiveNet) to manage court reservations. There's no way to get availability data directly — the website is a single-page app with no public data feed.

So this system launches an invisible web browser (Playwright/Chromium), visits the booking page, watches the network traffic for availability data, and extracts court times. It does this on a schedule, saves the results to a small JSON file, and a static webpage reads that file to show the dashboard.

```
  Every 45 min (GitHub Actions)
         |
         v
  Launch headless browser
         |
         v
  Visit ActiveNet booking portal
         |
         v
  Intercept network responses  -->  Extract court availability
         |
         v
  Filter: weekdays 6PM+, weekends all day, Tennis Ct 1-6 only
         |
         v
  Save to docs/data/status.json  -->  Git commit + push
         |
         v
  GitHub Pages serves updated dashboard
```

---

## Data Source

**Website:** Chicago Park District's ActiveNet portal (by ACTIVE Network)

The scraper tries three strategies in order:
1. **Network interception** — Captures the API responses the website makes internally as it loads
2. **Page structure reading** — Reads the rendered page elements for time slots
3. **Text extraction** — Last resort regex scan of page text

No login, API key, or account is needed. The portal is publicly accessible.

---

## Key Features

- **6-day lookahead** — Shows availability for the next 6 days
- **Smart scheduling** — Scans every 5 minutes at 7 AM (when new slots drop), every 45 minutes otherwise, stops overnight
- **Weekday filtering** — Only shows 6 PM+ slots on weekdays (after work hours)
- **Change tracking** — Shows which slots opened or closed since the last scan
- **"NEW" badges** — Highlights slots you haven't seen before
- **Browser notifications** — Optional alerts when new slots appear
- **Mobile-friendly** — Responsive grid layout, works as a home screen app
- **Dark mode** — Follows your system preference

---

## Services Used & Costs

| Service | What For | Cost |
|---------|----------|------|
| **GitHub Actions** | Runs the scraper on a schedule | Free (2,000 min/month for public repos) |
| **GitHub Pages** | Hosts the dashboard website | Free |
| **Playwright + Chromium** | Headless browser for scraping | Free (open source) |
| **Gmail SMTP** | Email alerts (currently disabled) | Free |
| **Fly.io** (optional) | Self-hosted deployment alternative | ~$5-10/month |

**Total running cost: $0** (current GitHub-based setup)

---

## Architecture

### GitHub Pages Mode (Current)

```
GitHub Actions (cron)
    └── scan_to_json.py
          ├── Playwright browser → visits ActiveNet portal
          ├── Extracts + filters available slots
          └── Writes docs/data/status.json
                └── Git commit → GitHub Pages auto-deploys
                      └── Static dashboard (vanilla HTML/JS/CSS)
```

Single file output. No server, no database, no backend.

### Self-Hosted Mode (Docker / Fly.io)

```
main.py (single Python process)
    ├── FastAPI web server (port 8080)
    ├── APScheduler (cron-like task runner)
    ├── Playwright scraper
    ├── SQLite database (scan history + dedup)
    └── Gmail SMTP emailer (optional)
```

All-in-one process. Includes a richer dashboard with scan history and a "Scan Now" button.

---

## Project Structure

```
scan_to_json.py          GitHub Actions entry point (one-shot scan → JSON)
main.py                  Self-hosted entry point (server + scheduler)
config.py                All settings (env vars / .env file)
db.py                    SQLite schema + queries

scraper/
  checker.py             Browser automation (XHR interception + DOM scraping)
  parser.py              Time parsing, slot filtering, court validation

notifications/
  emailer.py             Gmail SMTP with HTML email templates

web/
  app.py                 FastAPI routes (self-hosted dashboard)
  templates/             Jinja2 HTML templates
  static/                CSS

docs/                    GitHub Pages site
  index.html             Dashboard (vanilla JS, no framework)
  style.css              Responsive CSS with dark mode
  data/status.json       Scraped availability data (auto-updated)

.github/workflows/
  scan.yml               Scheduled scraper (every 45 min)
```

---

## Configuration

All settings are environment variables (set in `.env` or GitHub Actions):

| Setting | Default | What It Does |
|---------|---------|--------------|
| `WEEKDAY_EARLIEST_HOUR` | 18 (6 PM) | Only show weekday slots at or after this hour |
| `DAYS_AHEAD` | 6 | How many days to look ahead |
| `PEAK_INTERVAL_MINUTES` | 5 | Scan frequency during 6:50-8:00 AM CT |
| `NORMAL_INTERVAL_MINUTES` | 45 | Scan frequency 8:00 AM - midnight CT |
| `SMTP_USERNAME` | — | Gmail address (for email alerts) |
| `SMTP_PASSWORD` | — | Gmail App Password (for email alerts) |
| `DEBUG_HEADED` | false | Show the browser window for debugging |

---

## Scan Schedule (Central Time)

| Time | Frequency | Reason |
|------|-----------|--------|
| 6:50 - 8:00 AM | Every 5 min | New slots drop at 7 AM daily |
| 8:00 AM - Midnight | Every 45 min | Catch cancellations throughout the day |
| Midnight - 6:00 AM | No scanning | Nothing changes overnight |

---

## Developer Notes

- **If the scraper stops finding data:** The ActiveNet portal may have changed its HTML structure. Run with `DEBUG_HEADED=true` to see what the browser sees. Check `scraper/checker.py` selectors.
- **Court validation:** Only `Tennis Ct01` through `Tennis Ct06` are accepted. Anything without a valid court name is rejected to prevent false positives.
- **Notification dedup:** The system tracks `(date, time, court_name)` tuples so you never get duplicate alerts for the same slot. Old entries expire after 14 days.
- **No API keys needed:** The ActiveNet portal is public. No authentication or rate limiting has been observed.
- **GitHub Actions limits:** The workflow runs ~24 times/day (every 45 min, 18 hours). Each run uses ~2-3 minutes. Well within the free tier (~1,800 min/month).
