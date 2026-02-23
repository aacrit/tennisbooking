from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ActiveNet booking portal (Quick Reserve — the real court booking path)
    booking_url: str = (
        "https://anc.apm.activecommunities.com/chicagoparkdistrict/"
        "reservation/quick?onlineSiteId=0&from_original_cui=true&online=true"
    )

    # Scanning schedule (CT timezone)
    peak_interval_minutes: int = 5      # 6:50 AM - 8:00 AM CT
    normal_interval_minutes: int = 45   # 8:00 AM - 11:59 PM CT
    peak_start_hour: int = 6            # CT
    peak_end_hour: int = 8              # CT
    quiet_start_hour: int = 0           # No scans midnight-6AM
    quiet_end_hour: int = 6

    # WhatsApp notifications via Green API
    green_api_instance_id: str = ""
    green_api_token: str = ""
    whatsapp_chat_id: str = ""

    # API polling (lightweight HTTP checks between Playwright scans)
    api_poll_enabled: bool = True
    api_poll_peak_seconds: int = 15       # 6:55-7:10 AM burst interval
    api_poll_normal_seconds: int = 120    # 8 AM-midnight interval

    # Notification throttle
    notify_cooldown_seconds: int = 60     # Don't re-notify same slot within 60s

    # Time filters
    weekday_earliest_hour: int = 18     # 6 PM for Mon-Fri
    days_ahead: int = 6                 # Look ahead 6 days

    # Database
    db_path: str = "data/tennisbooking.db"

    # Web server
    host: str = "0.0.0.0"
    port: int = 8080

    # Debug
    debug_headed: bool = False

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}
