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
    level=logging.DEBUG if os.environ.get("SCRAPER_DEBUG") else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("scan_to_json")

CT = pytz.timezone("America/Chicago")
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "data")
OUT_PATH = os.path.join(OUT_DIR, "status.json")


def _is_prime_time_str(time_str: str, date_str: str, settings: Settings) -> bool:
    """Check if a slot is prime time from string representations."""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        if d.weekday() >= 5:
            return True
        t = datetime.strptime(time_str.strip(), "%I:%M %p").time()
        return t.hour >= settings.weekday_earliest_hour
    except (ValueError, AttributeError):
        return False


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
        is_weekend = d.weekday() >= 5
        day_slots = grouped.get(d_str, [])
        calendar.append({
            "date": d_str,
            "date_display": d.strftime("%b %d"),
            "day_name": d.strftime("%a"),
            "is_weekend": is_weekend,
            "slots": [
                {
                    "slot_time": s["time"],
                    "court_name": s.get("court_name", ""),
                    "is_prime_time": s.get("is_prime_time", is_weekend),
                }
                for s in day_slots
            ],
        })
    return calendar


def compute_changes(old_status: dict, new_calendar: list[dict], now_ct: str) -> dict:
    """Compare previous scan's slots with current to detect opens/closes."""
    old_set = set()
    for day in old_status.get("calendar", []):
        for s in day.get("slots", []):
            old_set.add((day["date"], s["slot_time"], s.get("court_name", "")))

    new_set = set()
    for day in new_calendar:
        for s in day.get("slots", []):
            new_set.add((day["date"], s["slot_time"], s.get("court_name", "")))

    opened = sorted(
        [{"date": d, "time": t, "court_name": c, "detected_at": now_ct}
         for d, t, c in (new_set - old_set)],
        key=lambda x: (x["date"], x["time"]),
    )
    closed = sorted(
        [{"date": d, "time": t, "court_name": c, "last_seen": old_status.get("last_scan_time", "")}
         for d, t, c in (old_set - new_set)],
        key=lambda x: (x["date"], x["time"]),
    )

    return {
        "since": old_status.get("last_scan_time", ""),
        "opened": opened,
        "closed": closed,
    }


