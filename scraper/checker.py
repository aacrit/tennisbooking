"""
Playwright-based availability checker for McFetridge tennis courts.

Navigates the ActiveNet SPA, intercepts API responses, and falls back
to DOM scraping to find available time slots.
"""
import asyncio
import json
import logging
import os
import re
from datetime import date, timedelta
from datetime import datetime as dt
from playwright.async_api import async_playwright, Page, Response, Request

from config import Settings
from scraper.parser import ALLOWED_COURTS_RE

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
    "/activity",
    "/session",
    "/enrollment",
]

# McFetridge activity search — this is the ACTUAL path users take to find court time
ACTIVITY_SEARCH_URL = (
    "https://anc.apm.activecommunities.com/chicagoparkdistrict/"
    "activity/search?onlineSiteId=0&locale=en-US"
    "&activity_select_param=2&activity_keyword=mcfetridge&viewMode=list"
)

# Quick reservation URL (facility reservation interface)
BOOKING_URL = (
    "https://anc.apm.activecommunities.com/chicagoparkdistrict/"
    "reservation/landing/quick?groupId=1&locale=en-US"
)

# Reservation page (where "Make a Reservation" on mcfetridgesportscenter.com redirects)
RESERVATION_URL = (
    "https://anc.apm.activecommunities.com/chicagoparkdistrict/"
    "reservation?onlineSiteId=0&from_original_cui=true"
)

# Legacy booking URL (may have simpler interface)
LEGACY_URL = (
    "https://apm.activecommunities.com/chicagoparkdistrict/"
    "ActiveNet_Home?FileName=onlinequickfacilityreserve.sdi"
)

# Broader facility name regex for non-tennis support
FACILITY_RE = re.compile(
    r'((?:McFetridge\s+)?(?:Tennis\s+(?:Ct|Court)\s*\d+|'
    r'Pickleball\s*(?:Ct|Court)?\s*\d*|'
    r'Ball\s+Machine\s*\d*|'
    r'Court\s*\d+|Ct\s*\d+|'
    r'(?:Tennis|Pickleball|Badminton|Volleyball)\s+\w+))',
    re.IGNORECASE,
)

# Time and date patterns for deep JSON extraction
_TIME_RE = re.compile(r'\d{1,2}:\d{2}(?:\s*[AP]M)?', re.IGNORECASE)
_DATE_RE = re.compile(r'\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4}')


