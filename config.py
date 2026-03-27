"""Application configuration from environment variables."""

import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    API_KEY: str = os.getenv("API_KEY", "")
    INTEGRATION_MODE: str = os.getenv("INTEGRATION_MODE", "demo")
    API_BASE_URL: str = os.getenv("API_BASE_URL", "")
    BACKOFFICE_API_KEY: str = os.getenv("BACKOFFICE_API_KEY", "")
    TWILIO_ACCOUNT_SID: str = os.getenv("TWILIO_ACCOUNT_SID", "")
    TWILIO_AUTH_TOKEN: str = os.getenv("TWILIO_AUTH_TOKEN", "")
    TWILIO_PHONE_NUMBER: str = os.getenv("TWILIO_PHONE_NUMBER", "")
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///calls.db")
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    # Configurable pricing multipliers
    PRICING_ECONOMY: float = float(os.getenv("PRICING_ECONOMY_MULTIPLIER", "1.0"))
    PRICING_PREMIUM: float = float(os.getenv("PRICING_PREMIUM_MULTIPLIER", "1.35"))
    PRICING_BUSINESS: float = float(os.getenv("PRICING_BUSINESS_MULTIPLIER", "1.9"))

    @property
    def is_live(self) -> bool:
        return self.INTEGRATION_MODE == "live"


settings = Settings()