def load_previous_status() -> dict:
    """Load the previous status.json for change tracking."""
    if os.path.exists(OUT_PATH):
        try:
            with open(OUT_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def write_json(data: dict):
    """Write JSON to docs/data/status.json, creating directories as needed."""
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(data, f, indent=2)
    logger.info("Wrote %s", OUT_PATH)


async def main():
    settings = Settings()
    logger.info("Starting one-shot availability scan...")

    old_status = load_previous_status()

    diag_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "diagnostics")
    try:
        checker = AvailabilityChecker(settings, diag_dir=diag_dir)
        raw_slots = await checker.check_availability()
        filtered = filter_slots(raw_slots, settings)
    except Exception as e:
        logger.exception("Scan failed: %s", e)
        now_ct = datetime.now(CT).strftime("%Y-%m-%d %H:%M:%S CST")
        calendar = build_calendar([])
        write_json({
            "last_scan_time": now_ct,
            "last_scan_success": False,
            "slots_found": 0,
            "calendar": calendar,
            "total_slots": 0,
            "changes": compute_changes(old_status, calendar, now_ct),
        })
        # Don't sys.exit(1) — let the workflow commit the failure status
        # so the dashboard shows when the last attempt was made
        return

    now_ct = datetime.now(CT).strftime("%Y-%m-%d %H:%M:%S CST")
    calendar = build_calendar(filtered)
    total_slots = sum(len(day["slots"]) for day in calendar)
    changes = compute_changes(old_status, calendar, now_ct)

    write_json({
        "last_scan_time": now_ct,
        "last_scan_success": True,
        "slots_found": len(filtered),
        "calendar": calendar,
        "total_slots": total_slots,
        "changes": changes,
    })

    # Send WhatsApp notification for newly opened slots
    instance_id = os.environ.get("GREEN_API_INSTANCE_ID", "")
    api_token = os.environ.get("GREEN_API_TOKEN", "")
    chat_ids_raw = os.environ.get("WHATSAPP_CHAT_ID", "")
    chat_ids = [cid.strip() for cid in chat_ids_raw.split(",") if cid.strip()]
    wa_configured = bool(instance_id and api_token and chat_ids)

    opened = changes.get("opened", [])
    # Only notify for prime-time slots (weekday 6PM+ or weekends)
    prime_opened = [
        s for s in opened
        if _is_prime_time_str(s.get("time", ""), s.get("date", ""), settings)
    ]
    if prime_opened:
        if wa_configured:
            from notifications.whatsapp import send_whatsapp, format_slots_message
            msg = format_slots_message(prime_opened)
            for chat_id in chat_ids:
                ok = send_whatsapp(instance_id, api_token, chat_id, msg)
                logger.info("WhatsApp sent=%s to %s for %d prime-time opened slots (of %d total)", ok, chat_id, len(prime_opened), len(opened))
        else:
            logger.warning("WhatsApp NOT configured — skipping notification for %d prime-time opened slots", len(prime_opened))
    elif opened:
        logger.info("Skipping WhatsApp: %d opened slots are off-peak only", len(opened))

    opened_count = len(changes.get("opened", []))
    closed_count = len(changes.get("closed", []))
    logger.info(
        "DIAGNOSTIC: raw=%d filtered=%d opened=%d closed=%d whatsapp_configured=%s",
        len(raw_slots), len(filtered), opened_count, closed_count, wa_configured,
    )
    logger.info(
        "Done: %d slots across %d days (changes: +%d opened, -%d closed)",
        total_slots, len(calendar), opened_count, closed_count,
    )

    # Save API context for potential use by fast_scan.py
    api_context = checker.get_api_context()
    api_context_path = os.path.join(OUT_DIR, "api_context.json")
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(api_context_path, "w") as f:
        json.dump(api_context, f, indent=2)
    logger.info("Saved API context: %d endpoints", len(api_context.get("endpoints", [])))


def send_test_whatsapp():
    """Send a test WhatsApp message to verify Green API credentials."""
    from notifications.whatsapp import send_whatsapp, format_slots_message

    instance_id = os.environ.get("GREEN_API_INSTANCE_ID", "")
    api_token = os.environ.get("GREEN_API_TOKEN", "")
    chat_ids_raw = os.environ.get("WHATSAPP_CHAT_ID", "")
    chat_ids = [cid.strip() for cid in chat_ids_raw.split(",") if cid.strip()]

    if not all([instance_id, api_token, chat_ids]):
        logger.error(
            "Cannot send test: missing GREEN_API_INSTANCE_ID, GREEN_API_TOKEN, "
            "or WHATSAPP_CHAT_ID environment variables"
        )
        sys.exit(1)

    mock_slots = [
        {"date": "2026-02-25", "time": "6:00 PM", "court_name": "Tennis Ct 1",
         "detected_at": datetime.now(CT).strftime("%Y-%m-%d %H:%M:%S CST")},
        {"date": "2026-02-25", "time": "7:00 PM", "court_name": "Tennis Ct 3",
         "detected_at": datetime.now(CT).strftime("%Y-%m-%d %H:%M:%S CST")},
    ]

    msg = format_slots_message(mock_slots)
    for chat_id in chat_ids:
        logger.info("Sending test WhatsApp message to %s...", chat_id)
        ok = send_whatsapp(instance_id, api_token, chat_id, msg)
        if ok:
            logger.info("Test message sent to %s successfully!", chat_id)
        else:
            logger.error("Test message to %s FAILED — check credentials and logs above", chat_id)
            sys.exit(1)


if __name__ == "__main__":
    if "--test-whatsapp" in sys.argv:
        send_test_whatsapp()
    else:
        asyncio.run(main())