class AvailabilityChecker:
    def __init__(self, settings: Settings, diag_dir: str | None = None):
        self.settings = settings
        self.captured_responses: list[dict] = []
        self.captured_request_headers: dict[str, dict] = {}
        self.all_network_urls: list[str] = []
        self._browser_cookies: dict[str, str] = {}
        self._diag_dir = diag_dir
        self._diag_counter = 0

    # ── Diagnostics helpers ──────────────────────────────────────────

    async def _save_diag(self, page: Page, label: str):
        """Save screenshot + HTML snapshot for diagnostics."""
        if not self._diag_dir:
            return
        self._diag_counter += 1
        prefix = f"{self._diag_counter:02d}_{label}"
        try:
            os.makedirs(self._diag_dir, exist_ok=True)
            await page.screenshot(
                path=os.path.join(self._diag_dir, f"{prefix}.png"),
                full_page=True,
            )
            html = await page.content()
            html_path = os.path.join(self._diag_dir, f"{prefix}.html")
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(html)
            logger.debug("Saved diagnostic: %s", prefix)
        except Exception as e:
            logger.warning("Diagnostic save failed for %s: %s", label, e)

    async def _dump_dom_structure(self, page: Page, label: str = "dom_structure"):
        """Capture a summary of the page's DOM structure for diagnostics."""
        if not self._diag_dir:
            return
        try:
            dom_info = await page.evaluate("""
                () => {
                    const results = {
                        title: document.title,
                        url: window.location.href,
                        all_classes: [],
                        elements_with_time: [],
                        elements_with_court: [],
                        all_buttons: [],
                        all_links_text: [],
                        all_inputs: [],
                        body_text_sample: (document.body?.innerText || '').substring(0, 5000),
                    };

                    // Collect all unique class names
                    const classSet = new Set();
                    document.querySelectorAll('*').forEach(el => {
                        if (el.className && typeof el.className === 'string') {
                            el.className.split(/\\s+/).forEach(c => { if (c) classSet.add(c); });
                        }
                    });
                    results.all_classes = Array.from(classSet).sort();

                    // Find elements containing time patterns (H:MM AM/PM)
                    const timeRe = /\\d{1,2}:\\d{2}\\s*(AM|PM)/i;
                    document.querySelectorAll('*').forEach(el => {
                        const text = (el.textContent || '').trim();
                        if (text.length < 200 && text.length > 0 && timeRe.test(text)) {
                            results.elements_with_time.push({
                                tag: el.tagName,
                                class: el.className || '',
                                text: text.substring(0, 200),
                                id: el.id || '',
                            });
                        }
                    });
                    results.elements_with_time = results.elements_with_time.slice(0, 100);

                    // Find elements containing court/tennis/pickleball text
                    const courtRe = /tennis|court|pickleball|ball.machine|mcfetridge/i;
                    document.querySelectorAll('*').forEach(el => {
                        const text = (el.textContent || '').trim();
                        if (text.length < 300 && text.length > 0 && courtRe.test(text)) {
                            results.elements_with_court.push({
                                tag: el.tagName,
                                class: el.className || '',
                                text: text.substring(0, 300),
                                id: el.id || '',
                            });
                        }
                    });
                    results.elements_with_court = results.elements_with_court.slice(0, 100);

                    // All buttons with text
                    document.querySelectorAll('button, [role="button"]').forEach(el => {
                        results.all_buttons.push({
                            text: (el.textContent || '').trim().substring(0, 100),
                            class: el.className || '',
                            id: el.id || '',
                        });
                    });
                    results.all_buttons = results.all_buttons.slice(0, 50);

                    // All links with text
                    document.querySelectorAll('a').forEach(el => {
                        results.all_links_text.push({
                            text: (el.textContent || '').trim().substring(0, 100),
                            href: el.href || '',
                            class: el.className || '',
                        });
                    });
                    results.all_links_text = results.all_links_text.slice(0, 50);

                    // All inputs/selects
                    document.querySelectorAll('input, select, textarea').forEach(el => {
                        results.all_inputs.push({
                            tag: el.tagName,
                            type: el.type || '',
                            name: el.name || '',
                            id: el.id || '',
                            class: el.className || '',
                            value: (el.value || '').substring(0, 100),
                            placeholder: el.placeholder || '',
                        });
                    });

                    // Check for global state objects
                    const stateKeys = ['__REDUX_STATE__', '__reduxInitialState',
                                       '__NEXT_DATA__', '__INITIAL_STATE__',
                                       '__APP_DATA__', '__STORE__'];
                    results.window_state = {};
                    for (const key of stateKeys) {
                        if (window[key]) results.window_state[key] = 'EXISTS';
                    }

                    return results;
                }
            """)
            os.makedirs(self._diag_dir, exist_ok=True)
            with open(os.path.join(self._diag_dir, f"{label}.json"), "w") as f:
                json.dump(dom_info, f, indent=2, default=str)
            logger.debug("Saved DOM structure: %s", label)
        except Exception as e:
            logger.warning("DOM structure dump failed: %s", e)

    def _save_diag_json(self, filename: str, data):
        """Save a JSON diagnostics file."""
        if not self._diag_dir:
            return
        try:
            os.makedirs(self._diag_dir, exist_ok=True)
            with open(os.path.join(self._diag_dir, filename), "w") as f:
                json.dump(data, f, indent=2, default=str)
        except Exception as e:
            logger.warning("Failed to save diagnostic %s: %s", filename, e)

    # ── Network interception ─────────────────────────────────────────

    async def _on_request(self, request: Request):
        """Capture request headers for API endpoints (used by the lightweight poller)."""
        url_lower = request.url.lower()
        if any(p in url_lower for p in AVAILABILITY_API_PATTERNS):
            headers = dict(request.headers)
            # Keep only useful headers for replay
            keep = {"cookie", "authorization", "x-csrf-token", "x-requested-with",
                    "accept", "referer", "origin", "content-type"}
            self.captured_request_headers[request.url] = {
                k: v for k, v in headers.items() if k.lower() in keep
            }

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

    # ── Main entry point ─────────────────────────────────────────────

    async def check_availability(self) -> list[dict]:
        """
        Launch browser, navigate booking portal, extract available slots.
        Returns list of raw slot dicts with keys: date, time, court_name, etc.
        """
        self.captured_responses = []
        self.captured_request_headers = {}
        self.all_network_urls = []
        self._browser_cookies = {}
        self._diag_counter = 0
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
            page.on("request", self._on_request)
            page.on("response", self._on_response)

            try:
                # Strategy 1: Activity search (primary — how users actually find court time)
                all_slots = await self._check_activity_search(page)

                # Strategy 2: Quick reservation page
                if not all_slots:
                    logger.info("No slots from activity search, trying quick reservation...")
                    all_slots = await self._check_modern_portal(page)

                # Strategy 3: Reservation page (where McFetridge site links to)
                if not all_slots:
                    logger.info("No slots from quick reservation, trying reservation page...")
                    all_slots = await self._check_reservation_page(page)

                # Strategy 4: Legacy portal
                if not all_slots:
                    logger.info("No slots from reservation page, trying legacy...")
                    all_slots = await self._check_legacy_portal(page)

            except Exception as e:
                logger.exception("Scraper error: %s", e)
                logger.debug(
                    "Network URLs captured: %s",
                    json.dumps(self.all_network_urls[:50], indent=2),
                )
                raise
            finally:
                # Capture cookies before closing for the API poller
                try:
                    cookies = await context.cookies()
                    self._browser_cookies = {c["name"]: c["value"] for c in cookies}
                except Exception:
                    pass

                # Save final diagnostics
                self._save_diag_json("network_urls.json", self.all_network_urls)
                api_diag = []
                for resp in self.captured_responses:
                    url = resp["url"]
                    data = resp.get("data")
                    api_diag.append({
                        "url": url,
                        "type": type(data).__name__,
                        "keys": list(data.keys()) if isinstance(data, dict) else None,
                        "length": len(data) if isinstance(data, list) else None,
                        "sample": json.dumps(data, default=str)[:3000],
                    })
                self._save_diag_json("api_responses.json", api_diag)

                await browser.close()

        return all_slots

    # ── Modern portal ────────────────────────────────────────────────

    async def _check_modern_portal(self, page: Page) -> list[dict]:
        """Navigate the modern ANC ActiveNet portal."""
        logger.info("Checking modern portal: %s", BOOKING_URL)
        await page.goto(BOOKING_URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_load_state("networkidle", timeout=30000)
        await asyncio.sleep(3)

        await self._save_diag(page, "page_loaded")
        await self._dump_dom_structure(page, "dom_structure_initial")

        # Try to find and interact with the facility reservation interface
        slots = []

        # Step 1: Look for facility/activity selection
        await self._try_select_tennis(page)
        await asyncio.sleep(2)
        await self._save_diag(page, "after_tennis_select")

        # Step 2: Check dates
        target_dates = self._get_target_dates()
        for target_date in target_dates:
            logger.info("Checking date: %s", target_date.isoformat())
            date_changed = await self._try_select_date(page, target_date)
            if date_changed:
                await page.wait_for_load_state("networkidle", timeout=15000)
                await asyncio.sleep(2)

            await self._save_diag(page, f"date_{target_date.isoformat()}")

            # Step 3: Extract available slots from DOM
            page_slots = await self._extract_slots_from_dom(page, target_date)
            slots.extend(page_slots)

        # Also check captured API responses for slot data
        api_slots = self._parse_captured_responses()
        if api_slots:
            slots.extend(api_slots)

        # Dump DOM structure after all navigation
        await self._dump_dom_structure(page, "dom_structure_final")

        logger.info(
            "SCRAPER SUMMARY: captured_responses=%d, network_urls=%d, "
            "dom_slots=%d, api_slots=%d, total=%d",
            len(self.captured_responses), len(self.all_network_urls),
            len(slots) - len(api_slots), len(api_slots), len(slots),
        )

        return slots

    # ── Legacy portal ────────────────────────────────────────────────

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

        await self._save_diag(page, "legacy_loaded")

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

    # ── Activity search approach (PRIMARY) ─────────────────────────

    @staticmethod
    def _clean_activity_name(name: str) -> str:
        """Extract a clean court/activity identifier from search result text.

        e.g. "McFetridge Tennis Ct 1 Court Time Reservation Feb 20-25 2026..."
             → "McFetridge Tennis Ct 1"
        """
        if not name:
            return ""

        # Try FACILITY_RE first (Tennis Ct 1, Pickleball Court, etc.)
        match = FACILITY_RE.search(name)
        if match:
            return match.group(1).strip()

        # Try a broader pattern: "McFetridge <something> Court Time"
        match = re.search(
            r'(McFetridge\s+[\w\s]+?)(?:\s+Court\s+Time|\s+Reservation|\s+Res\b)',
            name, re.IGNORECASE,
        )
        if match:
            return match.group(1).strip()

        # Strip common suffixes and keep meaningful prefix
        cleaned = re.sub(
            r'\s*(?:Court\s+Time|Reservation|Res\b|Registration).*$',
            '', name, flags=re.IGNORECASE,
        ).strip()

        # Strip leading date fragments (e.g. "2026" prefix from SPA rendering)
        cleaned = re.sub(r'^\d{4}\s*', '', cleaned).strip()

        # If cleaning produced a very short/garbled result (< 5 chars),
        # the SPA likely didn't render fully. Return a more descriptive name.
        if len(cleaned) < 5:
            # Try to salvage from the original name
            salvaged = re.sub(
                r'\s*(?:Court\s+Time|Reservation|Res\b|Registration).*$',
                '', name, flags=re.IGNORECASE,
            ).strip()
            salvaged = re.sub(r'^\d{4}\s*', '', salvaged).strip()
            if len(salvaged) > len(cleaned):
                cleaned = salvaged
            # Still too short — use original (truncated) so it's at least diagnosable
            if len(cleaned) < 5:
                cleaned = name.strip()[:80]
                logger.warning(
                    "Activity name cleaning produced garbled result, "
                    "using raw text: %r", cleaned,
                )

        return cleaned[:60]

    @staticmethod
    def _best_court_name(dom_name: str, activity_name: str) -> str:
        """Choose the best court name between a DOM-extracted name and an
        activity-page name.

        Priority:
        1. If the DOM name is a valid tennis court (Tennis Ct 1-6), keep it.
        2. If the DOM name matches FACILITY_RE (any recognizable facility), keep it.
        3. Otherwise, use the activity page name (if it's meaningful).
        4. Fall back to whichever is non-empty.
        """
        dom_name = (dom_name or "").strip()
        activity_name = (activity_name or "").strip()

        # DOM name is a recognized tennis court — always prefer it
        if dom_name and ALLOWED_COURTS_RE.search(dom_name):
            return dom_name

        # DOM name matches facility regex (e.g. "Pickleball Court 1")
        if dom_name and FACILITY_RE.search(dom_name):
            return dom_name

        # Activity name is a recognized tennis court
        if activity_name and ALLOWED_COURTS_RE.search(activity_name):
            return activity_name

        # Activity name looks like a real facility (longer than 5 chars)
        if activity_name and len(activity_name) >= 5:
            return activity_name

        # Fall back to whatever is available
        return dom_name or activity_name or "Unknown"

    async def _check_activity_search(self, page: Page) -> list[dict]:
        """Search for McFetridge activities and extract availability.

        This is the primary approach — it mirrors what actual users do:
        search for McFetridge activities, click on court time listings,
        and view available sessions.
        """
        logger.info("Checking activity search: %s", ACTIVITY_SEARCH_URL)
        await page.goto(ACTIVITY_SEARCH_URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_load_state("networkidle", timeout=30000)
        await asyncio.sleep(6)  # Extra wait for SPA to render results

        await self._save_diag(page, "activity_search_loaded")
        await self._dump_dom_structure(page, "dom_activity_search")

        slots = []

        # Find activity listing links from the rendered search results
        activity_links = await page.evaluate("""
            () => {
                const links = [];

                // Strategy A: Look for links to activity detail pages
                document.querySelectorAll('a[href*="/activity/search/detail/"]').forEach(el => {
                    links.push({
                        href: el.href,
                        text: (el.textContent || '').trim().substring(0, 200),
                    });
                });

                // Strategy B: Look for any links/buttons with court-related text
                if (links.length === 0) {
                    document.querySelectorAll('a, button, [role="link"]').forEach(el => {
                        const text = (el.textContent || '').toLowerCase();
                        const href = el.href || el.getAttribute('href') || '';
                        if (text.includes('court time') || text.includes('tennis ct') ||
                            text.includes('pickleball') || text.includes('ball machine') ||
                            text.includes('mcfetridge')) {
                            links.push({
                                href: href,
                                text: (el.textContent || '').trim().substring(0, 200),
                            });
                        }
                    });
                }

                // Strategy C: Look for any card/list item components with activity names
                if (links.length === 0) {
                    const cardSelectors = [
                        '[class*="activity"]', '[class*="result"]',
                        '[class*="card"]', '[class*="listing"]',
                        '[class*="item"]', 'li',
                    ];
                    for (const sel of cardSelectors) {
                        document.querySelectorAll(sel).forEach(el => {
                            const text = (el.textContent || '').toLowerCase();
                            if (text.length < 500 && (
                                text.includes('court') || text.includes('tennis') ||
                                text.includes('pickleball') || text.includes('ball machine')
                            )) {
                                const link = el.querySelector('a');
                                links.push({
                                    href: link ? (link.href || '') : '',
                                    text: (el.textContent || '').trim().substring(0, 200),
                                    isCard: true,
                                });
                            }
                        });
                        if (links.length > 0) break;
                    }
                }

                return links;
            }
        """)

        logger.info("Found %d activity links/cards", len(activity_links))

        # If links look garbled (very short text), wait more and retry
        if activity_links and all(
            len(l.get("text", "").strip()) < 10 for l in activity_links
        ):
            logger.warning(
                "Activity link text looks garbled (all < 10 chars), "
                "waiting for SPA to finish rendering..."
            )
            await asyncio.sleep(5)
            activity_links = await page.evaluate("""
                () => {
                    const links = [];
                    document.querySelectorAll(
                        'a[href*="/activity/search/detail/"]'
                    ).forEach(el => {
                        links.push({
                            href: el.href,
                            text: (el.textContent || '').trim().substring(0, 200),
                        });
                    });
                    return links;
                }
            """)
            logger.info("Retry found %d activity links", len(activity_links))

        self._save_diag_json("activity_links.json", activity_links)

        # Sort: tennis-related links first, then others
        def _tennis_score(link):
            text = (link.get("text", "") or "").lower()
            if "tennis" in text and "ct" in text:
                return 0  # Tennis Ct — highest priority
            if "tennis" in text:
                return 1
            if "court time" in text:
                return 2
            if "pickleball" in text or "ball machine" in text:
                return 3
            return 4  # Non-court activities (clubroom, etc.)

        activity_links.sort(key=_tennis_score)

        # Track API responses from search page (before visiting activity details)
        search_response_count = len(self.captured_responses)

        # Visit each activity detail page to get availability
        for link in activity_links[:10]:
            href = link.get("href", "")
            name = link.get("text", "").strip()
            if not href or not href.startswith("http"):
                continue

            # Extract a clean court/activity identifier from the link text
            clean_name = self._clean_activity_name(name)
            logger.info(
                "Checking activity: %s → clean_name=%s", name[:80], clean_name,
            )

            # Track which API responses belong to THIS activity page
            responses_before = len(self.captured_responses)

            try:
                await page.goto(href, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_load_state("networkidle", timeout=30000)
                await asyncio.sleep(3)

                safe_name = re.sub(r'[^\w]', '_', name[:30])
                await self._save_diag(page, f"activity_{safe_name}")
                await self._dump_dom_structure(page, f"dom_activity_{safe_name}")

                # If the search-page name was garbled, try to get a better
                # name from the activity detail page heading or title
                if len(clean_name) < 10 or not FACILITY_RE.search(clean_name):
                    page_title = await page.title()
                    heading_text = await page.evaluate("""
                        () => {
                            const h = document.querySelector(
                                'h1, h2, [class*="activity-name"], '
                                + '[class*="activityName"], [class*="title"]'
                            );
                            return h ? h.textContent.trim() : '';
                        }
                    """)
                    for candidate in [heading_text, page_title]:
                        better = self._clean_activity_name(candidate)
                        if better and FACILITY_RE.search(better):
                            logger.info(
                                "Upgraded activity name from %r to %r "
                                "(via detail page)",
                                clean_name, better,
                            )
                            clean_name = better
                            break

                # Extract from the activity detail page
                target_dates = self._get_target_dates()
                for td in target_dates:
                    page_slots = await self._extract_slots_from_dom(page, td)
                    for s in page_slots:
                        s["court_name"] = self._best_court_name(
                            s.get("court_name", ""), clean_name,
                        )
                    slots.extend(page_slots)

                # Parse API responses captured DURING this activity's page load
                new_responses = self.captured_responses[responses_before:]
                for resp in new_responses:
                    per_activity_slots = self._parse_single_response(resp)
                    for s in per_activity_slots:
                        s["court_name"] = self._best_court_name(
                            s.get("court_name", ""), clean_name,
                        )
                    slots.extend(per_activity_slots)

            except Exception as e:
                logger.warning("Error loading activity %s: %s", name[:50], e)

        # Log the activity name mapping for diagnostics
        activity_name_map = [
            {"raw": link.get("text", "")[:100],
             "clean": self._clean_activity_name(link.get("text", "")),
             "href": link.get("href", "")}
            for link in activity_links[:10]
        ]
        self._save_diag_json("activity_name_map.json", activity_name_map)

        logger.info(
            "Activity search: %d links checked, %d total slots found",
            len(activity_links), len(slots),
        )

        return slots

    # ── Reservation page approach ─────────────────────────────────

    async def _check_reservation_page(self, page: Page) -> list[dict]:
        """Check the reservation page (where McFetridge site links to)."""
        logger.info("Checking reservation page: %s", RESERVATION_URL)
        try:
            await page.goto(RESERVATION_URL, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_load_state("networkidle", timeout=30000)
            await asyncio.sleep(3)
        except Exception as e:
            logger.warning("Reservation page failed to load: %s", e)
            return []

        await self._save_diag(page, "reservation_page_loaded")
        await self._dump_dom_structure(page, "dom_reservation_page")

        slots = []

        # Try to interact with the reservation interface
        await self._try_select_tennis(page)
        await asyncio.sleep(2)

        target_dates = self._get_target_dates()
        for target_date in target_dates:
            await self._try_select_date(page, target_date)
            await asyncio.sleep(2)
            page_slots = await self._extract_slots_from_dom(page, target_date)
            slots.extend(page_slots)

        # Check captured API responses
        api_slots = self._parse_captured_responses()
        if api_slots:
            slots.extend(api_slots)

        return slots

    # ── Facility selection ───────────────────────────────────────────

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
                if el:
                    is_vis = await el.is_visible()
                    logger.debug("Tennis selector '%s': found=True visible=%s", selector, is_vis)
                    if is_vis:
                        await el.click()
                        logger.info("Clicked tennis selector: %s", selector)
                        await asyncio.sleep(1)
                        return True
                else:
                    logger.debug("Tennis selector '%s': not found", selector)
            except Exception as e:
                logger.debug("Tennis selector '%s': error=%s", selector, e)
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

    # ── Date selection ───────────────────────────────────────────────

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

    # ── Slot extraction from DOM ─────────────────────────────────────

    async def _extract_slots_from_dom(self, page: Page, target_date: date) -> list[dict]:
        """Extract available time slots from the rendered DOM."""
        slots = []
        strategy_counts = {"redux": 0, "targeted_dom": 0, "broad_dom": 0, "text": 0}

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
            if redux_data:
                # Save Redux state for diagnostics regardless of content
                self._save_diag_json(
                    f"redux_state_{target_date.isoformat()}.json",
                    redux_data[:100000],
                )
                if "slot" in redux_data.lower() or "available" in redux_data.lower():
                    logger.info("Found Redux data with potential slots")
                    try:
                        parsed = json.loads(redux_data)
                        extracted = self._extract_from_state(parsed, target_date)
                        strategy_counts["redux"] = len(extracted)
                        slots.extend(extracted)
                    except Exception:
                        pass
        except Exception:
            pass

        # Strategy 2: Scrape visible slot elements from DOM (targeted selectors)
        try:
            dom_slots = await page.evaluate("""
                () => {
                    const results = [];

                    // Look for time slot elements (targeted patterns)
                    const slotSelectors = [
                        '[class*="time-slot"]',
                        '[class*="timeslot"]',
                        '[class*="bookable"]',
                        'td[class*="open"]',
                        'td[class*="available"]',
                        '.schedule-cell',
                        '.time-cell',
                        '[data-available="true"]',
                        '[data-status="available"]',
                    ];

                    const timePattern = /\\d{1,2}:\\d{2}\\s*(AM|PM)/i;

                    for (const selector of slotSelectors) {
                        const elements = document.querySelectorAll(selector);
                        elements.forEach(el => {
                            const text = el.textContent.trim();
                            // Only capture elements whose text contains a time with AM/PM
                            if (text && timePattern.test(text)) {
                                const parent = el.closest('[class*="facility"], [class*="court"], [class*="resource"], [class*="lane"], [class*="room"]');
                                results.push({
                                    text: text,
                                    parentText: parent ? parent.textContent.trim().substring(0, 200) : '',
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

                    return results;
                }
            """)

            if dom_slots:
                logger.info("Found %d DOM elements with potential slot data (targeted)", len(dom_slots))
                for el in dom_slots:
                    parsed_slot = self._parse_dom_element(el, target_date)
                    if parsed_slot:
                        slots.append(parsed_slot)
                        strategy_counts["targeted_dom"] += 1
        except Exception as e:
            logger.warning("DOM scraping error (targeted): %s", e)

        # Strategy 2b: Broad DOM scan — find ALL elements with time text
        try:
            broad_slots = await page.evaluate("""
                () => {
                    const results = [];
                    const timePattern = /\\d{1,2}:\\d{2}\\s*(AM|PM)/i;

                    // Walk all leaf-ish elements (small text content)
                    document.querySelectorAll('td, div, span, li, a, button, p, label').forEach(el => {
                        const fullText = (el.textContent || '').trim();

                        if (fullText.length > 500) return; // Skip large containers
                        if (!timePattern.test(fullText)) return;

                        // Walk up to find context (facility name, date, etc.)
                        let contextEl = el;
                        let contextText = '';
                        for (let i = 0; i < 5 && contextEl; i++) {
                            contextEl = contextEl.parentElement;
                            if (contextEl) {
                                const ct = (contextEl.textContent || '').trim();
                                if (ct.length < 1000 && ct.length > contextText.length) {
                                    contextText = ct;
                                }
                            }
                        }

                        results.push({
                            text: fullText.substring(0, 300),
                            contextText: contextText.substring(0, 500),
                            className: el.className || '',
                            tag: el.tagName,
                            parentClass: (el.parentElement?.className) || '',
                            dataAttrs: Object.fromEntries(
                                Array.from(el.attributes || [])
                                    .filter(a => a.name.startsWith('data-'))
                                    .map(a => [a.name, a.value])
                            ),
                            ariaLabel: el.getAttribute('aria-label') || '',
                        });
                    });

                    return results;
                }
            """)

            if broad_slots:
                logger.info("Broad DOM scan found %d elements with time text", len(broad_slots))
                # Save for diagnostics
                self._save_diag_json(
                    f"broad_dom_{target_date.isoformat()}.json", broad_slots
                )
                for el in broad_slots:
                    parsed = self._parse_dom_element_broad(el, target_date)
                    if parsed:
                        slots.append(parsed)
                        strategy_counts["broad_dom"] += 1
        except Exception as e:
            logger.warning("Broad DOM scan error: %s", e)

        # Strategy 3: Full page text analysis for time patterns
        if not slots:
            try:
                page_text = await page.inner_text("body")
                text_slots = self._extract_times_from_text(page_text, target_date)
                strategy_counts["text"] = len(text_slots)
                slots.extend(text_slots)
            except Exception:
                pass

        logger.info(
            "DOM extraction for %s: redux=%d targeted=%d broad=%d text=%d total=%d",
            target_date.isoformat(),
            strategy_counts["redux"], strategy_counts["targeted_dom"],
            strategy_counts["broad_dom"], strategy_counts["text"], len(slots),
        )

        return slots

    # ── DOM element parsers ──────────────────────────────────────────

    def _parse_dom_element(self, el: dict, target_date: date) -> dict | None:
        """Parse a DOM element into a slot dict (targeted, strict matching)."""
        text = el.get("text", "")
        if not text:
            return None

        # Extract time from text — require full "H:MM AM/PM" format
        time_match = re.search(
            r'(\d{1,2}):(\d{2})\s*(AM|PM|am|pm)', text
        )
        if not time_match:
            return None

        hour = int(time_match.group(1))
        minute = int(time_match.group(2))
        ampm = time_match.group(3).upper()

        if ampm == "PM" and hour != 12:
            hour += 12
        elif ampm == "AM" and hour == 12:
            hour = 0

        time_str = f"{hour:02d}:{minute:02d}"

        # Check if element indicates availability (not booked/unavailable)
        class_name = el.get("className", "").lower()
        if any(x in class_name for x in ["unavailable", "booked", "disabled", "closed"]):
            return None

        # Require positive availability signal — at least one must be true:
        positive_class_signals = ["available", "bookable", "open", "reserv"]
        has_positive_class = any(s in class_name for s in positive_class_signals)
        has_positive_data = any(
            "available" in str(v).lower() or v.lower() == "true"
            for v in el.get("dataAttrs", {}).values()
        )
        has_court_in_text = bool(FACILITY_RE.search(text))
        has_court_in_parent = bool(FACILITY_RE.search(el.get("parentText", "")))
        has_court_in_aria = bool(FACILITY_RE.search(el.get("ariaLabel", "")))

        has_court_anywhere = has_court_in_text or has_court_in_parent or has_court_in_aria
        has_availability_signal = has_positive_class or has_positive_data

        # Must have court context
        if not has_court_anywhere:
            return None
        if not has_court_in_text and not has_court_in_aria and not has_availability_signal:
            return None

        # Extract court name from text, parentText, ariaLabel, data-attrs
        court_name = ""
        for source in [text, el.get("parentText", ""), el.get("ariaLabel", "")]:
            match = FACILITY_RE.search(source)
            if match:
                court_name = match.group(1)
                break

        if not court_name:
            for attr_val in el.get("dataAttrs", {}).values():
                match = FACILITY_RE.search(str(attr_val))
                if match:
                    court_name = match.group(1)
                    break

        if not court_name:
            logger.debug(
                "DOM element rejected: no court_name found (time=%s, class=%s)",
                time_str, el.get("className", ""),
            )
            return None

        return {
            "date": target_date.isoformat(),
            "time": time_str,
            "court_name": court_name,
            "day_of_week": target_date.strftime("%A"),
            "duration_minutes": 60,
            "raw": el,
        }

    def _parse_dom_element_broad(self, el: dict, target_date: date) -> dict | None:
        """Parse DOM element with broader facility name matching (Strategy 2b)."""
        text = el.get("text", "")
        if not text:
            return None

        # Extract time
        time_match = re.search(r'(\d{1,2}):(\d{2})\s*(AM|PM|am|pm)', text)
        if not time_match:
            return None

        hour = int(time_match.group(1))
        minute = int(time_match.group(2))
        ampm = time_match.group(3).upper()

        if ampm == "PM" and hour != 12:
            hour += 12
        elif ampm == "AM" and hour == 12:
            hour = 0

        time_str = f"{hour:02d}:{minute:02d}"

        # Check for negative signals in class
        class_name = (el.get("className", "") or "").lower()
        if any(x in class_name for x in ["unavailable", "booked", "disabled", "closed"]):
            return None

        # Search for facility name in text, contextText, ariaLabel
        court_name = ""
        for source in [text, el.get("contextText", ""), el.get("ariaLabel", "")]:
            match = FACILITY_RE.search(source or "")
            if match:
                court_name = match.group(1).strip()
                break

        # If no regex match, try generic name extraction from context
        if not court_name:
            context = el.get("contextText", "")
            name_match = re.search(
                r'([\w\s]+(?:Court|Ct|Field|Room|Lane|Machine)\s*\d*)',
                context or "", re.IGNORECASE
            )
            if name_match:
                court_name = name_match.group(1).strip()

        if not court_name:
            return None

        return {
            "date": target_date.isoformat(),
            "time": time_str,
            "court_name": court_name,
            "day_of_week": target_date.strftime("%A"),
            "duration_minutes": 60,
            "raw": {"source": "broad_dom_scan"},
        }

    # ── Text extraction fallback ─────────────────────────────────────

    def _extract_times_from_text(self, text: str, target_date: date) -> list[dict]:
        """Extract time slots from raw page text using regex patterns."""
        slots = []
        patterns = [
            r'(\d{1,2}:\d{2}\s*(?:AM|PM|am|pm))\s*[-\u2013]\s*(?:available|open|book)',
            r'(?:available|open)\s*[-\u2013:]\s*(\d{1,2}:\d{2}\s*(?:AM|PM|am|pm))',
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                time_str = match.group(1)
                # Look for court name near the time match (wider context)
                context = text[max(0, match.start() - 300):match.end() + 300]
                court_match = FACILITY_RE.search(context)
                # REQUIRE facility name for text-extracted results
                if not court_match:
                    logger.debug("Text extraction: skipping time %s (no facility name nearby)", time_str)
                    continue
                court_name = court_match.group(1)
                slots.append({
                    "date": target_date.isoformat(),
                    "time": time_str.strip(),
                    "court_name": court_name,
                    "day_of_week": target_date.strftime("%A"),
                    "duration_minutes": 60,
                    "raw": {"source": "text_extraction", "match": time_str},
                })
        return slots

    # ── Redux state extraction ───────────────────────────────────────

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

    # ── API response parsing ─────────────────────────────────────────

    def _parse_single_response(self, resp: dict) -> list[dict]:
        """Parse a single captured API response for slot data."""
        data = resp.get("data")
        if not data:
            return []
        fast = self._parse_response_fast(data)
        if fast:
            return fast
        return self._deep_extract_slots(data)

    def _parse_captured_responses(self) -> list[dict]:
        """Parse captured API responses for availability data."""
        slots = []

        for resp in self.captured_responses:
            data = resp.get("data")
            if not data:
                continue

            # Fast path: known field names in flat list structures
            fast_slots = self._parse_response_fast(data)
            if fast_slots:
                logger.info(
                    "Fast-path parsed %d slots from %s",
                    len(fast_slots), resp.get("url", "?"),
                )
                slots.extend(fast_slots)
            else:
                # Fallback: deep recursive extraction
                deep_slots = self._deep_extract_slots(data)
                if deep_slots:
                    logger.info(
                        "Deep extraction found %d potential slots from %s",
                        len(deep_slots), resp.get("url", "?"),
                    )
                    slots.extend(deep_slots)

        logger.info(
            "API parsing: %d total slots from %d captured responses",
            len(slots), len(self.captured_responses),
        )

        # Save extraction diagnostics
        if self.captured_responses:
            self._save_diag_json("api_extraction.json", {
                "total_responses": len(self.captured_responses),
                "total_slots_found": len(slots),
                "slots": [
                    {"time": s["time"], "date": s["date"], "court_name": s["court_name"]}
                    for s in slots[:100]
                ],
            })

        return slots

    def _parse_response_fast(self, data) -> list[dict]:
        """Fast path: parse API response with known field name conventions."""
        slots = []

        items = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            for key in ["data", "results", "items", "slots", "schedules",
                        "availability", "facilities", "timeSlots"]:
                if key in data and isinstance(data[key], list):
                    items = data[key]
                    break

        for item in items:
            if not isinstance(item, dict):
                continue
            time_val = (
                item.get("time") or item.get("startTime") or
                item.get("start_time") or item.get("timeSlot") or ""
            )
            date_val = (
                item.get("date") or item.get("startDate") or
                item.get("start_date") or ""
            )
            available = item.get("available", item.get("isAvailable", True))

            court_name_val = str(
                item.get("facility", item.get("court", item.get("name", "")))
            ).strip()
            if not court_name_val:
                continue

            if time_val and available:
                slots.append({
                    "date": str(date_val),
                    "time": str(time_val),
                    "court_name": court_name_val,
                    "day_of_week": "",
                    "duration_minutes": item.get("duration", 60),
                    "raw": item,
                })

        return slots

    def _deep_extract_slots(self, data, path: str = "root", depth: int = 0) -> list[dict]:
        """Recursively search JSON for objects that look like availability data.

        Instead of requiring specific field names, look for dicts containing
        time-like values, date-like values, and facility/resource name strings.
        """
        if depth > 10:
            return []

        slots = []

        if isinstance(data, dict):
            # Check if THIS dict looks like a slot
            time_val = None
            date_val = None
            name_val = None
            available = True

            for key, value in data.items():
                val_str = str(value).strip()
                key_lower = key.lower()

                # Time detection
                if time_val is None and _TIME_RE.search(val_str) and len(val_str) < 30:
                    time_val = val_str

                # Date detection
                if date_val is None and _DATE_RE.search(val_str) and len(val_str) < 30:
                    date_val = val_str

                # Facility name detection
                name_keys = ["name", "facility", "resource", "court", "location",
                             "resourcename", "facilityname", "description", "title"]
                if name_val is None and any(nk in key_lower for nk in name_keys):
                    if isinstance(value, str) and value.strip():
                        name_val = value.strip()

                # Availability detection
                avail_keys = ["available", "isavailable", "status", "bookable"]
                if any(ak in key_lower for ak in avail_keys):
                    if isinstance(value, bool):
                        available = value
                    elif isinstance(value, str):
                        available = value.lower() not in (
                            "false", "unavailable", "booked", "closed"
                        )

            if time_val and available:
                slots.append({
                    "date": date_val or "",
                    "time": time_val,
                    "court_name": name_val or "",
                    "day_of_week": "",
                    "duration_minutes": 60,
                    "raw": {"source": "deep_extraction", "path": path},
                })

            # Recurse into all values
            for key, value in data.items():
                if isinstance(value, (dict, list)):
                    slots.extend(
                        self._deep_extract_slots(value, f"{path}.{key}", depth + 1)
                    )

        elif isinstance(data, list):
            for i, item in enumerate(data):
                if isinstance(item, (dict, list)):
                    slots.extend(
                        self._deep_extract_slots(item, f"{path}[{i}]", depth + 1)
                    )

        return slots

    # ── Helpers ──────────────────────────────────────────────────────

    def _get_target_dates(self) -> list[date]:
        """Get the list of dates to check (next N days)."""
        today = date.today()
        return [today + timedelta(days=i) for i in range(1, self.settings.days_ahead + 1)]

    def get_api_context(self) -> dict:
        """Return discovered API endpoints, cookies, and headers for the lightweight poller.

        Call this after check_availability() completes. The returned dict contains
        everything needed to replay the API calls without a browser.
        """
        endpoints = []
        for resp in self.captured_responses:
            url = resp["url"]
            data = resp.get("data")
            # Check if response looks like it contains slot/availability data
            data_str = json.dumps(data)[:2000].lower() if data else ""
            has_slots = any(k in data_str for k in [
                "timeslot", "starttime", "start_time", "available",
                "schedule", "facility", "court",
            ])
            endpoints.append({
                "url": url,
                "method": "GET",
                "headers": self.captured_request_headers.get(url, {}),
                "has_slot_data": has_slots,
            })

        return {
            "endpoints": endpoints,
            "cookies": self._browser_cookies,
            "discovered_at": dt.utcnow().isoformat(),
        }
