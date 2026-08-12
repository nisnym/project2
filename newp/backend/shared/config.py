"""Environment-driven configuration, shared by every service.

One Settings object, imported everywhere. No service reaches for os.environ
directly — that way `.env.example` is a complete and honest description of
every knob the system has.
"""

from __future__ import annotations

import datetime as dt
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(BACKEND_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- ports ---
    gateway_port: int = 8080
    customer_service_port: int = 9001
    transaction_service_port: int = 9002
    budget_service_port: int = 9003
    insight_service_port: int = 9004
    chat_service_port: int = 9005

    # --- service discovery ---
    customer_service_url: str = "http://localhost:9001"
    transaction_service_url: str = "http://localhost:9002"
    budget_service_url: str = "http://localhost:9003"
    insight_service_url: str = "http://localhost:9004"
    chat_service_url: str = "http://localhost:9005"

    # --- data ---
    data_dir: str = str(BACKEND_ROOT / "data")
    persist_writes: bool = False

    # --- money / locale ---
    currency: str = "INR"
    currency_symbol: str = "₹"
    locale: str = "en-IN"

    # --- demo clock ---
    # The seeded ledger runs Jun–Aug 2026, so "today" is pinned to the middle
    # of the last seeded month. Without this the app would look fine today and
    # empty next month, which is not a surprise anyone wants during a demo.
    # Set DEMO_TODAY="" in .env to use the real system date instead.
    demo_today: str = "2026-08-12"

    # --- chat ---
    chat_provider: str = "fallback"
    github_token: str = ""
    github_models_base_url: str = "https://models.github.ai/inference"
    github_model: str = "openai/gpt-4o-mini"
    chat_timeout_seconds: float = 30.0
    chat_max_tokens: int = 700
    chat_temperature: float = 0.2

    # --- misc ---
    log_level: str = "INFO"
    cors_origins: str = "*"
    upstream_timeout_seconds: float = 8.0

    @property
    def data_path(self) -> Path:
        p = Path(self.data_dir)
        return p if p.is_absolute() else (BACKEND_ROOT / p).resolve()

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def today(self) -> dt.date:
        """'Today' for all month-to-date maths.

        The seed ledger is a fixed three-month window, so a pinned demo date
        keeps every projection reproducible. Judges can unpin it by clearing
        DEMO_TODAY, and the whole stack silently moves to the real date.
        """
        if self.demo_today:
            try:
                return dt.date.fromisoformat(self.demo_today)
            except ValueError:
                pass
        return dt.date.today()


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
