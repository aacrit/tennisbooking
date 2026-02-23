"""
Test suite for scraper data accuracy validation.

12 test scenarios covering:
- Parser time/date parsing (tests 1-3)
- Filter: court name enforcement, weekday/weekend rules, dedup (tests 4-9)
- Checker: DOM element parsing with positive signal requirement (tests 10-12)
"""
import sys
import os
from datetime import date, time, timedelta
from unittest.mock import MagicMock

import pytest

# Add project root to path so we can import modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper.parser import parse_time_string, parse_date_string, filter_slots
from scraper.checker import AvailabilityChecker
from config import Settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_slot(slot_date: str, slot_time: str, court_name: str = "Tennis Ct01") -> dict:
    """Create a raw slot dict matching the checker output format."""
    return {
        "date": slot_date,
        "time": slot_time,
        "court_name": court_name,
        "day_of_week": "",
        "duration_minutes": 60,
        "raw": {},
    }


def _next_weekday(weekday: int) -> date:
    """Return the next date with the given weekday (0=Mon, 6=Sun)."""
    today = date.today()
    days_ahead = weekday - today.weekday()
    if days_ahead <= 0:
        days_ahead += 7
    return today + timedelta(days=days_ahead)


def _settings() -> Settings:
    """Create a Settings instance for testing."""
    return Settings()


# ===========================================================================
# Tests 1-3: Parser time/date parsing
# ===========================================================================

class TestParseTime:
    """Test 1: 12-hour AM/PM time parsing."""

    def test_parse_time_6pm(self):
        assert parse_time_string("6:00 PM") == time(18, 0)

    def test_parse_time_8am(self):
        assert parse_time_string("8:00 AM") == time(8, 0)

    def test_parse_time_noon(self):
        assert parse_time_string("12:00 PM") == time(12, 0)

    def test_parse_time_midnight(self):
        assert parse_time_string("12:00 AM") == time(0, 0)

    def test_parse_time_830pm(self):
        assert parse_time_string("8:30 PM") == time(20, 30)

    def test_parse_time_no_minutes(self):
        """'6 PM' without minutes should still parse."""
        assert parse_time_string("6 PM") == time(18, 0)


class TestParseTime24h:
    """Test 2: 24-hour time parsing."""

    def test_parse_1800(self):
        assert parse_time_string("18:00") == time(18, 0)

    def test_parse_0800(self):
        assert parse_time_string("08:00") == time(8, 0)

    def test_parse_0000(self):
        assert parse_time_string("00:00") == time(0, 0)

    def test_parse_2359(self):
        assert parse_time_string("23:59") == time(23, 59)


class TestParseTimeInvalid:
    """Test 3: Invalid time strings."""

    def test_empty_string(self):
        assert parse_time_string("") is None

    def test_out_of_range(self):
        assert parse_time_string("25:00") is None

    def test_alphabetic(self):
        assert parse_time_string("abc") is None

    def test_bare_number(self):
        """Bare '8' should NOT parse as a valid time."""
        assert parse_time_string("8") is None

    def test_bare_number_no_ampm(self):
        """'8:00' without AM/PM still parses as 24-hour (valid)."""
        assert parse_time_string("8:00") == time(8, 0)


# ===========================================================================
# Tests 4-9: Filter logic
# ===========================================================================

class TestFilterRejectsEmptyCourtName:
    """Test 4: REGRESSION TEST — slots with empty court_name must be rejected.

    This is the exact bug that caused false positive Sat/Sun 8AM slots.
    """

    def test_empty_court_name_rejected(self):
        next_sat = _next_weekday(5)  # Saturday
        raw = [_make_slot(next_sat.isoformat(), "8:00 AM", court_name="")]
        result = filter_slots(raw, _settings())
        assert len(result) == 0, "Slot with empty court_name should be rejected"

    def test_whitespace_court_name_rejected(self):
        next_sat = _next_weekday(5)
        raw = [_make_slot(next_sat.isoformat(), "8:00 AM", court_name="   ")]
        result = filter_slots(raw, _settings())
        assert len(result) == 0, "Slot with whitespace-only court_name should be rejected"


