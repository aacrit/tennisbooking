"""
Playwright-based availability checker for McFetridge tennis courts.

Navigates the ActiveNet SPA, intercepts API responses, and falls back
to DOM scraping to find available time slots.
"""
import asyncio
import json
import logging
import re
from datetime import date, timedelta
from playwright.async_api import async_playwright, Page, Response

from config import Settings

logger = logging.getLogger(__name__)

# Known API URL patterns that carry availability data
AVAILABILITY_API_PATTERNS = [
    "/facilit",
    "/schedule",
    "/timeslot",
    "/availability",
    "/quickreservation",
    "/reservation",
    "/booking",
    "/calendar",
]

# Booking portal URL
BOOKING_URL = (
    "https://anc.apm.activecommunities.com/chicagoparkdistrict/"
    "reservation/landing/quick?groupId=1&locale=en-US"
)

# Legacy booking URL (may have simpler interface)
LEGACY_URL = (
    "https://apm.activecommunities.com/chicagoparkdistrict/"
    "ActiveNet_Home?FileName=onlinequickfacilityreserve.sdi"
)


class AvailabilityChecker:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.captured_responses: list[dict] = []
        self.all_network_urls: list[str] = []

    async def _on_response(self, response: Response):
        """Intercept all responses; capture those that look like availability data."""
        url = response.url
        self.all_network_urls.append(url)

        if response.status != 200:
            return

        url_lower = url.lower()
        is_api = any(p in url_lower for p in AVAILABILITY_API_PATTERNS)
        is_json = "json" in (response.headers.get("content-type", "") or "")

        if is_api or is_json:
            try:
                body = await response.json()
                self.captured_responses.append({"url": url, "data": body})
                logger.info("Captured API response: %s", url)
            except Exception:
                pass

    async def check_availability(self) -> list[dict]:
        """
        Launch browser, navigate booking portal, extract available slots.
        Returns list of raw slot dicts with keys: date, time, court_name, etc.
        """
        self.captured_responses = []
        self.all_network_urls = []
        all_slots = []

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=not self.settings.debug_headed,
            )
            context = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            page = await context.new_page()
            page.on("response", self._on_response)

            try:
                # Try the modern SPA first
                all_slots = await self._check_modern_portal(page)

                # If no data, try the legacy portal
                if not all_slots:
                    logger.info("No slots from modern portal, trying legacy...")
                    all_slots = await self._check_legacy_portal(page)

            except Exception as e:
                logger.exception("Scraper error: %s", e)
                # Log captured network URLs for debugging
                logger.debug(
                    "Network URLs captured: %s",
                    json.dumps(self.all_network_urls[:50], indent=2),
                )
                raise
            finally:
                await browser.close()

        return all_slots

    async def _check_modern_portal(self, page: Page) -> list[dict]:
        """Navigate the modern ANC ActiveNet portal."""
        logger.info("Checking modern portal: %s", BOOKING_URL)
        await page.goto(BOOKING_URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_load_state("networkidle", timeout=30000)
        await asyncio.sleep(3)

        # Try to find and interact with the facility reservation interface
        slots = []

        # Step 1: Look for facility/activity selection
        await self._try_select_tennis(page)
        await asyncio.sleep(2)

        # Step 2: Check dates
        target_dates = self._get_target_dates()
        for target_date in target_dates:
            logger.info("Checking date: %s", target_date.isoformat())
            date_changed = await self._try_select_date(page, target_date)
            if date_changed:
                await page.wait_for_load_state("networkidle", timeout=15000)
                await asyncio.sleep(2)

            # Step 3: Extract available slots from DOM
            page_slots = await self._extract_slots_from_dom(page, target_date)
            slots.extend(page_slots)

        # Also check captured API responses for slot data
        api_slots = self._parse_captured_responses()
        if api_slots:
            slots.extend(api_slots)

        return slots

    async def _check_legacy_portal(self, page: Page) -> list[dict]:
        """Navigate the legacy ActiveNet portal."""
        logger.info("Checking legacy portal: %s", LEGACY_URL)
        try:
            await page.goto(LEGACY_URL, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_load_state("networkidle", timeout=30000)
            await asyncio.sleep(3)
        except Exception as e:
            logger.warning("Legacy portal failed to load: %s", e)
            return []

        slots = []

        # The legacy portal may have a simpler form-based interface
        # Look for facility dropdowns, date selectors, and availability grids
        await self._try_select_tennis(page)
        await asyncio.sleep(2)

        target_dates = self._get_target_dates()
        for target_date in target_dates:
            await self._try_select_date(page, target_date)
            await asyncio.sleep(2)
            page_slots = await self._extract_slots_from_dom(page, target_date)
            slots.extend(page_slots)

        return slots

    async def _try_select_tennis(self, page: Page):
        """Try to select tennis/McFetridge from facility selection."""
        selectors_to_try = [
            # Text-based selectors
            "text=Tennis",
            "text=McFetridge",
            "text=Tennis Court",
            "text=Court Time",
            # Common dropdown/select patterns
            "select[name*='facility' i]",
            "select[name*='type' i]",
            "select[id*='facility' i]",
            # React component patterns
            "[class*='facility'] [class*='option']",
            "[class*='category'] [class*='item']",
            "[data-facility-type*='tennis' i]",
            # Button/link patterns
            "a:has-text('Tennis')",
            "button:has-text('Tennis')",
            "[role='option']:has-text('Tennis')",
            "[role='listitem']:has-text('Tennis')",
        ]

        for selector in selectors_to_try:
            try:
                el = await page.query_selector(selector)
                if el and await el.is_visible():
                    await el.click()
                    logger.info("Clicked tennis selector: %s", selector)
                    await asyncio.sleep(1)
                    return True
            except Exception:
                continue

        # Try selecting from a dropdown by value
        dropdowns = await page.query_selector_all("select")
        for dropdown in dropdowns:
            try:
                options = await dropdown.query_selector_all("option")
                for opt in options:
                    text = (await opt.text_content() or "").lower()
                    if "tennis" in text or "mcfetridge" in text:
                        value = await opt.get_attribute("value")
                        if value:
                            await dropdown.select_option(value=value)
                            logger.info("Selected tennis from dropdown: %s", text)
                            return True
            except Exception:
                continue

        logger.warning("Could not find tennis facility selector")
        return False

    async def _try_select_date(self, page: Page, target_date: date) -> bool:
        """Try to select a specific date in the booking calendar."""
        date_str = target_date.strftime("%m/%d/%Y")
        date_iso = target_date.isoformat()
        date_mmdd = target_date.strftime("%m/%d")
        day_num = str(target_date.day)

        # Try date input fields
        date_inputs = await page.query_selector_all(
            "input[type='date'], input[name*='date' i], input[id*='date' i], "
            "input[placeholder*='date' i], input[class*='date' i]"
        )
        for inp in date_inputs:
            try:
                await inp.fill("")
                await inp.fill(date_str)
                await inp.press("Enter")
                logger.info("Filled date input: %s", date_str)
                return True
            except Exception:
                continue

        # Try calendar day buttons (common in date pickers)
        calendar_selectors = [
            f"[data-date='{date_iso}']",
            f"[data-date='{date_str}']",
            f"[aria-label*='{target_date.strftime('%B')}'][aria-label*='{day_num}']",
            f"td[data-day='{day_num}']",
            f".calendar-day:has-text('{day_num}')",
            f"[class*='day']:has-text('{day_num}')",
        ]
        for sel in calendar_selectors:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    await el.click()
                    logger.info("Clicked calendar day: %s", sel)
                    return True
            except Exception:
                continue

        # Try next/forward buttons to navigate to the right date
        nav_selectors = [
            "[class*='next']",
            "[aria-label*='next']",
            "[class*='forward']",
            "button:has-text('>')",
            "button:has-text('Next')",
        ]
        for sel in nav_selectors:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    await el.click()
                    logger.info("Clicked date nav: %s", sel)
                    await asyncio.sleep(1)
                    break
            except Exception:
                continue

        return False

    async def _extract_slots_from_dom(self, page: Page, target_date: date) -> list[dict]:
        """Extract available time slots from the rendered DOM."""
        slots = []

        # Strategy 1: Try to read Redux store directly
        try:
            redux_data = await page.evaluate("""
                () => {
                    // Check various places React/Redux stores are accessible
                    const sources = [
                        window.__REDUX_STATE__,
                        window.__reduxInitialState,
                        window.__NEXT_DATA__,
                        window.__INITIAL_STATE__,
                    ];
                    for (const src of sources) {
                        if (src) return JSON.stringify(src).substring(0, 50000);
                    }
                    return null;
                }
            """)
            if redux_data and ("slot" in redux_data.lower() or "available" in redux_data.lower()):
                logger.info("Found Redux data with potential slots")
                try:
                    parsed = json.loads(redux_data)
                    extracted = self._extract_from_state(parsed, target_date)
                    slots.extend(extracted)
                except Exception:
                    pass
        except Exception:
            pass

        # Strategy 2: Scrape visible slot elements from DOM
        try:
            dom_slots = await page.evaluate("""
                () => {
                    const results = [];

                    // Look for time slot elements (common patterns in booking UIs)
                    const slotSelectors = [
                        '[class*="slot"]',
                        '[class*="time-slot"]',
                        '[class*="timeslot"]',
                        '[class*="available"]',
                        '[class*="bookable"]',
                        '[class*="schedule"] [class*="item"]',
                        '[class*="calendar"] [class*="event"]',
                        'td[class*="open"]',
                        'td[class*="available"]',
                        '.schedule-cell',
                        '.time-cell',
                        '[data-available="true"]',
                        '[data-status="available"]',
                    ];

                    for (const selector of slotSelectors) {
                        const elements = document.querySelectorAll(selector);
                        elements.forEach(el => {
                            const text = el.textContent.trim();
                            if (text) {
                                results.push({
                                    text: text,
                                    className: el.className,
                                    tag: el.tagName,
                                    dataAttrs: Object.fromEntries(
                                        Array.from(el.attributes)
                                            .filter(a => a.name.startsWith('data-'))
                                            .map(a => [a.name, a.value])
                                    ),
                                    ariaLabel: el.getAttribute('aria-label') || '',
                                });
                            }
                        });
                    }

                    // Also look for any table rows that might contain time information
                    const tables = document.querySelectorAll('table');
                    tables.forEach(table => {
                        const rows = table.querySelectorAll('tr');
                        rows.forEach(row => {
                            const text = row.textContent.trim();
                            // Look for time patterns like "6:00 PM", "18:00", etc.
                            if (/\\d{1,2}:\\d{2}/.test(text)) {
                                const cells = Array.from(row.querySelectorAll('td, th'));
                                results.push({
                                    text: text,
                                    cells: cells.map(c => c.textContent.trim()),
                                    className: row.className,
                                    tag: 'TR',
                                    dataAttrs: {},
                                    ariaLabel: '',
                                });
                            }
                        });
                    });

                    return results;
                }
            """)

            if dom_slots:
                logger.info("Found %d DOM elements with potential slot data", len(dom_slots))
                for el in dom_slots:
                    parsed_slot = self._parse_dom_element(el, target_date)
                    if parsed_slot:
                        slots.append(parsed_slot)
        except Exception as e:
            logger.warning("DOM scraping error: %s", e)

        # Strategy 3: Full page text analysis for time patterns
        if not slots:
            try:
                page_text = await page.inner_text("body")
                text_slots = self._extract_times_from_text(page_text, target_date)
                slots.extend(text_slots)
            except Exception:
                pass

        return slots

    def _parse_dom_element(self, el: dict, target_date: date) -> dict | None:
        """Parse a DOM element into a slot dict."""
        text = el.get("text", "")
        if not text:
            return None

        # Extract time from text (e.g., "6:00 PM", "18:00", "6pm")
        time_match = re.search(
            r'(\d{1,2}):?(\d{2})?\s*(AM|PM|am|pm)?', text
        )
        if not time_match:
            return None

        hour = int(time_match.group(1))
        minute = int(time_match.group(2) or 0)
        ampm = (time_match.group(3) or "").upper()

        if ampm == "PM" and hour != 12:
            hour += 12
        elif ampm == "AM" and hour == 12:
            hour = 0

        time_str = f"{hour:02d}:{minute:02d}"

        # Check if element indicates availability (not booked/unavailable)
        class_name = el.get("className", "").lower()
        if any(x in class_name for x in ["unavailable", "booked", "disabled", "closed"]):
            return None

        # Extract court name if present
        court_name = ""
        court_match = re.search(r'(court\s*\d+|tennis\s*\d+)', text, re.IGNORECASE)
        if court_match:
            court_name = court_match.group(1)

        return {
            "date": target_date.isoformat(),
            "time": time_str,
            "court_name": court_name,
            "day_of_week": target_date.strftime("%A"),
            "duration_minutes": 60,
            "raw": el,
        }

    def _extract_times_from_text(self, text: str, target_date: date) -> list[dict]:
        """Extract time slots from raw page text using regex patterns."""
        slots = []
        # Look for time patterns like "6:00 PM - Available" or similar
        patterns = [
            r'(\d{1,2}:\d{2}\s*(?:AM|PM|am|pm))\s*[-–]\s*(?:available|open|book)',
            r'(?:available|open)\s*[-–:]\s*(\d{1,2}:\d{2}\s*(?:AM|PM|am|pm))',
        ]
        for pattern in patterns:
            matches = re.findall(pattern, text, re.IGNORECASE)
            for time_str in matches:
                slots.append({
                    "date": target_date.isoformat(),
                    "time": time_str.strip(),
                    "court_name": "",
                    "day_of_week": target_date.strftime("%A"),
                    "duration_minutes": 60,
                    "raw": {"source": "text_extraction", "match": time_str},
                })
        return slots

    def _extract_from_state(self, state: dict, target_date: date) -> list[dict]:
        """Recursively search Redux state for availability data."""
        slots = []
        if isinstance(state, dict):
            for key, value in state.items():
                key_lower = key.lower()
                if any(k in key_lower for k in ["slot", "schedule", "availability", "timeslot"]):
                    if isinstance(value, list):
                        for item in value:
                            if isinstance(item, dict):
                                slots.append({
                                    "date": target_date.isoformat(),
                                    "time": str(item.get("time", item.get("startTime", ""))),
                                    "court_name": str(item.get("facility", item.get("court", ""))),
                                    "day_of_week": target_date.strftime("%A"),
                                    "duration_minutes": item.get("duration", 60),
                                    "raw": item,
                                })
                elif isinstance(value, (dict, list)):
                    slots.extend(self._extract_from_state(value, target_date))
        elif isinstance(state, list):
            for item in state:
                if isinstance(item, dict):
                    slots.extend(self._extract_from_state(item, target_date))
        return slots

    def _parse_captured_responses(self) -> list[dict]:
        """Parse captured API responses for availability data."""
        slots = []
        for resp in self.captured_responses:
            data = resp.get("data")
            if not data:
                continue

            # Try to extract slots from various response formats
            items = []
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                # Common API response wrappers
                for key in ["data", "results", "items", "slots", "schedules",
                            "availability", "facilities", "timeSlots"]:
                    if key in data and isinstance(data[key], list):
                        items = data[key]
                        break

            for item in items:
                if not isinstance(item, dict):
                    continue
                # Try to extract time and availability info
                time_val = (
                    item.get("time") or item.get("startTime") or
                    item.get("start_time") or item.get("timeSlot") or ""
                )
                date_val = (
                    item.get("date") or item.get("startDate") or
                    item.get("start_date") or ""
                )
                available = item.get("available", item.get("isAvailable", True))

                if time_val and available:
                    slots.append({
                        "date": str(date_val),
                        "time": str(time_val),
                        "court_name": str(
                            item.get("facility", item.get("court", item.get("name", "")))
                        ),
                        "day_of_week": "",
                        "duration_minutes": item.get("duration", 60),
                        "raw": item,
                    })

        return slots

    def _get_target_dates(self) -> list[date]:
        """Get the list of dates to check (next N days)."""
        today = date.today()
        return [today + timedelta(days=i) for i in range(1, self.settings.days_ahead + 1)]
