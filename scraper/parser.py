"""
Parse and filter raw availability data by user time preferences.

Weekdays (Mon-Fri): Only slots at or after 6 PM
Weekends (Sat-Sun): All times allowed
"""
import re
import logging
from datetime import date, time, datetime

from config import Settings

logger = logging.getLogger(__name__)

# Only allow McFetridge Tennis courts 1-6 (exclude pickleball, ball machine, etc.)
ALLOWED_COURTS_RE = re.compile(
    r'(McFetridge\s+)?Tennis\s+(Ct|Court)\s*0?[1-6]\b',
    re.IGNORECASE,
)


def parse_time_string(time_str: str) -> time | None:
    """Parse various time formats into a time object."""
    if not time_str:
        return None

    time_str = time_str.strip()

    # Handle HH:MM format (24-hour)
    match = re.match(r'^(\d{1,2}):(\d{2})$', time_str)
    if match:
        h, m = int(match.group(1)), int(match.group(2))
        if 0 <= h <= 23 and 0 <= m <= 59:
            return time(h, m)

    # Handle HH:MM AM/PM format
    match = re.match(r'^(\d{1,2}):(\d{2})\s*(AM|PM|am|pm)$', time_str)
    if match:
        h, m = int(match.group(1)), int(match.group(2))
        ampm = match.group(3).upper()
        if ampm == "PM" and h != 12:
            h += 12
        elif ampm == "AM" and h == 12:
            h = 0
        if 0 <= h <= 23 and 0 <= m <= 59:
            return time(h, m)

    # Handle H AM/PM (no minutes)
    match = re.match(r'^(\d{1,2})\s*(AM|PM|am|pm)$', time_str)
    if match:
        h = int(match.group(1))
        ampm = match.group(2).upper()
        if ampm == "PM" and h != 12:
            h += 12
        elif ampm == "AM" and h == 12:
            h = 0
        if 0 <= h <= 23:
            return time(h, 0)

    return None


def parse_date_string(date_str: str) -> date | None:
    """Parse various date formats into a date object."""
    if not date_str:
        return None

    date_str = date_str.strip()

    formats = [
        "%Y-%m-%d",     # 2024-01-15
        "%m/%d/%Y",     # 01/15/2024
        "%m/%d/%y",     # 01/15/24
        "%B %d, %Y",   # January 15, 2024
        "%b %d, %Y",   # Jan 15, 2024
    ]

    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue

    return None


def filter_slots(raw_slots: list[dict], settings: Settings) -> list[dict]:
    """
    Filter availability slots based on user preferences:
    - Weekdays (Mon-Fri): only slots at or after weekday_earliest_hour
    - Weekends (Sat-Sun): all slots OK
    - Only future dates within the lookahead window
    """
    today = date.today()
    filtered = []
    seen = set()

    for slot in raw_slots:
        slot_date = parse_date_string(slot.get("date", ""))
        slot_time = parse_time_string(slot.get("time", ""))

        if slot_date is None:
            logger.debug("Skipping slot with unparseable date: %s", slot.get("date"))
            continue

        # Must be in the future and within lookahead window
        if slot_date <= today:
            continue
        if (slot_date - today).days > settings.days_ahead:
            continue

        if slot_time is None:
            logger.debug("Skipping slot with unparseable time: %s", slot.get("time"))
            continue

        # Apply day-of-week filter
        day_of_week = slot_date.weekday()  # 0=Monday, 6=Sunday
        is_weekend = day_of_week >= 5

        if not is_weekend and slot_time.hour < settings.weekday_earliest_hour:
            continue

        # Court name filter: only Ct01-Ct06, skip pickleball/ball machine
        court_name = slot.get("court_name", "").strip()
        if court_name and not ALLOWED_COURTS_RE.search(court_name):
            continue

        # Dedup by (date, time, court_name)
        key = (slot_date.isoformat(), slot_time.strftime("%H:%M"), slot.get("court_name", ""))
        if key in seen:
            continue
        seen.add(key)

        filtered.append({
            "date": slot_date.isoformat(),
            "time": slot_time.strftime("%I:%M %p"),
            "time_24h": slot_time.strftime("%H:%M"),
            "court_name": slot.get("court_name", ""),
            "day_of_week": slot_date.strftime("%A"),
            "is_weekend": is_weekend,
            "duration_minutes": slot.get("duration_minutes", 60),
        })

    # Sort by date then time
    filtered.sort(key=lambda s: (s["date"], s["time_24h"]))

    logger.info("Filtered %d slots from %d raw entries", len(filtered), len(raw_slots))
    return filtered