class TestFilterRejectsNonTennisCourts:
    """Test 5: Non-tennis court names must be filtered out."""

    def test_pickleball_rejected(self):
        next_sat = _next_weekday(5)
        raw = [_make_slot(next_sat.isoformat(), "8:00 AM", court_name="Pickleball Ct1")]
        result = filter_slots(raw, _settings())
        assert len(result) == 0

    def test_ball_machine_rejected(self):
        next_sat = _next_weekday(5)
        raw = [_make_slot(next_sat.isoformat(), "8:00 AM", court_name="Ball Machine")]
        result = filter_slots(raw, _settings())
        assert len(result) == 0

    def test_court_7_rejected(self):
        next_sat = _next_weekday(5)
        raw = [_make_slot(next_sat.isoformat(), "8:00 AM", court_name="Tennis Ct07")]
        result = filter_slots(raw, _settings())
        assert len(result) == 0

    def test_random_name_rejected(self):
        next_sat = _next_weekday(5)
        raw = [_make_slot(next_sat.isoformat(), "8:00 AM", court_name="Gymnasium")]
        result = filter_slots(raw, _settings())
        assert len(result) == 0


class TestFilterAcceptsValidCourts:
    """Test 6: Valid tennis courts Ct01-Ct06 must be accepted."""

    @pytest.mark.parametrize("court", [
        "Tennis Ct01", "Tennis Ct02", "Tennis Ct03",
        "Tennis Ct04", "Tennis Ct05", "Tennis Ct06",
    ])
    def test_tennis_ct_accepted(self, court):
        next_sat = _next_weekday(5)
        raw = [_make_slot(next_sat.isoformat(), "8:00 AM", court_name=court)]
        result = filter_slots(raw, _settings())
        assert len(result) == 1, f"Court '{court}' should be accepted"
        assert result[0]["court_name"] == court

    def test_mcfetridge_prefix_accepted(self):
        next_sat = _next_weekday(5)
        raw = [_make_slot(next_sat.isoformat(), "8:00 AM", court_name="McFetridge Tennis Ct03")]
        result = filter_slots(raw, _settings())
        assert len(result) == 1

    def test_court_word_accepted(self):
        next_sat = _next_weekday(5)
        raw = [_make_slot(next_sat.isoformat(), "8:00 AM", court_name="Tennis Court 3")]
        result = filter_slots(raw, _settings())
        assert len(result) == 1


class TestFilterWeekdayRestriction:
    """Test 7: Weekdays only allow slots >= 6 PM."""

    def test_weekday_2pm_rejected(self):
        next_mon = _next_weekday(0)  # Monday
        raw = [_make_slot(next_mon.isoformat(), "14:00", court_name="Tennis Ct01")]
        result = filter_slots(raw, _settings())
        assert len(result) == 0, "Weekday 2PM should be rejected"

    def test_weekday_6pm_accepted(self):
        next_mon = _next_weekday(0)
        raw = [_make_slot(next_mon.isoformat(), "18:00", court_name="Tennis Ct01")]
        result = filter_slots(raw, _settings())
        assert len(result) == 1, "Weekday 6PM should be accepted"

    def test_weekday_9pm_accepted(self):
        next_mon = _next_weekday(0)
        raw = [_make_slot(next_mon.isoformat(), "21:00", court_name="Tennis Ct01")]
        result = filter_slots(raw, _settings())
        assert len(result) == 1


class TestFilterWeekendAllTimes:
    """Test 8: Weekends allow all times (including early morning)."""

    def test_saturday_8am_accepted(self):
        next_sat = _next_weekday(5)
        raw = [_make_slot(next_sat.isoformat(), "8:00 AM", court_name="Tennis Ct01")]
        result = filter_slots(raw, _settings())
        assert len(result) == 1, "Weekend 8AM with valid court should be accepted"

    def test_sunday_6am_accepted(self):
        next_sun = _next_weekday(6)
        raw = [_make_slot(next_sun.isoformat(), "6:00 AM", court_name="Tennis Ct02")]
        result = filter_slots(raw, _settings())
        assert len(result) == 1

    def test_weekend_slot_has_correct_fields(self):
        next_sat = _next_weekday(5)
        raw = [_make_slot(next_sat.isoformat(), "8:00 AM", court_name="Tennis Ct01")]
        result = filter_slots(raw, _settings())
        assert len(result) == 1
        slot = result[0]
        assert slot["is_weekend"] is True
        assert slot["court_name"] == "Tennis Ct01"
        assert "08:00" in slot["time"] or "8:00" in slot["time"]


