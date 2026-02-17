"""
WhatsApp notifications via Green API.

Sends a WhatsApp message when new tennis court slots are detected.
Uses the Green API free tier (Developer plan): 3 chats/month, unlimited messages.

Setup:
    1. Sign up at https://green-api.com
    2. Create a free Developer instance
    3. Scan QR code to link your WhatsApp
    4. Set GREEN_API_INSTANCE_ID, GREEN_API_TOKEN, WHATSAPP_CHAT_ID env vars
"""
import json
import logging
import urllib.request
import urllib.error
from datetime import datetime

logger = logging.getLogger(__name__)

BOOKING_URL = (
    "https://anc.apm.activecommunities.com/chicagoparkdistrict/"
    "reservation/landing/quick?groupId=1&locale=en-US"
)


def format_slots_message(opened_slots: list[dict]) -> str:
    """Build a WhatsApp message for newly opened slots.

    Args:
        opened_slots: List of dicts with keys: date, time, court_name, detected_at.
                      (Same shape as changes["opened"] from scan_to_json.py)

    Returns:
        Formatted WhatsApp message string with bold formatting.
    """
    grouped: dict[str, list[dict]] = {}
    for slot in opened_slots:
        grouped.setdefault(slot["date"], []).append(slot)

    lines = ["\U0001f3be *New Tennis Court Slots!*", ""]

    for date_str in sorted(grouped.keys()):
        date_slots = grouped[date_str]
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d")
            day_label = d.strftime("%a %b %d")
        except ValueError:
            day_label = date_str

        lines.append(f"*{day_label}:*")
        for slot in sorted(date_slots, key=lambda s: s["time"]):
            court = slot.get("court_name", "")
            if court:
                lines.append(f"\u2022 {slot['time']} \u2014 {court}")
            else:
                lines.append(f"\u2022 {slot['time']}")
        lines.append("")

    lines.append(f"Book now: {BOOKING_URL}")
    return "\n".join(lines)


def send_whatsapp(instance_id: str, api_token: str, chat_id: str, message: str) -> bool:
    """Send a WhatsApp message via Green API.

    Args:
        instance_id: Green API instance ID (idInstance).
        api_token: Green API token (apiTokenInstance).
        chat_id: Recipient in format "1XXXXXXXXXX@c.us".
        message: Message text to send.

    Returns:
        True on success, False on failure.
    """
    url = (
        f"https://api.green-api.com/"
        f"waInstance{instance_id}/sendMessage/{api_token}"
    )
    payload = json.dumps({"chatId": chat_id, "message": message}).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = resp.status
            body = resp.read().decode("utf-8", errors="replace")
            logger.info("WhatsApp sent (HTTP %d): %s", status, body[:200])
            return True
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        logger.error("WhatsApp send failed (HTTP %d): %s", e.code, body[:300])
        return False
    except Exception as e:
        logger.exception("WhatsApp send failed: %s", e)
        return False
