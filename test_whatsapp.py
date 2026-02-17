#!/usr/bin/env python3
"""
Quick test: send a WhatsApp message with mock court data.

Usage:
    GREEN_API_INSTANCE_ID=xxx GREEN_API_TOKEN=xxx WHATSAPP_CHAT_ID=1XXXXXXXXXX@c.us python test_whatsapp.py
"""
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

from notifications.whatsapp import format_slots_message, send_whatsapp

# Mock data: slots that "just opened"
MOCK_OPENED = [
    {"date": "2026-02-19", "time": "6:00 PM", "court_name": "Tennis Ct 1", "detected_at": "2026-02-17 15:00:00 CT"},
    {"date": "2026-02-19", "time": "7:00 PM", "court_name": "Tennis Ct 3", "detected_at": "2026-02-17 15:00:00 CT"},
    {"date": "2026-02-20", "time": "10:00 AM", "court_name": "Tennis Ct 2", "detected_at": "2026-02-17 15:00:00 CT"},
    {"date": "2026-02-21", "time": "2:00 PM", "court_name": "Tennis Ct 5", "detected_at": "2026-02-17 15:00:00 CT"},
]

msg = format_slots_message(MOCK_OPENED)
print("=== Message Preview ===")
print(msg)
print("=======================\n")

instance_id = os.environ.get("GREEN_API_INSTANCE_ID", "")
api_token = os.environ.get("GREEN_API_TOKEN", "")
chat_id = os.environ.get("WHATSAPP_CHAT_ID", "")

if not all([instance_id, api_token, chat_id]):
    print("Set these env vars to send a live message:")
    print("  GREEN_API_INSTANCE_ID=your-instance-id")
    print("  GREEN_API_TOKEN=your-api-token")
    print("  WHATSAPP_CHAT_ID=1XXXXXXXXXX@c.us")
    sys.exit(0)

print(f"Sending to {chat_id}...")
ok = send_whatsapp(instance_id, api_token, chat_id, msg)
if ok:
    print("Sent successfully! Check your WhatsApp.")
else:
    print("Failed to send. Check the error above.")
    sys.exit(1)