class TestFilterDedup:
    """Test 9: Duplicate (date, time, court) slots are deduplicated."""

    def test_exact_duplicates(self):
        next_sat = _next_weekday(5)
        raw = [
            _make_slot(next_sat.isoformat(), "8:00 AM", court_name="Tennis Ct01"),
            _make_slot(next_sat.isoformat(), "8:00 AM", court_name="Tennis Ct01"),
        ]
        result = filter_slots(raw, _settings())
        assert len(result) == 1, "Duplicate slots should be deduplicated to 1"

    def test_different_courts_not_deduped(self):
        next_sat = _next_weekday(5)
        raw = [
            _make_slot(next_sat.isoformat(), "8:00 AM", court_name="Tennis Ct01"),
            _make_slot(next_sat.isoformat(), "8:00 AM", court_name="Tennis Ct02"),
        ]
        result = filter_slots(raw, _settings())
        assert len(result) == 2, "Different courts at same time should both be kept"


# ===========================================================================
# Tests 10-12: Checker DOM element parsing
# ===========================================================================

class TestDomRejectsBareNumber:
    """Test 10: DOM elements with bare numbers (no H:MM AM/PM) are rejected."""

    def test_bare_8_items(self):
        checker = AvailabilityChecker(Settings())
        el = {
            "text": "8 items available",
            "className": "header-count",
            "parentText": "",
            "dataAttrs": {},
            "ariaLabel": "",
        }
        result = checker._parse_dom_element(el, date.today() + timedelta(days=1))
        assert result is None, "Bare '8' should not be parsed as a time slot"

    def test_bare_10_colon_00(self):
        """'10:00' without AM/PM should be rejected by tightened regex."""
        checker = AvailabilityChecker(Settings())
        el = {
            "text": "Last updated 10:00",
            "className": "timestamp",
            "parentText": "",
            "dataAttrs": {},
            "ariaLabel": "",
        }
        result = checker._parse_dom_element(el, date.today() + timedelta(days=1))
        assert result is None, "'10:00' without AM/PM should be rejected"

    def test_price_string(self):
        checker = AvailabilityChecker(Settings())
        el = {
            "text": "$8.00 per hour",
            "className": "pricing",
            "parentText": "",
            "dataAttrs": {},
            "ariaLabel": "",
        }
        result = checker._parse_dom_element(el, date.today() + timedelta(days=1))
        assert result is None, "Price string should not match as time"


class TestDomRejectsTimeWithoutSignal:
    """Test 11: Valid time but no positive signal → rejected."""

    def test_time_in_generic_widget(self):
        checker = AvailabilityChecker(Settings())
        el = {
            "text": "8:00 AM",
            "className": "some-widget",
            "parentText": "",
            "dataAttrs": {},
            "ariaLabel": "",
        }
        result = checker._parse_dom_element(el, date.today() + timedelta(days=1))
        assert result is None, "Time without any positive signal should be rejected"

    def test_time_in_navigation(self):
        checker = AvailabilityChecker(Settings())
        el = {
            "text": "Hours: 8:00 AM - 10:00 PM",
            "className": "nav-hours",
            "parentText": "",
            "dataAttrs": {},
            "ariaLabel": "",
        }
        result = checker._parse_dom_element(el, date.today() + timedelta(days=1))
        assert result is None, "Navigation time should be rejected (no availability signal)"


