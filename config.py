from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ActiveNet booking portal
    booking_url: str = (
        "https://anc.apm.activecommunities.com/chicagoparkdistrict/"
        "reservation/landing/quick?groupId=1&locale=en-US"
    )

    # Scanning schedule (CT timezone)
    peak_interval_minutes: int = 5      # 6:50 AM - 8:00 AM CT
    normal_interval_minutes: int = 15   # 8:00 AM - 11:59 PM CT
    peak_start_hour: int = 6            # CT
    peak_end_hour: int = 8              # CT
    quiet_start_hour: int = 0           # No scans midnight-6AM
    quiet_end_hour: int = 6

    # Email / SMTP
    smtp_server: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""             # Gmail App Password
    notify_email: str = "aacritm@gmail.com"
    from_email: str = ""

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
