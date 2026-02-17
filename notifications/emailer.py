# [GITHUB-PAGES] Entire emailer module disabled for static deployment.
# Uncomment below to re-enable email notifications.
#
# """
# Email notifications via Gmail SMTP.
#
# Sends HTML + plaintext emails when new tennis court slots are detected.
# Includes deduplication to avoid sending duplicate alerts.
# """
# import asyncio
# import logging
# import smtplib
# from email.mime.multipart import MIMEMultipart
# from email.mime.text import MIMEText
# from itertools import groupby
#
# from config import Settings
#
# logger = logging.getLogger(__name__)
#
# BOOKING_URL = (
#     "https://apm.activecommunities.com/chicagoparkdistrict/Reserve_Options"
# )
#
#
# def _build_html(slots: list[dict]) -> str:
#     """Build a clean HTML email body with slots grouped by date."""
#     grouped = {}
#     for slot in slots:
#         grouped.setdefault(slot["date"], []).append(slot)
#
#     html = """
#     <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
#                 max-width: 600px; margin: 0 auto; padding: 20px;">
#         <div style="background: #16a34a; color: white; padding: 16px 24px;
#                     border-radius: 12px 12px 0 0;">
#             <h2 style="margin: 0; font-size: 20px;">
#                 Tennis Court Available at McFetridge
#             </h2>
#         </div>
#         <div style="border: 1px solid #e5e7eb; border-top: none;
#                     border-radius: 0 0 12px 12px; padding: 24px;">
#     """
#
#     for date_str in sorted(grouped.keys()):
#         date_slots = grouped[date_str]
#         day_name = date_slots[0]["day_of_week"]
#         weekend_tag = (
#             ' <span style="background: #dbeafe; color: #1d4ed8; '
#             'font-size: 11px; padding: 2px 8px; border-radius: 9999px;">'
#             'Weekend</span>'
#             if date_slots[0].get("is_weekend") else ""
#         )
#
#         html += f"""
#             <h3 style="margin: 20px 0 8px; color: #111827; font-size: 16px;">
#                 {day_name}, {date_str}{weekend_tag}
#             </h3>
#             <div style="background: #f9fafb; border-radius: 8px; padding: 12px;">
#         """
#
#         for slot in sorted(date_slots, key=lambda s: s.get("time_24h", s["time"])):
#             court = f" &mdash; {slot['court_name']}" if slot.get("court_name") else ""
#             html += f"""
#                 <div style="padding: 8px 0; border-bottom: 1px solid #e5e7eb;">
#                     <span style="font-weight: 600; color: #16a34a; font-size: 15px;">
#                         {slot['time']}
#                     </span>
#                     <span style="color: #6b7280; font-size: 14px;">{court}</span>
#                 </div>
#             """
#
#         html += "</div>"
#
#     html += f"""
#             <div style="margin-top: 24px; text-align: center;">
#                 <a href="{BOOKING_URL}"
#                    style="display: inline-block; background: #16a34a; color: white;
#                           padding: 12px 32px; border-radius: 8px; text-decoration: none;
#                           font-weight: 600; font-size: 15px;">
#                     Book Now
#                 </a>
#             </div>
#             <p style="margin-top: 16px; font-size: 12px; color: #9ca3af; text-align: center;">
#                 McFetridge Sports Center &bull; 3843 N. California Ave, Chicago
#                 <br>Slots fill up fast &mdash; book immediately!
#             </p>
#         </div>
#     </div>
#     """
#     return html
#
#
# def _build_text(slots: list[dict]) -> str:
#     """Build plaintext email body."""
#     grouped = {}
#     for slot in slots:
#         grouped.setdefault(slot["date"], []).append(slot)
#
#     lines = ["TENNIS COURT AVAILABLE AT MCFETRIDGE", "=" * 40, ""]
#
#     for date_str in sorted(grouped.keys()):
#         date_slots = grouped[date_str]
#         day_name = date_slots[0]["day_of_week"]
#         weekend = " (Weekend)" if date_slots[0].get("is_weekend") else ""
#         lines.append(f"{day_name}, {date_str}{weekend}")
#         lines.append("-" * 30)
#         for slot in sorted(date_slots, key=lambda s: s.get("time_24h", s["time"])):
#             court = f" - {slot['court_name']}" if slot.get("court_name") else ""
#             lines.append(f"  {slot['time']}{court}")
#         lines.append("")
#
#     lines.append(f"Book now: {BOOKING_URL}")
#     lines.append("")
#     lines.append("McFetridge Sports Center - 3843 N. California Ave, Chicago")
#     return "\n".join(lines)
#
#
# def _send_smtp(settings: Settings, msg: MIMEMultipart):
#     """Send email via SMTP (blocking, run in executor)."""
#     with smtplib.SMTP(settings.smtp_server, settings.smtp_port, timeout=30) as server:
#         server.ehlo()
#         server.starttls()
#         server.ehlo()
#         server.login(settings.smtp_username, settings.smtp_password)
#         server.send_message(msg)
#
#
# async def send_availability_email(settings: Settings, slots: list[dict]) -> bool:
#     """Send notification email with available slots. Returns True on success."""
#     if not settings.smtp_username or not settings.smtp_password:
#         logger.warning("SMTP credentials not configured, skipping email")
#         return False
#
#     count = len(slots)
#     subject = f"Tennis Court{'s' if count != 1 else ''} Available! ({count} slot{'s' if count != 1 else ''})"
#
#     msg = MIMEMultipart("alternative")
#     msg["Subject"] = subject
#     msg["From"] = settings.from_email or settings.smtp_username
#     msg["To"] = settings.notify_email
#
#     msg.attach(MIMEText(_build_text(slots), "plain"))
#     msg.attach(MIMEText(_build_html(slots), "html"))
#
#     try:
#         loop = asyncio.get_event_loop()
#         await loop.run_in_executor(None, _send_smtp, settings, msg)
#         logger.info("Email sent to %s: %s", settings.notify_email, subject)
#         return True
#     except Exception as e:
#         logger.exception("Failed to send email: %s", e)
#         return False