class TestDomAcceptsValidSlot:
    """Test 12: Valid time + positive availability signal + court → accepted."""

    def test_slot_with_available_class_and_court(self):
        checker = AvailabilityChecker(Settings())
        el = {
            "text": "8:00 AM",
            "className": "time-slot available",
            "parentText": "McFetridge Tennis Ct01",
            "dataAttrs": {},
            "ariaLabel": "",
        }
        target = date.today() + timedelta(days=1)
        result = checker._parse_dom_element(el, target)
        assert result is not None, "Valid slot with available class and court should be accepted"
        assert result["time"] == "08:00"
        assert result["court_name"] == "McFetridge Tennis Ct01"
        assert result["date"] == target.isoformat()

    def test_slot_with_court_in_text(self):
        checker = AvailabilityChecker(Settings())
        el = {
            "text": "Tennis Ct03 - 6:30 PM",
            "className": "cell",
            "parentText": "",
            "dataAttrs": {},
            "ariaLabel": "",
        }
        target = date.today() + timedelta(days=1)
        result = checker._parse_dom_element(el, target)
        assert result is not None, "Slot with court name in text should be accepted"
        assert result["court_name"] == "Tennis Ct03"

    def test_slot_with_data_attr_available(self):
        checker = AvailabilityChecker(Settings())
        el = {
            "text": "9:00 AM",
            "className": "cell",
            "parentText": "McFetridge Tennis Ct05",
            "dataAttrs": {"data-status": "available"},
            "ariaLabel": "",
        }
        target = date.today() + timedelta(days=1)
        result = checker._parse_dom_element(el, target)
        assert result is not None, "Slot with data-status=available should be accepted"

    def test_slot_with_court_in_aria(self):
        checker = AvailabilityChecker(Settings())
        el = {
            "text": "7:00 PM",
            "className": "slot-cell",
            "parentText": "",
            "dataAttrs": {},
            "ariaLabel": "Book Tennis Ct02 at 7:00 PM",
        }
        target = date.today() + timedelta(days=1)
        result = checker._parse_dom_element(el, target)
        assert result is not None, "Slot with court in aria-label should be accepted"
        assert result["court_name"] == "Tennis Ct02"


# ===========================================================================
# Tests 13-16: DOM rejects empty court_name (Fix 1)
# ===========================================================================

class TestDomRejectsEmptyCourtName:
    """_parse_dom_element must return None when no court_name can be extracted,
    even if other positive signals are present."""

    def test_empty_court_positive_class(self):
        """Element with class 'open' but no court anywhere → None."""
        checker = AvailabilityChecker(Settings())
        el = {
            "text": "8:00 AM",
            "className": "time-slot open",
            "parentText": "",
            "dataAttrs": {},
            "ariaLabel": "",
        }
        result = checker._parse_dom_element(el, date.today() + timedelta(days=1))
        assert result is None, "No court_name → must be rejected even with positive class"

    def test_empty_court_positive_data_attr(self):
        """Element with data-status='available' but no court → None."""
        checker = AvailabilityChecker(Settings())
        el = {
            "text": "8:00 AM",
            "className": "cell",
            "parentText": "",
            "dataAttrs": {"data-status": "available"},
            "ariaLabel": "",
        }
        result = checker._parse_dom_element(el, date.today() + timedelta(days=1))
        assert result is None, "No court_name → must be rejected even with positive data attr"

    def test_valid_court_in_text_accepted(self):
        """Court name in element text → accepted."""
        checker = AvailabilityChecker(Settings())
        el = {
            "text": "Tennis Ct 1 8:00 AM",
            "className": "available",
            "parentText": "",
            "dataAttrs": {},
            "ariaLabel": "",
        }
        result = checker._parse_dom_element(el, date.today() + timedelta(days=1))
        assert result is not None, "Court in text should be accepted"
        assert "Tennis Ct" in result["court_name"]

    def test_valid_court_in_parent_with_signal(self):
        """Court in parentText + availability class → accepted."""
        checker = AvailabilityChecker(Settings())
        el = {
            "text": "8:00 AM",
            "className": "time-slot available",
            "parentText": "McFetridge Tennis Ct 3 Schedule",
            "dataAttrs": {},
            "ariaLabel": "",
        }
        result = checker._parse_dom_element(el, date.today() + timedelta(days=1))
        assert result is not None, "Court in parent + available class should be accepted"
        assert "Tennis Ct" in result["court_name"]


