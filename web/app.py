"""
FastAPI web dashboard for the tennis court monitor.
"""
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import db

WEB_DIR = Path(__file__).parent
app = FastAPI(title="Tennis Court Monitor")
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

# Will be set by main.py
_run_check_fn = None


def set_check_fn(fn):
    global _run_check_fn
    _run_check_fn = fn


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
    recent_scans = await db.get_recent_scans(limit=5)
    current_slots = await db.get_current_availability()
    # [GITHUB-PAGES] Notifications disabled for static deployment
    # notifications = await db.get_notification_history(limit=3)
    notifications = []
    last_scan = recent_scans[0] if recent_scans else None

    grouped_slots = {}
    for slot in current_slots:
        grouped_slots.setdefault(slot["slot_date"], []).append(slot)

    calendar = _build_calendar(grouped_slots)
    total_slots = sum(len(day["slots"]) for day in calendar)

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "last_scan": last_scan,
        "calendar": calendar,
        "total_slots": total_slots,
        "recent_scans": recent_scans,
        "notifications": notifications,
    })


@app.get("/api/status")
async def api_status():
    scans = await db.get_recent_scans(limit=1)
    slots = await db.get_current_availability()
    last = scans[0] if scans else None

    grouped_slots = {}
    for slot in slots:
        grouped_slots.setdefault(slot["slot_date"], []).append(slot)

    calendar = _build_calendar(grouped_slots)

    return {
        "last_scan_time": last["scan_time"] if last else None,
        "last_scan_success": bool(last["success"]) if last else None,
        "slots_found": last["slots_found"] if last else 0,
        "calendar": [
            {
                "date": day["date"],
                "date_display": day["date_display"],
                "day_name": day["day_name"],
                "is_weekend": day["is_weekend"],
                "slots": [
                    {"slot_time": s["slot_time"], "court_name": s.get("court_name", "")}
                    for s in day["slots"]
                ],
            }
            for day in calendar
        ],
        "total_slots": sum(len(d["slots"]) for d in calendar),
    }


@app.post("/api/scan-now")
async def trigger_scan():
    if _run_check_fn is None:
        return JSONResponse({"error": "Scanner not initialized"}, status_code=503)
    try:
        result = await _run_check_fn()
        return {"status": "completed", "slots_found": result}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
