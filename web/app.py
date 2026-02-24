"""
FastAPI web dashboard for the tennis court monitor.
"""
from datetime import date, datetime, timedelta
from pathlib import Path

import pytz
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import db
from config import Settings

_settings = Settings()
_CT = pytz.timezone("America/Chicago")

WEB_DIR = Path(__file__).parent
app = FastAPI(title="Tennis Court Monitor")
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")


def _format_utc_to_cst(utc_str: str) -> str:
    """Convert SQLite UTC timestamp like '2026-02-22 22:19:33' to 'Feb 22, 4:19 PM'."""
    if not utc_str:
        return ""
    try:
        utc_dt = datetime.strptime(utc_str, "%Y-%m-%d %H:%M:%S")
        utc_dt = pytz.utc.localize(utc_dt)
        cst_dt = utc_dt.astimezone(_CT)
        hour = cst_dt.strftime("%I:%M %p").lstrip("0")
        day = cst_dt.strftime("%b ") + str(cst_dt.day)
        return f"{day}, {hour}"
    except (ValueError, AttributeError):
        return utc_str


templates.env.filters["cst"] = _format_utc_to_cst

# Will be set by main.py
_run_check_fn = None


def set_check_fn(fn):
    global _run_check_fn
    _run_check_fn = fn


def _is_prime_time(slot_time_str: str, slot_date_str: str) -> bool:
    """Check if a slot is prime time (weekend or weekday >= 6PM)."""
    try:
        d = datetime.strptime(slot_date_str, "%Y-%m-%d").date()
        if d.weekday() >= 5:  # Weekend
            return True
        # Parse 12-hour time like "06:00 PM"
        t = datetime.strptime(slot_time_str.strip(), "%I:%M %p").time()
        return t.hour >= _settings.weekday_earliest_hour
    except (ValueError, AttributeError):
        return False


def _build_calendar(grouped_slots: dict) -> list[dict]:
    """Build a full 6-day calendar with slots mapped to each date."""
    today = date.today()
    calendar = []
    for i in range(1, 7):
        d = today + timedelta(days=i)
        d_str = d.isoformat()
        calendar.append({
            "date": d_str,
            "date_display": d.strftime("%b %d"),
            "day_name": d.strftime("%a"),
            "day_full": d.strftime("%A"),
            "is_weekend": d.weekday() >= 5,
            "is_tomorrow": i == 1,
            "slots": grouped_slots.get(d_str, []),
        })
    return calendar


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    recent_scans = await db.get_recent_scans(limit=1)
    scan_log = await db.get_scan_log(limit=50)
    current_slots = await db.get_current_availability()
    slot_events = await db.get_slot_events(limit=20)
    last_scan = recent_scans[0] if recent_scans else None

    # Group slots by date, enriching with prime-time flag for color coding
    grouped = {}
    for slot in current_slots:
        slot["is_prime_time"] = _is_prime_time(slot["slot_time"], slot["slot_date"])
        grouped.setdefault(slot["slot_date"], []).append(slot)

    calendar = _build_calendar(grouped)
    total_slots = sum(len(day["slots"]) for day in calendar)

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "last_scan": last_scan,
        "calendar": calendar,
        "total_slots": total_slots,
        "slot_events": slot_events,
        "scan_log": scan_log,
    })


@app.get("/api/status")
async def api_status():
    scans = await db.get_recent_scans(limit=1)
    slots = await db.get_current_availability()
    last = scans[0] if scans else None

    # Group slots by date, enriching with prime-time flag for color coding
    grouped = {}
    for slot in slots:
        slot["is_prime_time"] = _is_prime_time(slot["slot_time"], slot["slot_date"])
        grouped.setdefault(slot["slot_date"], []).append(slot)

    calendar = _build_calendar(grouped)

    def _calendar_json(cal):
        return [
            {
                "date": day["date"],
                "date_display": day["date_display"],
                "day_name": day["day_name"],
                "is_weekend": day["is_weekend"],
                "slots": [
                    {
                        "slot_time": s["slot_time"],
                        "court_name": s.get("court_name", ""),
                        "is_prime_time": s.get("is_prime_time", False),
                    }
                    for s in day["slots"]
                ],
            }
            for day in cal
        ]

    raw_time = last["scan_time"] if last else None
    return {
        "last_scan_time": _format_utc_to_cst(raw_time) if raw_time else None,
        "last_scan_time_utc": raw_time,
        "last_scan_success": bool(last["success"]) if last else None,
        "slots_found": last["slots_found"] if last else 0,
        "calendar": _calendar_json(calendar),
        "total_slots": sum(len(d["slots"]) for d in calendar),
    }


@app.get("/api/slot-events")
async def api_slot_events(limit: int = 100):
    """Return recent slot lifecycle events (opened/closed)."""
    events = await db.get_slot_events(limit=limit)
    return {"events": events}


@app.get("/api/notifications")
async def api_notifications(limit: int = 50):
    """Return recent notification history."""
    notifications = await db.get_notification_history(limit=limit)
    return {"notifications": notifications}


@app.post("/api/scan-now")
async def trigger_scan():
    if _run_check_fn is None:
        return JSONResponse({"error": "Scanner not initialized"}, status_code=503)
    try:
        result = await _run_check_fn()
        return {"status": "completed", "slots_found": result}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
