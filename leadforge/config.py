"""Runtime settings, read from environment variables (or a .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(ROOT / ".env")


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


@dataclass
class Settings:
    db_path: str = field(default_factory=lambda: os.getenv("LEADFORGE_DB", str(ROOT / "data" / "leadforge.db")))
    public_base_url: str = field(default_factory=lambda: os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/"))
    secret_key: str = field(default_factory=lambda: os.getenv("SECRET_KEY", "change-me"))
    user_agent: str = field(default_factory=lambda: os.getenv(
        "SCRAPER_USER_AGENT",
        "LeadForgeBot/1.0 (+business contact discovery; respects robots.txt)",
    ))
    respect_robots: bool = field(default_factory=lambda: _bool("RESPECT_ROBOTS", True))
    crawl_concurrency: int = field(default_factory=lambda: _int("CRAWL_CONCURRENCY", 8))
    crawl_max_pages: int = field(default_factory=lambda: _int("CRAWL_MAX_PAGES", 5))
    http_timeout: int = field(default_factory=lambda: _int("HTTP_TIMEOUT", 15))

    # Optional data sources
    google_places_api_key: str = field(default_factory=lambda: os.getenv("GOOGLE_PLACES_API_KEY", ""))
    gmaps_scraper_url: str = field(default_factory=lambda: os.getenv("GMAPS_SCRAPER_URL", "").rstrip("/"))
    overpass_url: str = field(default_factory=lambda: os.getenv("OVERPASS_URL", "https://overpass-api.de/api/interpreter"))
    nominatim_url: str = field(default_factory=lambda: os.getenv("NOMINATIM_URL", "https://nominatim.openstreetmap.org"))

    # Optional AI (target suggestions + personalised pitches)
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    anthropic_model: str = field(default_factory=lambda: os.getenv("ANTHROPIC_MODEL", "claude-opus-5-5"))

    # Outreach / sender identity
    smtp_host: str = field(default_factory=lambda: os.getenv("SMTP_HOST", ""))
    smtp_port: int = field(default_factory=lambda: _int("SMTP_PORT", 587))
    smtp_user: str = field(default_factory=lambda: os.getenv("SMTP_USER", ""))
    smtp_password: str = field(default_factory=lambda: os.getenv("SMTP_PASSWORD", ""))
    smtp_use_ssl: bool = field(default_factory=lambda: _bool("SMTP_USE_SSL", False))
    smtp_starttls: bool = field(default_factory=lambda: _bool("SMTP_STARTTLS", True))
    from_email: str = field(default_factory=lambda: os.getenv("FROM_EMAIL", ""))
    from_name: str = field(default_factory=lambda: os.getenv("FROM_NAME", ""))
    sender_company: str = field(default_factory=lambda: os.getenv("SENDER_COMPANY", ""))
    sender_address: str = field(default_factory=lambda: os.getenv("SENDER_POSTAL_ADDRESS", ""))
    # Automation: follow-up sequences, auto-send, inbox reply/bounce detection, webhooks
    automation_enabled: bool = field(default_factory=lambda: _bool("AUTOMATION_ENABLED", True))
    auto_send: bool = field(default_factory=lambda: _bool("AUTO_SEND", True))
    imap_host: str = field(default_factory=lambda: os.getenv("IMAP_HOST", ""))
    imap_port: int = field(default_factory=lambda: _int("IMAP_PORT", 993))
    imap_use_ssl: bool = field(default_factory=lambda: _bool("IMAP_USE_SSL", True))
    imap_user: str = field(default_factory=lambda: os.getenv("IMAP_USER", "") or os.getenv("SMTP_USER", ""))
    imap_password: str = field(default_factory=lambda: os.getenv("IMAP_PASSWORD", "") or os.getenv("SMTP_PASSWORD", ""))
    webhook_url: str = field(default_factory=lambda: os.getenv("WEBHOOK_URL", ""))
    calendar_url: str = field(default_factory=lambda: os.getenv("CALENDAR_URL", ""))
    sender_whatsapp: str = field(default_factory=lambda: os.getenv("SENDER_WHATSAPP", ""))
    daily_send_limit: int = field(default_factory=lambda: _int("DAILY_SEND_LIMIT", 50))
    send_delay_seconds: int = field(default_factory=lambda: _int("SEND_DELAY_SECONDS", 20))

    @property
    def smtp_configured(self) -> bool:
        return bool(self.smtp_host and self.from_email)

    @property
    def imap_configured(self) -> bool:
        return bool(self.imap_host and self.imap_user and self.imap_password)

    @property
    def ai_enabled(self) -> bool:
        return bool(self.anthropic_api_key)


settings = Settings()
