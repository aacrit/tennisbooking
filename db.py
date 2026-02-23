import aiosqlite
import json
import os
from datetime import datetime

_db_path: str = "data/tennisbooking.db"


async def init_db(db_path: str = None):
    global _db_path
    if db_path:
        _db_path = db_path
    os.makedirs(os.path.dirname(_db_path) or ".", exist_ok=True)

    async with aiosqlite.connect(_db_path) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS scan_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_time TEXT NOT NULL DEFAULT (datetime('now')),
                success INTEGER NOT NULL,
                error_message TEXT,
                slots_found INTEGER NOT NULL DEFAULT 0,
                duration_seconds REAL
            );

            CREATE TABLE IF NOT EXISTS available_slots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id INTEGER NOT NULL REFERENCES scan_results(id),
                slot_date TEXT NOT NULL,
                slot_time TEXT NOT NULL,
                court_name TEXT NOT NULL DEFAULT '',
                duration_minutes INTEGER,
                day_of_week TEXT,
                raw_data TEXT
            );

            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sent_time TEXT NOT NULL DEFAULT (datetime('now')),
                channel TEXT NOT NULL DEFAULT 'whatsapp',
                recipient TEXT NOT NULL,
                slot_count INTEGER NOT NULL,
                slot_details TEXT NOT NULL,
                success INTEGER NOT NULL DEFAULT 1,
                error_message TEXT
            );

            CREATE TABLE IF NOT EXISTS notified_slots (
                slot_date TEXT NOT NULL,
                slot_time TEXT NOT NULL,
                court_name TEXT NOT NULL DEFAULT '',
                notified_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (slot_date, slot_time, court_name)
            );

            CREATE TABLE IF NOT EXISTS slot_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_time TEXT NOT NULL DEFAULT (datetime('now')),
                event_type TEXT NOT NULL,
                slot_date TEXT NOT NULL,
                slot_time TEXT NOT NULL,
                court_name TEXT NOT NULL DEFAULT '',
                scan_id INTEGER REFERENCES scan_results(id),
                scan_method TEXT DEFAULT 'playwright'
            );

            CREATE TABLE IF NOT EXISTS current_slots (
                slot_date TEXT NOT NULL,
                slot_time TEXT NOT NULL,
                court_name TEXT NOT NULL DEFAULT '',
                first_seen TEXT NOT NULL DEFAULT (datetime('now')),
                last_seen TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (slot_date, slot_time, court_name)
            );
        """)
        await db.commit()


async def _conn():
    return aiosqlite.connect(_db_path)


async def record_scan(success: bool, error_message: str | None,
                      slots_found: int, duration_seconds: float) -> int:
    async with aiosqlite.connect(_db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "INSERT INTO scan_results (success, error_message, slots_found, duration_seconds) "
            "VALUES (?, ?, ?, ?)",
            (int(success), error_message, slots_found, duration_seconds),
        )
        await db.commit()
        return cursor.lastrowid


async def save_slots(scan_id: int, slots: list[dict]):
    async with aiosqlite.connect(_db_path) as db:
        for s in slots:
            await db.execute(
                "INSERT INTO available_slots "
                "(scan_id, slot_date, slot_time, court_name, duration_minutes, day_of_week, raw_data) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    scan_id,
                    s.get("date", ""),
                    s.get("time", ""),
                    s.get("court_name", ""),
                    s.get("duration_minutes"),
                    s.get("day_of_week", ""),
                    json.dumps(s.get("raw")) if s.get("raw") else None,
                ),
            )
        await db.commit()


async def get_notified_slot_keys() -> set[tuple]:
    async with aiosqlite.connect(_db_path) as db:
        cursor = await db.execute(
            "SELECT slot_date, slot_time, court_name FROM notified_slots"
        )
        rows = await cursor.fetchall()
        return {(r[0], r[1], r[2]) for r in rows}


async def mark_slots_notified(slots: list[dict]):
    async with aiosqlite.connect(_db_path) as db:
        for s in slots:
            await db.execute(
                "INSERT OR IGNORE INTO notified_slots (slot_date, slot_time, court_name) "
                "VALUES (?, ?, ?)",
                (s.get("date", ""), s.get("time", ""), s.get("court_name", "")),
            )
        await db.commit()


async def record_notification(channel: str, recipient: str,
                              slots: list[dict], success: bool,
                              error_message: str | None = None):
    async with aiosqlite.connect(_db_path) as db:
        await db.execute(
            "INSERT INTO notifications (channel, recipient, slot_count, slot_details, success, error_message) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                channel,
                recipient,
                len(slots),
                json.dumps(slots, default=str),
                int(success),
                error_message,
            ),
        )
        await db.commit()


async def get_previous_scan_slots() -> list[dict]:
    """Get slots from the most recent successful scan for change detection."""
    async with aiosqlite.connect(_db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT DISTINCT slot_date, slot_time, court_name "
            "FROM available_slots "
            "WHERE scan_id = ("
            "  SELECT id FROM scan_results "
            "  WHERE success = 1 "
            "  ORDER BY id DESC LIMIT 1"
            ")"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def update_current_slots(new_slots: set[tuple], scan_id: int,
                                method: str = "playwright") -> tuple[set, set]:
    """Atomically update current_slots and record lifecycle events.

    Args:
        new_slots: Set of (date, time, court_name) tuples from the latest scan.
        scan_id: The scan_results.id for this scan.
        method: 'playwright' or 'api_poll'.

    Returns:
        (opened, closed) sets of (date, time, court_name) tuples.
    """
    async with aiosqlite.connect(_db_path) as db:
        # Get current known state
        cursor = await db.execute(
            "SELECT slot_date, slot_time, court_name FROM current_slots"
        )
        rows = await cursor.fetchall()
        old_set = {(r[0], r[1], r[2]) for r in rows}

        opened = new_slots - old_set
        closed = old_set - new_slots

        # Record opened events
        for d, t, c in opened:
            await db.execute(
                "INSERT INTO slot_events (event_type, slot_date, slot_time, court_name, scan_id, scan_method) "
                "VALUES ('opened', ?, ?, ?, ?, ?)",
                (d, t, c, scan_id, method),
            )
            await db.execute(
                "INSERT OR REPLACE INTO current_slots (slot_date, slot_time, court_name, first_seen, last_seen) "
                "VALUES (?, ?, ?, datetime('now'), datetime('now'))",
                (d, t, c),
            )

        # Record closed events
        for d, t, c in closed:
            await db.execute(
                "INSERT INTO slot_events (event_type, slot_date, slot_time, court_name, scan_id, scan_method) "
                "VALUES ('closed', ?, ?, ?, ?, ?)",
                (d, t, c, scan_id, method),
            )
            await db.execute(
                "DELETE FROM current_slots WHERE slot_date=? AND slot_time=? AND court_name=?",
                (d, t, c),
            )

        # Update last_seen for still-present slots
        for d, t, c in (new_slots & old_set):
            await db.execute(
                "UPDATE current_slots SET last_seen=datetime('now') "
                "WHERE slot_date=? AND slot_time=? AND court_name=?",
                (d, t, c),
            )

        await db.commit()
        return opened, closed


async def get_slot_events(limit: int = 100) -> list[dict]:
    """Get recent slot lifecycle events."""
    async with aiosqlite.connect(_db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM slot_events ORDER BY id DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_recent_scans(limit: int = 20) -> list[dict]:
    async with aiosqlite.connect(_db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM scan_results ORDER BY id DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_current_availability() -> list[dict]:
    today = datetime.utcnow().strftime("%Y-%m-%d")
    async with aiosqlite.connect(_db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT slot_date, slot_time, court_name "
            "FROM current_slots WHERE slot_date >= ? "
            "ORDER BY slot_date, slot_time",
            (today,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_notification_history(limit: int = 50) -> list[dict]:
    async with aiosqlite.connect(_db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def cleanup_old_data(days: int = 14):
    """Remove scan data older than N days."""
    async with aiosqlite.connect(_db_path) as db:
        await db.execute(
            "DELETE FROM scan_results WHERE scan_time < datetime('now', ?)",
            (f"-{days} days",),
        )
        await db.execute(
            "DELETE FROM notified_slots WHERE notified_at < datetime('now', ?)",
            (f"-{days} days",),
        )
        await db.execute(
            "DELETE FROM slot_events WHERE event_time < datetime('now', ?)",
            (f"-{days} days",),
        )
        await db.execute(
            "DELETE FROM notifications WHERE sent_time < datetime('now', ?)",
            (f"-{days} days",),
        )
        await db.execute(
            "DELETE FROM current_slots WHERE slot_date < date('now')"
        )
        await db.commit()
