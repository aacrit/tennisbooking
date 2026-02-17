"""
Tennis Court Availability Monitor - McFetridge Sports Center

Single-process app: FastAPI dashboard + APScheduler + Playwright scraper.
"""
import asyncio
import logging
import time as _time
from contextlib import asynccontextmanager

import pytz
import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import db
from config import Settings
from notifications.emailer import send_availability_email
from scraper.checker import AvailabilityChecker
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

# Track running state to prevent overlapping scans
_scan_lock = asyncio.Lock()


async def run_check() -> int:
    """Core scan function: scrape → filter → notify. Returns slot count."""
    if _scan_lock.locked():
        logger.info("Scan already in progress, skipping")
        return 0

    async with _scan_lock:
        start = _time.time()
        logger.info("Starting availability check...")

        try:
            checker = AvailabilityChecker(settings)
            raw_slots = await checker.check_availability()
            filtered = filter_slots(raw_slots, settings)
            duration = _time.time() - start

            scan_id = await db.record_scan(True, None, len(filtered), duration)
            if filtered:
                await db.save_slots(scan_id, filtered)

            # Dedup: only notify on slots we haven't already emailed about
            previously_notified = await db.get_notified_slot_keys()
            new_slots = []
            for slot in filtered:
                key = (slot["date"], slot["time"], slot.get("court_name", ""))
                if key not in previously_notified:
                    new_slots.append(slot)

            if new_slots:
                logger.info("Found %d NEW slots, sending notification", len(new_slots))
                count = len(new_slots)
                subject = f"Tennis Court{'s' if count != 1 else ''} Available! ({count} slot{'s' if count != 1 else ''})"
                success = await send_availability_email(settings, new_slots)
                await db.record_notification(
                    settings.notify_email, subject, new_slots, success
                )
                if success:
                    await db.mark_slots_notified(new_slots)
            else:
                logger.info("No new slots (found %d total, all previously notified)", len(filtered))

            logger.info(
                "Check complete: %d raw, %d filtered, %d new, %.1fs",
                len(raw_slots), len(filtered), len(new_slots), duration,
            )
            return len(filtered)

        except Exception as e:
            duration = _time.time() - start
            logger.exception("Check failed: %s", e)
            await db.record_scan(False, str(e), 0, duration)
            return 0


def setup_scheduler() -> AsyncIOScheduler:
    """Configure scan schedule in Central Time."""
    scheduler = AsyncIOScheduler(timezone=CT)

    # Peak: every 5 minutes from 6:50 AM to 8:00 AM CT (catch 7 AM new slots)
    scheduler.add_job(
        run_check,
        CronTrigger(hour="6-7", minute="*/5", timezone=CT),
        id="peak_scan",
        name="Peak scan (6:50-8 AM CT)",
        replace_existing=True,
        misfire_grace_time=120,
    )

    # Normal: every 15 minutes from 8 AM to midnight CT (catch cancellations)
    scheduler.add_job(
        run_check,
        CronTrigger(hour="8-23", minute="*/15", timezone=CT),
        id="normal_scan",
        name="Normal scan (8 AM - midnight CT)",
        replace_existing=True,
        misfire_grace_time=120,
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
    set_check_fn(run_check)

    scheduler = setup_scheduler()
    scheduler.start()
    logger.info("Scheduler started with %d jobs", len(scheduler.get_jobs()))

    # Run an initial check on startup
    asyncio.create_task(run_check())

    yield

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
