#!/usr/bin/env python3
"""
One-shot scraper that writes availability data to docs/data/status.json.
Used by GitHub Actions to update the static GitHub Pages dashboard.

Usage:
    python scan_to_json.py
"""
import asyncio
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta

import pytz

from config import Settings
from scraper.checker import AvailabilityChecker
from scraper.parser import filter_slots

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("scan_to_json")

CT = pytz.timezone("America/Chicago")
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "data")
OUT_PATH = os.path.join(OUT_DIR, "status.json")


def build_calendar(filtered_slots: list[dict]) -> list[dict]:
    """Build 6-day calendar from filtered slots (mirrors web/app.py _build_calendar)."""
    today = date.today()
    grouped: dict[str, list[dict]] = {}
    for slot in filtered_slots:
        grouped.setdefault(slot["date"], []).append(slot)

    calendar = []
    for i in range(1, 7):
        d = today + timedelta(days=i)
        d_str = d.isoformat()
        day_slots = grouped.get(d_str, [])
        calendar.append({
            "date": d_str,
            "date_display": d.strftime("%b %d"),
            "day_name": d.strftime("%a"),
            "is_weekend": d.weekday() >= 5,
            "slots": [
                {"slot_time": s["time"], "court_name": s.get("court_name", "")}
                for s in day_slots
            ],
        })
    return calendar


def write_json(data: dict):
    """Write JSON to docs/data/status.json, creating directories as needed."""
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(data, f, indent=2)
    logger.info("Wrote %s", OUT_PATH)


async def main():
    settings = Settings()
    logger.info("Starting one-shot availability scan...")

    try:
        checker = AvailabilityChecker(settings)
        raw_slots = await checker.check_availability()
        filtered = filter_slots(raw_slots, settings)
    except Exception as e:
        logger.exception("Scan failed: %s", e)
        now_ct = datetime.now(CT).strftime("%Y-%m-%d %H:%M:%S CT")
        write_json({
            "last_scan_time": now_ct,
            "last_scan_success": False,
            "slots_found": 0,
            "calendar": build_calendar([]),
            "total_slots": 0,
        })
        sys.exit(1)

    now_ct = datetime.now(CT).strftime("%Y-%m-%d %H:%M:%S CT")
    calendar = build_calendar(filtered)
    total_slots = sum(len(day["slots"]) for day in calendar)

    write_json({
        "last_scan_time": now_ct,
        "last_scan_success": True,
        "slots_found": len(filtered),
        "calendar": calendar,
        "total_slots": total_slots,
    })

    logger.info("Done: %d slots across %d days", total_slots, len(calendar))


if __name__ == "__main__":
    asyncio.run(main())
