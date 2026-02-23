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

# Allow McFetridge Tennis courts 1-6 and common name variations from the scraper
# Matches: "Tennis Ct 1", "McFetridge Tennis Ct 1", "McFetridge Tennis Court 3",
#           "McFetridge Tennis Ct 1 Court Time", etc.
ALLOWED_COURTS_RE = re.compile(
    r'(?:McFetridge\s+)?Tennis\s+(?:Ct|Court)\s*0?[1-6]\b',
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


def _filter_slots_base(raw_slots: list[dict], settings: Settings,
                       court_filter_fn) -> list[dict]:
    """
    Shared slot filtering with pluggable court name filter.

    Applies:
    - Date validation (future, within lookahead window)
    - Time parsing and weekday/weekend hour filter
    - court_filter_fn(court_name: str) -> bool
    - Dedup by (date, time, court_name)
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

        # Court name filter (caller decides tennis vs non-tennis)
        court_name = slot.get("court_name", "").strip()
        if not court_filter_fn(court_name):
            continue

        # Dedup by (date, time, court_name)
        key = (slot_date.isoformat(), slot_time.strftime("%H:%M"), court_name)
        if key in seen:
            continue
        seen.add(key)

        filtered.append({
            "date": slot_date.isoformat(),
            "time": slot_time.strftime("%I:%M %p"),
            "time_24h": slot_time.strftime("%H:%M"),
            "court_name": court_name,
            "day_of_week": slot_date.strftime("%A"),
            "is_weekend": is_weekend,
            "duration_minutes": slot.get("duration_minutes", 60),
        })

    # Sort by date then time
    filtered.sort(key=lambda s: (s["date"], s["time_24h"]))
    return filtered


def filter_slots(raw_slots: list[dict], settings: Settings) -> list[dict]:
    """
    Filter for tennis court slots (Ct 01-06) based on user preferences:
    - Weekdays (Mon-Fri): only slots at or after weekday_earliest_hour
    - Weekends (Sat-Sun): all slots OK
    - Only future dates within the lookahead window
    """
    def _tennis_court(name: str) -> bool:
        return bool(name and ALLOWED_COURTS_RE.search(name))

    # Diagnostic: count raw tennis slots before time/date filtering
    tennis_raw = sum(1 for s in raw_slots if _tennis_court(s.get("court_name", "")))
    if tennis_raw == 0 and raw_slots:
        # Log sample court_names to understand what's available
        court_names = sorted(set(s.get("court_name", "") for s in raw_slots if s.get("court_name")))
        logger.info(
            "No tennis courts in %d raw slots. Court names present: %s",
            len(raw_slots), court_names[:20],
        )
        # Log source distribution to understand where slots came from
        sources: dict[str, int] = {}
        for s in raw_slots:
            raw_data = s.get("raw")
            src = (
                raw_data.get("source", "unknown")
                if isinstance(raw_data, dict)
                else "unknown"
            )
            sources[src] = sources.get(src, 0) + 1
        logger.info("Raw slot sources: %s", sources)
    elif tennis_raw > 0:
        # Count how many pass date/time filters
        today = date.today()
        tennis_future = 0
        tennis_time_ok = 0
        for s in raw_slots:
            if not _tennis_court(s.get("court_name", "")):
                continue
            sd = parse_date_string(s.get("date", ""))
            st = parse_time_string(s.get("time", ""))
            if sd and sd > today and (sd - today).days <= settings.days_ahead:
                tennis_future += 1
                if st:
                    is_weekend = sd.weekday() >= 5
                    if is_weekend or st.hour >= settings.weekday_earliest_hour:
                        tennis_time_ok += 1
        logger.info(
            "Tennis slot pipeline: raw=%d → future=%d → time_ok=%d",
            tennis_raw, tennis_future, tennis_time_ok,
        )

    result = _filter_slots_base(raw_slots, settings, _tennis_court)
    logger.info("Filtered %d tennis slots from %d raw entries", len(result), len(raw_slots))
    return result


def filter_other_slots(raw_slots: list[dict], settings: Settings) -> list[dict]:
    """
    Filter for non-tennis slots (pickleball, ball machines, etc.).
    Same date/time validation as filter_slots() but keeps slots whose
    court_name does NOT match the tennis court regex.
    """
    def _non_tennis_court(name: str) -> bool:
        return bool(name and not ALLOWED_COURTS_RE.search(name))

    result = _filter_slots_base(raw_slots, settings, _non_tennis_court)
    logger.info("Filtered %d non-tennis slots from %d raw entries", len(result), len(raw_slots))
    return result
