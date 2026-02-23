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

# Facility reservation URL — the actual court booking path.
# mcfetridgesportscenter.com "Book Court Time" links to
# apm.activecommunities.com/chicagoparkdistrict/Reserve_Options which
# redirects here.  Note: /reservation/quick returns 404 as of Feb 2026.
QUICK_RESERVE_URL = (
    "https://anc.apm.activecommunities.com/chicagoparkdistrict/"
    "reservation?onlineSiteId=0&from_original_cui=true&locale=en-US"
)

# Activity search — fallback; searches for tennis court time activities
ACTIVITY_SEARCH_URL = (
    "https://anc.apm.activecommunities.com/chicagoparkdistrict/"
    "activity/search?onlineSiteId=0&locale=en-US"
    "&activity_select_param=2&activity_keyword=tennis+court+time+mcfetridge"
    "&viewMode=list"
)

# Broader activity search — second fallback with just McFetridge keyword
ACTIVITY_SEARCH_BROAD_URL = (
    "https://anc.apm.activecommunities.com/chicagoparkdistrict/"
    "activity/search?onlineSiteId=0&locale=en-US"
    "&activity_select_param=2&activity_keyword=mcfetridge"
    "&viewMode=list"
)

# Facility name regex — only matches known McFetridge facility patterns.
# Intentionally strict to avoid matching garbled SPA text or unrelated rooms.
FACILITY_RE = re.compile(
    r'((?:McFetridge\s+)?(?:Tennis\s+(?:Ct|Court)\s*\d+|'
    r'Pickleball\s+(?:Ct|Court)\s*\d*|'
    r'Ball\s+Machine\s*\d*|'
    r'McFetridge\s+(?:Ct|Court)\s*\d+))',
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
                # Log response summary
                body_str = json.dumps(body, default=str)[:2000]
                logger.info(
                    "Captured API response: %s (type=%s, size=%d, preview=%.500s)",
                    url, type(body).__name__, len(json.dumps(body, default=str)),
                    body_str,
                )
                # Extra diagnostics for availability grid endpoint
                if "quickreservation" in url_lower and "availability" in url_lower:
                    logger.info(
                        "AVAILABILITY API STRUCTURE: %s",
                        self._describe_structure(body),
                    )
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
            logger.info("Launching Chromium browser...")
            browser = await p.chromium.launch(
                headless=not self.settings.debug_headed,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-extensions",
                    "--disable-background-networking",
                    "--single-process",
                ],
            )
            logger.info("Browser launched, creating context...")
            context = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            logger.info("Context created, opening page...")
            page = await context.new_page()
            logger.info("Page ready, starting scrape")
            page.on("request", self._on_request)
            page.on("response", self._on_response)

            try:
                # Strategy 1: Quick Reserve page (real booking path)
                all_slots = await self._check_quick_reserve(page)

                # Strategy 2: Activity search with tennis keywords (fallback)
                if not all_slots:
                    logger.info(
                        "Quick Reserve found 0 slots, trying activity search..."
                    )
                    all_slots = await self._check_activity_search(page)

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

    # ── Quick Reserve page ───────────────────────────────────────────

    async def _check_quick_reserve(self, page: Page) -> list[dict]:
        """Navigate the Quick Reserve page — the real court booking path.

        This is the path real users take:
        mcfetridgesportscenter.com → "Make a Reservation" → ActiveNet Quick Reserve.
        The page shows a facility search where the user selects McFetridge tennis
        courts, then picks a date and time.
        """
        logger.info("Checking Quick Reserve page: %s", QUICK_RESERVE_URL)
        await page.goto(QUICK_RESERVE_URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_load_state("networkidle", timeout=30000)
        await asyncio.sleep(4)

        await self._save_diag(page, "quick_reserve_loaded")

        # Log the current URL (the SPA may have navigated internally)
        current_url = page.url
        logger.info("Quick Reserve page URL after load: %s", current_url)

        # Dump page body text (first 3000 chars) for diagnostics
        try:
            body_text = await page.inner_text("body")
            logger.info(
                "Quick Reserve body text (first 3000 chars): %s",
                body_text[:3000].replace("\n", " | "),
            )
        except Exception as e:
            logger.warning("Could not read body text: %s", e)

        # Log all network URLs captured so far
        logger.info(
            "Network URLs after Quick Reserve load (%d): %s",
            len(self.all_network_urls),
            json.dumps([u for u in self.all_network_urls if "activecommunities" in u], indent=2)[:3000],
        )

        # Try to select McFetridge / Tennis from whatever UI is presented
        await self._try_select_facility(page)
        await asyncio.sleep(3)
        await page.wait_for_load_state("networkidle", timeout=15000)

        # Initialize date tracking — the grid loads with today's date
        self._current_grid_date = date.today()

        await self._save_diag(page, "after_facility_select")

        slots = []
        total_api_slots = 0

        # Extract resource names from the page
        resource_names = await self._extract_resource_names(page)
        logger.info(
            "Found %d resources on Quick Reserve page: %s",
            len(resource_names), resource_names,
        )

        # Iterate through target dates
        target_dates = self._get_target_dates()

        # Process initial API responses (captured during facility selection)
        # with the first target date. When date navigation works, each date
        # change will trigger a new API response that gets its own date.
        if self.captured_responses and target_dates:
            initial_api_slots = self._parse_captured_responses(
                current_date=target_dates[0],
                responses=self.captured_responses,
            )
            if initial_api_slots:
                slots.extend(initial_api_slots)
                total_api_slots += len(initial_api_slots)

        # Track how many API responses we've already processed
        api_responses_processed = len(self.captured_responses)

        for target_date in target_dates:
            logger.info("Checking date: %s", target_date.isoformat())
            date_changed = await self._try_select_date(page, target_date)
            if date_changed:
                await page.wait_for_load_state("networkidle", timeout=15000)
                await asyncio.sleep(2)

            await self._save_diag(page, f"date_{target_date.isoformat()}")

            # Parse any NEW captured API responses with this date
            new_responses = self.captured_responses[api_responses_processed:]
            if new_responses:
                api_slots = self._parse_captured_responses(
                    current_date=target_date,
                    responses=new_responses,
                )
                if api_slots:
                    slots.extend(api_slots)
                    total_api_slots += len(api_slots)
                api_responses_processed = len(self.captured_responses)

            # Extract available slots from DOM
            page_slots = await self._extract_slots_from_dom(page, target_date)

            # Enrich slots with resource names from the page if needed
            for slot in page_slots:
                court = slot.get("court_name", "")
                if not court or len(court) < 8:
                    matched = self._match_slot_to_resource(slot, resource_names)
                    if matched:
                        slot["court_name"] = matched

            slots.extend(page_slots)

        await self._dump_dom_structure(page, "dom_quick_reserve_final")

        logger.info(
            "QUICK RESERVE SUMMARY: captured_responses=%d, network_urls=%d, "
            "dom_slots=%d, api_slots=%d, total=%d, resources=%d",
            len(self.captured_responses), len(self.all_network_urls),
            len(slots) - total_api_slots, total_api_slots, len(slots),
            len(resource_names),
        )

        return slots

    # ── Activity search (fallback) ───────────────────────────────────

    async def _check_activity_search(self, page: Page) -> list[dict]:
        """Search for tennis court time activities.

        Fallback strategy: search ActiveNet's activity listing for tennis
        court time at McFetridge.  First tries a targeted search for
        'tennis court time mcfetridge', then a broader search.
        """
        slots = []

        for label, url in [
            ("tennis+mcfetridge", ACTIVITY_SEARCH_URL),
            ("mcfetridge (broad)", ACTIVITY_SEARCH_BROAD_URL),
        ]:
            if slots:
                break  # Already found results with previous search

            logger.info("Activity search [%s]: %s", label, url)
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_load_state("networkidle", timeout=30000)
            await asyncio.sleep(5)

            await self._save_diag(page, f"activity_search_{label}")

            # Log page body text for debugging
            try:
                body_text = await page.inner_text("body")
                logger.info(
                    "Activity search [%s] body text (first 2000 chars): %s",
                    label, body_text[:2000].replace("\n", " | "),
                )
            except Exception:
                pass

            # Find activity links — try multiple selector patterns
            activity_links = await page.evaluate("""
                () => {
                    const links = [];
                    // Pattern 1: detail links
                    document.querySelectorAll(
                        'a[href*="/activity/search/detail/"]'
                    ).forEach(el => {
                        links.push({
                            href: el.href,
                            text: (el.textContent || '').trim().substring(0, 200),
                        });
                    });
                    // Pattern 2: activity links (broader)
                    if (links.length === 0) {
                        document.querySelectorAll(
                            'a[href*="/activity/"], a[href*="/Activity_Search/"]'
                        ).forEach(el => {
                            links.push({
                                href: el.href,
                                text: (el.textContent || '').trim().substring(0, 200),
                            });
                        });
                    }
                    return links;
                }
            """)

            logger.info(
                "Activity search [%s]: found %d activity links",
                label, len(activity_links),
            )

            # Prioritize tennis-related links
            def _tennis_score(link):
                text = (link.get("text", "") or "").lower()
                if "tennis" in text and "ct" in text:
                    return 0
                if "court time" in text:
                    return 1
                if "tennis" in text:
                    return 2
                return 3

            activity_links.sort(key=_tennis_score)
            self._save_diag_json(f"activity_links_{label}.json", activity_links)

            # Visit each activity detail page (up to 15)
            for link in activity_links[:15]:
                href = link.get("href", "")
                name = link.get("text", "").strip()
                if not href or not href.startswith("http"):
                    continue

                # Skip obviously non-tennis activities
                name_lower = name.lower()
                if any(skip in name_lower for skip in [
                    "music", "dance", "gymnastics", "swimming", "hockey",
                    "skating", "soccer", "basketball", "yoga", "fitness",
                    "camp", "cooking", "art",
                ]):
                    logger.info("Skipping non-tennis activity: %s", name[:60])
                    continue

                logger.info("Checking activity: %s", name[:80])
                responses_before = len(self.captured_responses)

                try:
                    await page.goto(
                        href, wait_until="domcontentloaded", timeout=30000,
                    )
                    await page.wait_for_load_state("networkidle", timeout=30000)
                    await asyncio.sleep(3)

                    # Extract court name from the activity detail page
                    court_name = await self._extract_court_name_from_detail(
                        page, name,
                    )
                    logger.info(
                        "COURT_NAME: link_text=%r → extracted=%r",
                        name[:60], court_name,
                    )

                    # Extract time slots for each target date
                    target_dates = self._get_target_dates()
                    for td in target_dates:
                        page_slots = await self._extract_slots_from_dom(page, td)
                        for s in page_slots:
                            if court_name and (
                                not s.get("court_name")
                                or len(s["court_name"]) < 8
                            ):
                                s["court_name"] = court_name
                        slots.extend(page_slots)

                    # Parse API responses captured during this page load
                    new_responses = self.captured_responses[responses_before:]
                    for resp in new_responses:
                        per_activity_slots = self._parse_single_response(resp)
                        for s in per_activity_slots:
                            if court_name and (
                                not s.get("court_name")
                                or len(s["court_name"]) < 8
                            ):
                                s["court_name"] = court_name
                        slots.extend(per_activity_slots)

                except Exception as e:
                    logger.warning(
                        "Error loading activity %s: %s", name[:50], e,
                    )

        logger.info("Activity search total: %d raw slots found", len(slots))
        return slots

    async def _extract_court_name_from_detail(
        self, page: Page, link_text: str,
    ) -> str:
        """Extract a clean court/facility name from an activity detail page."""
        # Try the page heading first
        heading = await page.evaluate("""
            () => {
                const h = document.querySelector(
                    'h1, h2, [class*="activity-name"], '
                    + '[class*="activityName"], [class*="title"]'
                );
                return h ? h.textContent.trim() : '';
            }
        """)

        # Try to extract from API responses (most reliable)
        for resp in reversed(self.captured_responses[-4:]):
            data = resp.get("data")
            if isinstance(data, dict):
                body = data.get("body", data)
                if isinstance(body, dict):
                    api_name = (
                        body.get("activityName", "")
                        or body.get("name", "")
                        or body.get("description", "")
                    )
                    if api_name:
                        match = FACILITY_RE.search(api_name)
                        if match:
                            return match.group(1)

        # Try heading text
        for candidate in [heading, link_text]:
            if candidate:
                match = FACILITY_RE.search(candidate)
                if match:
                    return match.group(1)

        return link_text.strip()[:80]

    # ── Facility selection ───────────────────────────────────────────

    async def _try_select_facility(self, page: Page):
        """Try to search/select McFetridge tennis courts on the Quick Reserve page."""
        # Strategy A: Fill search/filter inputs
        search_selectors = [
            "input[type='search']",
            "input[type='text'][placeholder*='search' i]",
            "input[type='text'][placeholder*='facility' i]",
            "input[type='text'][placeholder*='location' i]",
            "input[name*='search' i]",
            "input[name*='filter' i]",
            "input[id*='search' i]",
            "input[class*='search' i]",
        ]
        for sel in search_selectors:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    await el.fill("McFetridge Tennis")
                    await el.press("Enter")
                    logger.info("Filled search input: %s", sel)
                    await asyncio.sleep(2)
                    return
            except Exception:
                continue

        # Strategy B: Click on text links/buttons
        text_targets = [
            "text=Tennis",
            "text=McFetridge",
            "text=Court Time",
            "text=Quick Reserve",
            "a:has-text('Tennis')",
            "button:has-text('Tennis')",
            "a:has-text('McFetridge')",
            "button:has-text('McFetridge')",
            "[role='option']:has-text('Tennis')",
            "[role='listitem']:has-text('Tennis')",
        ]
        for sel in text_targets:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    await el.click()
                    logger.info("Clicked facility selector: %s", sel)
                    await asyncio.sleep(2)
                    return
            except Exception:
                continue

        # Strategy C: Select from dropdowns
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
                            logger.info(
                                "Selected from dropdown: %s", text,
                            )
                            return
            except Exception:
                continue

        logger.warning("Could not find facility selector on Quick Reserve page")

    # ── Resource extraction ─────────────────────────────────────────

    async def _extract_resource_names(self, page: Page) -> list[str]:
        """Extract resource/facility names from the quick reservation page.

        The ActiveNet quick reservation page displays resources in a list
        or grid. Resource names from this page are already properly formatted
        (e.g. "McFetridge Tennis Ct01").
        """
        resource_names = await page.evaluate("""
            () => {
                const names = new Set();
                const courtPattern = /McFetridge|Tennis|Pickleball|Ball\\s*Machine|Clubroom/i;
                const detailedPattern = /(?:McFetridge\\s+)?(?:Tennis\\s+(?:Ct|Court)\\s*\\d+|Pickleball\\s+(?:Ct|Court)\\s*\\d*|Ball\\s+Machine\\s*\\d*)/i;

                // Strategy A: Look for resource/facility labeled elements
                const resourceSelectors = [
                    '[class*="resource"] [class*="name"]',
                    '[class*="facility"] [class*="name"]',
                    '[class*="resource-name"]',
                    '[class*="facility-name"]',
                    '[class*="resource-label"]',
                    '[class*="resource-title"]',
                    '[class*="lane-name"]',
                    '[class*="room-name"]',
                    '[class*="booking-resource"]',
                    '[class*="reservation-resource"]',
                    'th[class*="resource"]',
                    'td[class*="resource-header"]',
                ];

                for (const sel of resourceSelectors) {
                    document.querySelectorAll(sel).forEach(el => {
                        const text = (el.textContent || '').trim();
                        if (text.length > 5 && text.length < 100 && courtPattern.test(text)) {
                            names.add(text);
                        }
                    });
                }

                // Strategy B: Look for table headers / row labels
                if (names.size === 0) {
                    document.querySelectorAll(
                        'th, td:first-child, [role="rowheader"], [role="columnheader"]'
                    ).forEach(el => {
                        const text = (el.textContent || '').trim();
                        if (text.length > 5 && text.length < 100 && courtPattern.test(text)) {
                            names.add(text);
                        }
                    });
                }

                // Strategy C: Broader scan for text matching resource patterns
                if (names.size === 0) {
                    document.querySelectorAll('div, span, label, a, button, li').forEach(el => {
                        const text = (el.textContent || '').trim();
                        if (text.length > 5 && text.length < 80) {
                            const match = text.match(detailedPattern);
                            if (match) {
                                names.add(match[0]);
                            }
                        }
                    });
                }

                return Array.from(names);
            }
        """)

        # Also try to extract resource names from captured API responses
        for resp in self.captured_responses:
            data = resp.get("data")
            if not isinstance(data, dict):
                continue
            for key in ["resources", "facilities", "items", "data"]:
                items = data.get(key)
                if isinstance(items, list):
                    for item in items:
                        if isinstance(item, dict):
                            name = str(
                                item.get("resourceName",
                                         item.get("name",
                                                   item.get("facilityName", "")))
                            ).strip()
                            if name and FACILITY_RE.search(name):
                                resource_names.append(name)

        # Deduplicate while preserving order
        seen = set()
        unique_names = []
        for name in resource_names:
            if name not in seen:
                seen.add(name)
                unique_names.append(name)

        self._save_diag_json("resource_names.json", unique_names)
        return unique_names

    def _match_slot_to_resource(
        self, slot: dict, resource_names: list[str]
    ) -> str:
        """Try to match a slot to a known resource name from the page.

        Uses the slot's raw data (context text, parent text, aria label)
        to identify which resource the slot belongs to.
        """
        original = slot.get("court_name", "").strip()
        raw = slot.get("raw", {})

        # Check if original already matches a known resource
        if original:
            for name in resource_names:
                if name.lower() in original.lower() or original.lower() in name.lower():
                    return name

        # Check contextText / parentText for resource name matches
        for text_key in ["contextText", "parentText"]:
            context = str(raw.get(text_key, ""))
            for name in resource_names:
                if name in context:
                    return name

        # Check ariaLabel
        aria = str(raw.get("ariaLabel", ""))
        for name in resource_names:
            if name.lower() in aria.lower():
                return name

        return ""

    # ── Date selection ───────────────────────────────────────────────

    async def _try_select_date(self, page: Page, target_date: date) -> bool:
        """Try to select a specific date in the ActiveNet Quick Reserve calendar.

        ActiveNet uses an `an-date-picker` component with:
        - A text input: aria-label="Date picker, current date"
          value format: "Mon, Feb 23, 2026"
        - A popper/dropdown calendar: .an-date-picker__popper
        - Container: .an-date-picker.quick-rez__date-picker
        """
        date_iso = target_date.isoformat()

        # Format date to match ActiveNet's display format: "Tue, Feb 24, 2026"
        date_display = target_date.strftime("%a, %b ") + str(target_date.day) + target_date.strftime(", %Y")

        # First time only: log what date-related elements exist on the page
        if not hasattr(self, "_date_picker_logged"):
            self._date_picker_logged = True
            try:
                date_elements = await page.evaluate("""
                    () => {
                        const results = [];
                        const selectors = [
                            'input[aria-label*="date" i]',
                            '[class*="date-picker" i]',
                            '[class*="an-date" i]',
                            '[class*="calendar" i]',
                            'button[class*="arrow" i]',
                            'button[class*="chevron" i]',
                            'button[class*="prev" i]',
                            'button[class*="next" i]',
                        ];
                        for (const sel of selectors) {
                            document.querySelectorAll(sel).forEach(el => {
                                results.push({
                                    selector: sel,
                                    tag: el.tagName,
                                    type: el.type || '',
                                    className: (el.className || '').substring(0, 200),
                                    id: el.id || '',
                                    value: (el.value || '').substring(0, 50),
                                    text: (el.textContent || '').trim().substring(0, 100),
                                    ariaLabel: el.getAttribute('aria-label') || '',
                                    visible: el.offsetParent !== null,
                                });
                            });
                        }
                        return results;
                    }
                """)
                if date_elements:
                    logger.info(
                        "Date picker elements found (%d): %s",
                        len(date_elements),
                        json.dumps(date_elements[:20], indent=2)[:3000],
                    )
                else:
                    logger.info("No date picker elements found")
            except Exception as e:
                logger.warning("Date picker diagnostics error: %s", e)

        # Strategy 1: ActiveNet date input with aria-label
        # The input shows "Mon, Feb 23, 2026" and has aria-label="Date picker, current date"
        date_input_sel = 'input[aria-label="Date picker, current date"]'
        try:
            el = await page.query_selector(date_input_sel)
            if el and await el.is_visible():
                current_val = await el.get_attribute("value") or ""
                logger.info(
                    "Found date picker input: current='%s', target='%s'",
                    current_val, date_display,
                )

                # Triple-click to select all text, then type new date
                await el.click(click_count=3)
                await asyncio.sleep(0.3)
                await el.fill(date_display)
                await asyncio.sleep(0.3)
                await el.press("Enter")
                logger.info("Filled date picker with: %s", date_display)

                # Wait for the SPA to react and fire a new availability API call
                await asyncio.sleep(2)
                try:
                    await page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass

                # Verify the date actually changed
                new_val = await el.get_attribute("value") or ""
                if new_val != current_val:
                    logger.info("Date picker value changed to: %s", new_val)
                    self._current_grid_date = target_date
                    return True
                else:
                    logger.warning(
                        "Date picker value unchanged after fill (still '%s'). "
                        "Trying keyboard input...", new_val,
                    )

                    # Fallback: use keyboard to type character by character
                    # (some React inputs don't respond to fill())
                    await el.click(click_count=3)
                    await asyncio.sleep(0.2)
                    await page.keyboard.press("Backspace")
                    await asyncio.sleep(0.1)
                    await page.keyboard.type(date_display, delay=50)
                    await asyncio.sleep(0.3)
                    await page.keyboard.press("Enter")
                    await asyncio.sleep(2)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=10000)
                    except Exception:
                        pass

                    new_val2 = await el.get_attribute("value") or ""
                    if new_val2 != current_val:
                        logger.info("Date changed via keyboard to: %s", new_val2)
                        self._current_grid_date = target_date
                        return True
                    else:
                        logger.warning("Keyboard input also failed to change date")
        except Exception as e:
            logger.warning("Date picker input strategy failed: %s", e)

        # Strategy 2: Click input to open calendar popup, then click target day
        try:
            el = await page.query_selector(date_input_sel)
            if el and await el.is_visible():
                await el.click()
                await asyncio.sleep(1)

                # Look for the calendar popup and clickable day cells
                # ActiveNet calendar days typically have data attributes or aria-labels
                day_selectors = [
                    f".an-date-picker__popper [data-date='{date_iso}']",
                    f".an-date-picker__popper td[data-day='{target_date.day}']",
                    f".an-date-picker__popper [aria-label*='{target_date.strftime('%B')} {target_date.day}']",
                    f".an-date-picker__popper button:has-text('{target_date.day}')",
                    f".an-date-picker__popper td:has-text('{target_date.day}')",
                ]
                for sel in day_selectors:
                    try:
                        day_el = await page.query_selector(sel)
                        if day_el and await day_el.is_visible():
                            await day_el.click()
                            logger.info("Clicked calendar day via popup: %s", sel)
                            await asyncio.sleep(2)
                            try:
                                await page.wait_for_load_state("networkidle", timeout=10000)
                            except Exception:
                                pass
                            self._current_grid_date = target_date
                            return True
                    except Exception:
                        continue

                # Close the popup if nothing was clicked
                await page.keyboard.press("Escape")
        except Exception as e:
            logger.debug("Calendar popup strategy failed: %s", e)

        logger.warning("Could not change date to %s — no matching UI element found", date_iso)
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

        # Strategy 2c: ActiveNet Quick Reserve grid scan.
        # The grid uses: .resource-header-cell__title for resource names,
        # td.td-grid-cell--disabled for unavailable cells (gray),
        # td.td-grid-cell (without --disabled) for available cells (white).
        # Time headers are in thead th .header-cell elements.
        strategy_counts["grid_dom"] = 0
        try:
            grid_slots = await page.evaluate("""
                () => {
                    const results = [];

                    // ActiveNet Quick Reserve grid container
                    const grid = document.querySelector(
                        '.an-resource-grid, [data-qa-id="resource-grid-view-container"]'
                    );
                    if (!grid) return results;

                    const table = grid.querySelector('table');
                    if (!table) return results;

                    // Extract time slot headers from <thead>
                    const timeHeaders = [];
                    table.querySelectorAll('thead th .header-cell, thead th').forEach(th => {
                        const text = (th.textContent || '').trim();
                        const m = text.match(/\\d{1,2}:\\d{2}\\s*(?:AM|PM)/i);
                        if (m) timeHeaders.push(m[0]);
                    });

                    if (timeHeaders.length < 3) return results;

                    // Process each resource row in <tbody>
                    const rows = table.querySelectorAll('tbody tr, tbody [role="row"]');
                    rows.forEach((row, rowIdx) => {
                        // Resource name: target the most specific element first
                        // to avoid picking up junk from sibling elements (type tags,
                        // selection state text like "Unselected", etc.)
                        let resourceName = '';
                        const titleEl = row.querySelector('.resource-header-cell__title');
                        if (titleEl) {
                            // Use innerText to skip hidden content; fallback to textContent
                            resourceName = (titleEl.innerText || titleEl.textContent || '').trim();
                        }
                        if (!resourceName) {
                            const nameEl = row.querySelector('.resource-header-cell__name');
                            if (nameEl) {
                                resourceName = (nameEl.innerText || nameEl.textContent || '').trim();
                            }
                        }
                        if (!resourceName) {
                            // Last resort: th text, but strip known junk
                            const th = row.querySelector('th.table-sticky-left');
                            if (th) {
                                resourceName = (th.innerText || th.textContent || '').trim();
                            }
                        }
                        // Strip ActiveNet prefix junk: "Unselected"/"Selected" state
                        // and single-char type tags (E/F/etc.) that leak from sibling elements
                        resourceName = resourceName
                            .replace(/^(?:Un)?[Ss]elected/i, '')
                            .replace(/^[A-Z](?=[A-Z][a-z])/, '')
                            .trim();
                        if (!resourceName) return;

                        // Get all td cells (excluding the th header cell)
                        const cells = row.querySelectorAll('td.td-grid-cell');

                        cells.forEach((cell, colIdx) => {
                            if (colIdx >= timeHeaders.length) return;

                            const cls = (cell.className || '').toLowerCase();
                            // ActiveNet: --disabled class = unavailable (gray)
                            // Absence of --disabled = available (white)
                            const isDisabled = cls.includes('td-grid-cell--disabled');

                            if (!isDisabled) {
                                results.push({
                                    resourceName: resourceName.substring(0, 100),
                                    time: timeHeaders[colIdx],
                                    rowIndex: rowIdx,
                                });
                            }
                        });
                    });

                    return results;
                }
            """)

            if grid_slots:
                logger.info("Grid-aware DOM scan found %d available cells", len(grid_slots))
                sample_names = sorted(set(
                    c.get("resourceName", "")[:60] for c in grid_slots[:50]
                ))
                logger.info("Grid DOM resourceNames: %s", sample_names)
                self._save_diag_json(
                    f"grid_dom_{target_date.isoformat()}.json", grid_slots
                )

                for cell in grid_slots:
                    court_name = cell.get("resourceName", "")
                    time_str = cell.get("time", "")
                    if court_name and time_str:
                        time_match = re.search(
                            r'(\d{1,2}):(\d{2})\s*(AM|PM|am|pm)', time_str
                        )
                        if time_match:
                            hour = int(time_match.group(1))
                            minute = int(time_match.group(2))
                            ampm = time_match.group(3).upper()
                            if ampm == "PM" and hour != 12:
                                hour += 12
                            elif ampm == "AM" and hour == 12:
                                hour = 0
                            slots.append({
                                "date": target_date.isoformat(),
                                "time": f"{hour:02d}:{minute:02d}",
                                "court_name": court_name,
                                "day_of_week": target_date.strftime("%A"),
                                "duration_minutes": 60,
                                "raw": {"source": "grid_dom_scan"},
                            })
                            strategy_counts["grid_dom"] += 1
        except Exception as e:
            logger.warning("Grid-aware DOM scan error: %s", e)

        # Strategy 2b: Broad DOM scan — find ALL elements with time text
        # Only run as last resort if grid-aware scan found nothing
        if not strategy_counts.get("grid_dom"):
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

        # Post-processing safety net: if all broad_dom slots have the same
        # court_name, they are likely column headers misidentified as slots.
        if strategy_counts["broad_dom"] > 0:
            broad_court_names = {
                s["court_name"] for s in slots
                if s.get("raw", {}).get("source") == "broad_dom_scan"
            }
            if len(broad_court_names) == 1 and strategy_counts["broad_dom"] > 3:
                logger.warning(
                    "Broad DOM safety net: all %d broad slots have same court_name='%s' — "
                    "likely column headers, discarding",
                    strategy_counts["broad_dom"], broad_court_names.pop(),
                )
                slots = [s for s in slots if s.get("raw", {}).get("source") != "broad_dom_scan"]
                strategy_counts["broad_dom"] = 0

        logger.info(
            "DOM extraction for %s: redux=%d targeted=%d grid=%d broad=%d text=%d total=%d",
            target_date.isoformat(),
            strategy_counts["redux"], strategy_counts["targeted_dom"],
            strategy_counts["grid_dom"], strategy_counts["broad_dom"],
            strategy_counts["text"], len(slots),
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
        """Parse DOM element with broader facility name matching (Strategy 2b).

        Tightened to avoid false positives:
        - Rejects header elements (th, class*=header)
        - Rejects container elements (contextText with 3+ distinct facilities)
        - Only uses FACILITY_RE for court name extraction (no generic fallback)
        """
        text = el.get("text", "")
        if not text:
            return None

        # Reject header elements — these are column/row headers, not cells
        tag = (el.get("tag", "") or "").upper()
        class_name = (el.get("className", "") or "").lower()
        if tag == "TH" or "header" in class_name or "column-header" in class_name:
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
        if any(x in class_name for x in ["unavailable", "booked", "disabled", "closed"]):
            return None

        # Reject container elements: if contextText contains 3+ distinct
        # facility names, this element is a container (e.g. grid wrapper),
        # not a specific availability cell.
        context = el.get("contextText", "") or ""
        context_facilities = FACILITY_RE.findall(context)
        # Deduplicate
        unique_facilities = set(f.strip().lower() for f in context_facilities)
        if len(unique_facilities) > 2:
            return None

        # Search for facility name in text, contextText, ariaLabel
        # Only use FACILITY_RE — no generic fallback that matches
        # Pickleball/Ball Machine/Field/Room/Lane
        court_name = ""
        for source in [text, context, el.get("ariaLabel", "")]:
            match = FACILITY_RE.search(source or "")
            if match:
                court_name = match.group(1).strip()
                break

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

    def _parse_captured_responses(
        self,
        current_date: date | None = None,
        responses: list[dict] | None = None,
    ) -> list[dict]:
        """Parse captured API responses for availability data."""
        slots = []
        if current_date is None:
            current_date = date.today() + timedelta(days=1)

        if responses is None:
            responses = self.captured_responses

        for resp in responses:
            data = resp.get("data")
            if not data:
                continue
            url = resp.get("url", "")

            # Specialized path: Quick Reserve availability grid
            if "quickreservation" in url.lower() and "availability" in url.lower():
                grid_slots = self._parse_availability_grid(data, current_date)
                if grid_slots:
                    logger.info(
                        "Grid parser found %d slots from %s",
                        len(grid_slots), url,
                    )
                    slots.extend(grid_slots)
                    continue

            # Fast path: known field names in flat list structures
            fast_slots = self._parse_response_fast(data)
            if fast_slots:
                logger.info(
                    "Fast-path parsed %d slots from %s",
                    len(fast_slots), url,
                )
                slots.extend(fast_slots)
            else:
                # Fallback: deep recursive extraction
                deep_slots = self._deep_extract_slots(data)
                if deep_slots:
                    logger.info(
                        "Deep extraction found %d potential slots from %s",
                        len(deep_slots), url,
                    )
                    slots.extend(deep_slots)

        logger.info(
            "API parsing: %d total slots from %d captured responses",
            len(slots), len(responses),
        )

        # Save extraction diagnostics
        if responses:
            self._save_diag_json("api_extraction.json", {
                "total_responses": len(responses),
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

            if time_val and available and name_val:
                slots.append({
                    "date": date_val or "",
                    "time": time_val,
                    "court_name": name_val,
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

    # ── Availability grid parser ────────────────────────────────────

    def _describe_structure(self, data, depth: int = 0, max_depth: int = 4) -> str:
        """Describe the nested structure of a JSON object for diagnostics."""
        if depth >= max_depth:
            return f"({type(data).__name__})"
        if isinstance(data, dict):
            items = []
            for k, v in list(data.items())[:20]:
                items.append(f"{k}: {self._describe_structure(v, depth + 1, max_depth)}")
            return "{" + ", ".join(items) + "}"
        elif isinstance(data, list):
            if not data:
                return "[]"
            return f"[{self._describe_structure(data[0], depth + 1, max_depth)} x{len(data)}]"
        elif isinstance(data, str):
            return f'str({len(data)})'
        elif isinstance(data, bool):
            return str(data)
        elif isinstance(data, (int, float)):
            return str(data)
        else:
            return f"({type(data).__name__})"

    def _parse_availability_grid(self, data: dict, current_date: date) -> list[dict]:
        """Parse the Quick Reserve availability API response grid.

        ActiveNet /rest/reservation/quickreservation/availability returns:
        {
          "body": {
            "availability": {
              "time_slots": ["06:00:00", "07:00:00", ...],
              "time_increment": 60,
              "resources": [
                {
                  "resourceName": "McFetridge Tennis Ct01",
                  "resourceID": 123,
                  "timeSlotDetails": [
                    {"status": 0, "selected": false},  // 0=available
                    {"status": 1, "selected": false},  // 1=unavailable
                    ...
                  ]
                }, ...
              ]
            }
          }
        }

        Field names may use snake_case or camelCase depending on ActiveNet version.
        """
        slots = []

        # Navigate to body.availability (ActiveNet response wrapper)
        body = data.get("body", data)
        avail = body.get("availability", body)

        if not isinstance(avail, dict):
            logger.debug("Availability grid: no 'availability' dict found")
            return []

        # time_slots can be snake_case or camelCase
        time_slots = (
            avail.get("time_slots")
            or avail.get("timeSlots")
            or []
        )
        if not time_slots:
            logger.debug("Availability grid: no time_slots array")
            return []

        time_increment = (
            avail.get("time_increment")
            or avail.get("timeIncrement")
            or 60
        )

        logger.info(
            "Availability grid: %d time_slots, keys=%s",
            len(time_slots), sorted(avail.keys()),
        )

        resources = avail.get("resources", [])
        if not resources:
            logger.info(
                "Availability grid: no 'resources' array. Full structure: %s",
                self._describe_structure(avail, max_depth=5),
            )
            return []

        if not isinstance(resources[0], dict):
            return []

        # Log first resource structure for diagnostics
        logger.info(
            "Availability grid: resource[0] keys=%s",
            sorted(resources[0].keys()),
        )

        for res in resources:
            # Resource name: try camelCase first, then snake_case
            res_name = str(
                res.get("resourceName", "")
                or res.get("resource_name", "")
                or res.get("name", "")
            ).strip()

            # Per-time-slot availability: timeSlotDetails or time_slot_details
            details = (
                res.get("timeSlotDetails")
                or res.get("time_slot_details")
                or []
            )

            # Fallback: find ANY list with same length as time_slots
            if not details:
                for key, val in res.items():
                    if isinstance(val, list) and len(val) == len(time_slots):
                        details = val
                        logger.info(
                            "Availability grid: using '%s' as slot details for '%s'",
                            key, res_name,
                        )
                        break

            if len(details) != len(time_slots):
                continue

            for i, ts in enumerate(time_slots):
                detail = details[i]

                # Determine availability from detail
                if isinstance(detail, dict):
                    # ActiveNet uses status: 0=available, 1=unavailable
                    status = detail.get("status")
                    if status is not None:
                        is_avail = (status == 0)
                    else:
                        # Fallback to boolean fields
                        is_avail = detail.get("available",
                                    detail.get("isAvailable", False))
                elif isinstance(detail, (int, float)):
                    is_avail = (detail == 0)
                elif isinstance(detail, bool):
                    is_avail = detail
                else:
                    continue

                if not is_avail:
                    continue

                # Parse time: "06:00:00" → "06:00"
                time_str = str(ts)
                parts = time_str.split(":")
                if len(parts) == 3:
                    time_str = f"{parts[0]}:{parts[1]}"

                slots.append({
                    "date": current_date.isoformat(),
                    "time": time_str,
                    "court_name": res_name,
                    "day_of_week": current_date.strftime("%A"),
                    "duration_minutes": time_increment,
                    "raw": {"source": "availability_grid"},
                })

        logger.info(
            "Availability grid: parsed %d available slots from %d resources "
            "(total cells=%d)",
            len(slots), len(resources), len(resources) * len(time_slots),
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
