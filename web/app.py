"""
FastAPI web dashboard for the tennis court monitor.
"""
import json
from datetime import datetime
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


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    recent_scans = await db.get_recent_scans(limit=10)
    current_slots = await db.get_current_availability()
    notifications = await db.get_notification_history(limit=5)
    last_scan = recent_scans[0] if recent_scans else None

    # Group slots by date
    grouped_slots = {}
    for slot in current_slots:
        grouped_slots.setdefault(slot["slot_date"], []).append(slot)

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "last_scan": last_scan,
        "grouped_slots": grouped_slots,
        "total_slots": len(current_slots),
        "recent_scans": recent_scans,
        "notifications": notifications,
        "now": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
    })


@app.get("/api/status")
async def api_status():
    scans = await db.get_recent_scans(limit=1)
    slots = await db.get_current_availability()
    last = scans[0] if scans else None
    return {
        "last_scan_time": last["scan_time"] if last else None,
        "last_scan_success": bool(last["success"]) if last else None,
        "slots_found": last["slots_found"] if last else 0,
        "available_slots": slots,
        "total_slots": len(slots),
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
