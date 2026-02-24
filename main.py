"""
Tennis Court Availability Monitor - McFetridge Sports Center

Single-process app: FastAPI dashboard + APScheduler + Playwright scraper
+ lightweight API poller for real-time slot detection.
"""
import asyncio
import logging
import time as _time
from contextlib import asynccontextmanager
from datetime import datetime

import pytz
import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import db
from config import Settings
from notifications.whatsapp import send_whatsapp, format_slots_message
from scraper.checker import AvailabilityChecker
from scraper.api_poller import APIPoller
from scraper.parser import filter_slots
from web.app import app, set_check_fn

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("tennismonitor")

settings = Settings()
CT = pytz.timezone("America/Chicago")

# Concurrency locks
_scan_lock = asyncio.Lock()
_poll_lock = asyncio.Lock()

# Module-level state for the API poller
_api_poller: APIPoller | None = None

# Notification cooldown: slot_key -> last notification timestamp
_last_notification_time: dict[tuple, float] = {}


async def run_full_scan() -> int:
    """Full Playwright scan: scrape, filter, detect changes, notify.

    Also refreshes the API context for the lightweight poller.
    """
    global _api_poller

    if _scan_lock.locked():
        logger.info("Full scan already in progress, skipping")
        return 0

    async with _scan_lock:
        start = _time.time()
        logger.info("Starting full Playwright scan...")

        try:
            checker = AvailabilityChecker(settings, diag_dir="data/diag")
            # Timeout after 5 minutes to allow login + 7 date scans
            raw_slots = await asyncio.wait_for(
                checker.check_availability(), timeout=300
            )
            filtered = filter_slots(raw_slots, settings)
            duration = _time.time() - start

            scan_id = await db.record_scan(True, None, len(filtered), duration)
            if filtered:
                await db.save_slots(scan_id, filtered)

            # Change detection via current_slots table (tennis only)
            current_set = {
                (s["date"], s["time"], s.get("court_name", ""))
                for s in filtered
            }
            opened, closed = await db.update_current_slots(
                current_set, scan_id, "playwright"
            )

            # Notify only on prime-time slots (weekday 6PM+ or weekends)
            prime_time_set = {
                (s["date"], s["time"], s.get("court_name", ""))
                for s in filtered if s.get("is_prime_time")
            }
            prime_opened = opened & prime_time_set
            if prime_opened:
                await _notify_opened_slots(prime_opened)

            # Refresh API context for the lightweight poller
            if settings.api_poll_enabled:
                api_context = checker.get_api_context()
                ep_count = len(api_context.get("endpoints", []))
                slot_eps = sum(
                    1 for e in api_context.get("endpoints", [])
                    if e.get("has_slot_data")
                )
                logger.info(
                    "API discovery: %d endpoints (%d with slot data)",
                    ep_count, slot_eps,
                )
                if _api_poller is None:
                    _api_poller = APIPoller(api_context, settings.days_ahead)
                else:
                    _api_poller.update_context(api_context)

            logger.info(
                "DIAGNOSTIC: full_scan raw=%d filtered=%d opened=%d closed=%d duration=%.1fs",
                len(raw_slots), len(filtered), len(opened), len(closed), duration,
            )
            return len(filtered)

        except Exception as e:
            duration = _time.time() - start
            logger.exception("Full scan failed: %s", e)
            await db.record_scan(False, str(e), 0, duration)
            return 0


async def run_api_poll() -> int:
    """Lightweight API poll for fast change detection (<1 second)."""
    global _api_poller

    if _api_poller is None:
        return 0

    if _poll_lock.locked():
        return 0

    async with _poll_lock:
        start = _time.time()
        try:
            # Run the synchronous poll in a thread to avoid blocking the event loop
            loop = asyncio.get_event_loop()
            raw_slots = await loop.run_in_executor(None, _api_poller.poll)

            if not raw_slots:
                if _api_poller.needs_rediscovery:
                    logger.info("API poller needs rediscovery, triggering full scan")
                    asyncio.create_task(run_full_scan())
                return 0

            filtered = filter_slots(raw_slots, settings)
            duration = _time.time() - start

            scan_id = await db.record_scan(True, None, len(filtered), duration)
            if filtered:
                await db.save_slots(scan_id, filtered)

            current_set = {
                (s["date"], s["time"], s.get("court_name", ""))
                for s in filtered
            }
            opened, closed = await db.update_current_slots(
                current_set, scan_id, "api_poll"
            )

            # Notify only on prime-time slots (weekday 6PM+ or weekends)
            prime_time_set = {
                (s["date"], s["time"], s.get("court_name", ""))
                for s in filtered if s.get("is_prime_time")
            }
            prime_opened = opened & prime_time_set
            if prime_opened:
                await _notify_opened_slots(prime_opened)

            if filtered or opened or closed:
                logger.info(
                    "DIAGNOSTIC: api_poll filtered=%d opened=%d closed=%d duration=%.2fs",
                    len(filtered), len(opened), len(closed), duration,
                )
            else:
                logger.debug("API poll: 0 filtered, %.2fs", duration)

            return len(filtered)

        except Exception as e:
            logger.warning("API poll failed: %s", e)
            return 0


