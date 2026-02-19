"""
Lightweight HTTP-based availability poller.

Replays discovered ActiveNet API endpoints using urllib.request (no browser).
Each poll takes <1 second, enabling high-frequency checking.
"""
import json
import logging
import ssl
import urllib.request
import urllib.error
from datetime import date, timedelta
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

logger = logging.getLogger(__name__)

# Reuse the same response parsing logic as checker.py
_SLOT_WRAPPER_KEYS = [
    "data", "results", "items", "slots", "schedules",
    "availability", "facilities", "timeSlots",
]


def parse_api_response(data) -> list[dict]:
    """Parse an API JSON response into raw slot dicts.

    Same logic as AvailabilityChecker._parse_captured_responses() but operates
    on a single response body instead of the full captured list.
    """
    slots = []
    items = []

    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        for key in _SLOT_WRAPPER_KEYS:
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
        court_name = str(
            item.get("facility", item.get("court", item.get("name", "")))
        ).strip()

        if not court_name:
            continue
        if time_val and available:
            slots.append({
                "date": str(date_val),
                "time": str(time_val),
                "court_name": court_name,
                "day_of_week": "",
                "duration_minutes": item.get("duration", 60),
                "raw": item,
            })

    return slots


class APIPoller:
    """Lightweight poller that replays discovered API endpoints via HTTP."""

    def __init__(self, api_context: dict, days_ahead: int = 6):
        self.api_context = api_context
        self.days_ahead = days_ahead
        self.needs_rediscovery = False
        self._ssl_ctx = ssl.create_default_context()

    def update_context(self, api_context: dict):
        """Refresh the API context (after a new Playwright discovery scan)."""
        self.api_context = api_context
        self.needs_rediscovery = False
        logger.info(
            "API poller context updated: %d endpoints",
            len(api_context.get("endpoints", [])),
        )

    def poll(self) -> list[dict]:
        """Poll discovered endpoints for availability data.

        Returns raw slot list (same format as AvailabilityChecker.check_availability()).
        Returns empty list on failure; sets self.needs_rediscovery on auth errors.
        """
        endpoints = self.api_context.get("endpoints", [])
        cookies = self.api_context.get("cookies", {})

        # Prefer endpoints flagged as having slot data
        slot_endpoints = [e for e in endpoints if e.get("has_slot_data")]
        if not slot_endpoints:
            slot_endpoints = endpoints

        if not slot_endpoints:
            logger.debug("API poller: no endpoints available")
            self.needs_rediscovery = True
            return []

        all_slots = []
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())

        for endpoint in slot_endpoints:
            try:
                urls = self._build_date_urls(endpoint["url"])
                for url in urls:
                    headers = dict(endpoint.get("headers", {}))
                    headers.setdefault("User-Agent", (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ))
                    headers.setdefault("Accept", "application/json, text/plain, */*")
                    if cookie_str:
                        headers["Cookie"] = cookie_str

                    req = urllib.request.Request(url, headers=headers, method="GET")
                    with urllib.request.urlopen(req, timeout=15, context=self._ssl_ctx) as resp:
                        body = resp.read().decode("utf-8", errors="replace")
                        data = json.loads(body)
                        slots = parse_api_response(data)
                        all_slots.extend(slots)

            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    logger.warning("API poll got HTTP %d — session expired, need rediscovery", e.code)
                    self.needs_rediscovery = True
                    return []
                logger.debug("API poll HTTP error %d for %s", e.code, endpoint.get("url", "?"))
            except urllib.error.URLError as e:
                logger.debug("API poll URL error: %s", e.reason)
            except (json.JSONDecodeError, KeyError, OSError) as e:
                logger.debug("API poll parse/network error: %s", e)

        return all_slots

    def _build_date_urls(self, base_url: str) -> list[str]:
        """Generate URLs for each target date by modifying query parameters."""
        parsed = urlparse(base_url)
        params = parse_qs(parsed.query)

        # Look for date-like parameters
        date_params = [k for k in params if any(
            d in k.lower() for d in ["date", "start", "from", "begin"]
        )]

        if not date_params:
            return [base_url]

        today = date.today()
        target_dates = [today + timedelta(days=i) for i in range(1, self.days_ahead + 1)]

        urls = []
        for target_date in target_dates:
            new_params = dict(params)
            for dp in date_params:
                new_params[dp] = [target_date.strftime("%Y-%m-%d")]
            new_query = urlencode(new_params, doseq=True)
            new_url = urlunparse(parsed._replace(query=new_query))
            urls.append(new_url)
        return urls