# ===========================================================================
# Tests 17-19: Positive signal requires court (Fix 3)
# ===========================================================================

class TestDomPositiveSignalRequiresCourt:
    """Positive class/data signals alone are NOT enough — court must be present."""

    def test_class_open_no_court_rejected(self):
        """Generic 'open' class with no court context → rejected."""
        checker = AvailabilityChecker(Settings())
        el = {
            "text": "8:00 AM",
            "className": "dropdown-open",
            "parentText": "Select a time",
            "dataAttrs": {},
            "ariaLabel": "",
        }
        result = checker._parse_dom_element(el, date.today() + timedelta(days=1))
        assert result is None, "Class 'open' without court context should be rejected"

    def test_class_available_with_court_in_parent(self):
        """Available class + court in parent → accepted."""
        checker = AvailabilityChecker(Settings())
        el = {
            "text": "9:00 AM",
            "className": "slot available",
            "parentText": "Tennis Ct 2 availability",
            "dataAttrs": {},
            "ariaLabel": "",
        }
        result = checker._parse_dom_element(el, date.today() + timedelta(days=1))
        assert result is not None, "Available class + court in parent should be accepted"

    def test_court_in_text_no_class_signal(self):
        """Court name directly in element text → accepted even without class signal."""
        checker = AvailabilityChecker(Settings())
        el = {
            "text": "Tennis Ct03 6:30 PM",
            "className": "cell",
            "parentText": "",
            "dataAttrs": {},
            "ariaLabel": "",
        }
        result = checker._parse_dom_element(el, date.today() + timedelta(days=1))
        assert result is not None, "Court in text should be accepted without class signal"
        assert result["court_name"] == "Tennis Ct03"


# ===========================================================================
# Tests 20-21: API response parser rejects empty court_name (Fix 4)
# ===========================================================================

class TestApiResponseRejectsEmptyCourtName:
    """_parse_captured_responses must skip items with empty court_name."""

    def test_api_empty_court_skipped(self):
        checker = AvailabilityChecker(Settings())
        checker.captured_responses = [{
            "url": "https://example.com/api/availability",
            "data": {
                "items": [
                    {"time": "08:00", "date": "2026-02-21", "court": "", "available": True},
                ]
            },
        }]
        result = checker._parse_captured_responses()
        assert len(result) == 0, "API item with empty court should be skipped"

    def test_api_valid_court_kept(self):
        checker = AvailabilityChecker(Settings())
        checker.captured_responses = [{
            "url": "https://example.com/api/availability",
            "data": {
                "items": [
                    {"time": "08:00", "date": "2026-02-21", "facility": "Tennis Ct 5", "available": True},
                ]
            },
        }]
        result = checker._parse_captured_responses()
        assert len(result) == 1, "API item with valid court should be kept"
        assert result[0]["court_name"] == "Tennis Ct 5"


# ===========================================================================
# Tests 22-23: End-to-end integration (no empty courts in output)
# ===========================================================================

class TestEndToEndNoEmptyCourts:
    """Verify the full pipeline never outputs empty court_name slots."""

    def test_filter_rejects_checker_empty_court(self):
        """A slot from checker with court_name='' should be rejected by filter_slots."""
        raw = [_make_slot(
            _next_weekday(5).isoformat(),  # Saturday
            "8:00 AM",
            court_name="",
        )]
        result = filter_slots(raw, _settings())
        assert len(result) == 0, "Empty court_name must be rejected at filter level"

    def test_build_calendar_no_empty_courts(self):
        """build_calendar output should never contain empty court_name when fed filtered data."""
        from scan_to_json import build_calendar

        # Feed only valid filtered data
        next_sat = _next_weekday(5)
        filtered = [{
            "date": next_sat.isoformat(),
            "time": "06:00 AM",
            "time_24h": "06:00",
            "court_name": "Tennis Ct01",
            "day_of_week": "Saturday",
            "is_weekend": True,
            "duration_minutes": 60,
        }]
        calendar = build_calendar(filtered)
        for day in calendar:
            for slot in day.get("slots", []):
                assert slot["court_name"] != "", \
                    f"Found empty court_name in calendar output for {day['date']}"