async def _notify_opened_slots(opened: set[tuple]):
    """Send WhatsApp notification for newly opened slots with cooldown."""
    now = _time.time()
    now_ct = datetime.now(CT).strftime("%Y-%m-%d %H:%M:%S CT")

    # Filter out recently notified slots (cooldown)
    slots_to_notify = []
    for d, t, c in sorted(opened):
        key = (d, t, c)
        last_notified = _last_notification_time.get(key, 0)
        if now - last_notified > settings.notify_cooldown_seconds:
            slots_to_notify.append({
                "date": d, "time": t, "court_name": c, "detected_at": now_ct,
            })
            _last_notification_time[key] = now

    if not slots_to_notify:
        return

    logger.info("Notifying about %d new slots via WhatsApp", len(slots_to_notify))

    instance_id = settings.green_api_instance_id
    api_token = settings.green_api_token
    chat_ids = settings.whatsapp_chat_ids

    if instance_id and api_token and chat_ids:
        msg = format_slots_message(slots_to_notify)
        for chat_id in chat_ids:
            success = send_whatsapp(instance_id, api_token, chat_id, msg)
            await db.record_notification(
                "whatsapp", chat_id, slots_to_notify, success,
            )
    else:
        logger.warning(
            "WhatsApp not configured (missing GREEN_API_INSTANCE_ID / "
            "GREEN_API_TOKEN / WHATSAPP_CHAT_ID)"
        )


async def _burst_poll_loop():
    """Background loop for aggressive polling during the 7 AM CT window.

    Polls every api_poll_peak_seconds (default 15s) from 6:55-7:10 AM CT.
    Sleeps between burst windows.
    """
    while True:
        try:
            now = datetime.now(CT)
            in_burst = (
                (now.hour == 6 and now.minute >= 55) or
                (now.hour == 7 and now.minute <= 10)
            )

            if in_burst:
                await run_api_poll()
                await asyncio.sleep(settings.api_poll_peak_seconds)
            else:
                # Sleep until next check (60s), the scheduler handles non-burst polls
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("Burst poll loop error: %s", e)
            await asyncio.sleep(30)


def setup_scheduler() -> AsyncIOScheduler:
    """Configure scan schedule in Central Time."""
    scheduler = AsyncIOScheduler(timezone=CT)

    # === PLAYWRIGHT FULL SCANS ===

    # Peak: every 5 minutes from 6 AM to 8 AM CT
    scheduler.add_job(
        run_full_scan,
        CronTrigger(hour="6-7", minute="*/5", timezone=CT),
        id="peak_full_scan",
        name="Peak full scan (every 5 min, 6-8 AM CT)",
        replace_existing=True,
        misfire_grace_time=120,
    )

    # Normal: every 10 minutes from 8 AM to midnight CT
    scheduler.add_job(
        run_full_scan,
        CronTrigger(hour="8-23", minute="*/10", timezone=CT),
        id="normal_full_scan",
        name="Normal full scan (every 10 min, 8 AM - midnight CT)",
        replace_existing=True,
        misfire_grace_time=120,
    )

    # Overnight: every hour from midnight to 6 AM CT
    scheduler.add_job(
        run_full_scan,
        CronTrigger(hour="0-5", minute="0", timezone=CT),
        id="overnight_full_scan",
        name="Overnight full scan (every 1 hr, midnight-6 AM CT)",
        replace_existing=True,
        misfire_grace_time=120,
    )

    # === LIGHTWEIGHT API POLLS ===

    if settings.api_poll_enabled:
        # Peak hours: every 30 seconds from 6-8 AM CT
        scheduler.add_job(
            run_api_poll,
            CronTrigger(hour="6-7", minute="*", second="0,30", timezone=CT),
            id="peak_api_poll",
            name="Peak API poll (every 30s, 6-8 AM CT)",
            replace_existing=True,
            misfire_grace_time=10,
        )

        # Normal hours: every 2 minutes from 8 AM to midnight CT
        scheduler.add_job(
            run_api_poll,
            CronTrigger(hour="8-23", minute="*/2", timezone=CT),
            id="normal_api_poll",
            name="Normal API poll (every 2 min, 8 AM - midnight CT)",
            replace_existing=True,
            misfire_grace_time=30,
        )

    # Daily cleanup of old data
    scheduler.add_job(
        lambda: asyncio.ensure_future(db.cleanup_old_data(14)),
        CronTrigger(hour=3, minute=0, timezone=CT),
        id="cleanup",
        name="Daily data cleanup",
        replace_existing=True,
    )

    return scheduler


@asynccontextmanager
async def lifespan(app):
    """App startup and shutdown."""
    logger.info("Starting Tennis Court Monitor")
    await db.init_db(settings.db_path)

    # Wire up the scan function for the "Scan Now" button
    set_check_fn(run_full_scan)

    scheduler = setup_scheduler()
    scheduler.start()
    logger.info("Scheduler started with %d jobs", len(scheduler.get_jobs()))

    # Start burst poll loop for 7 AM window
    burst_task = None
    if settings.api_poll_enabled:
        burst_task = asyncio.create_task(_burst_poll_loop())
        logger.info("Burst poll loop started (6:55-7:10 AM CT, every %ds)", settings.api_poll_peak_seconds)

    # Run an initial full scan on startup (also discovers API endpoints)
    async def _startup_scan():
        try:
            logger.info("Starting initial scan...")
            await run_full_scan()
            logger.info("Initial scan completed")
        except Exception as e:
            logger.exception("STARTUP SCAN CRASHED: %s", e)
    asyncio.create_task(_startup_scan())

    # Log WhatsApp configuration status
    chat_ids = settings.whatsapp_chat_ids
    wa_configured = bool(
        settings.green_api_instance_id and
        settings.green_api_token and
        chat_ids
    )
    logger.info(
        "WhatsApp notifications: %s",
        f"CONFIGURED ({len(chat_ids)} recipient{'s' if len(chat_ids) != 1 else ''})" if wa_configured else "NOT CONFIGURED",
    )

    yield

    if burst_task:
        burst_task.cancel()
        try:
            await burst_task
        except asyncio.CancelledError:
            pass
    scheduler.shutdown(wait=False)
    logger.info("Shutting down")


app.router.lifespan_context = lifespan


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
    )
