from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    admin_token: str
    data_encryption_key: str
    database_path: Path
    default_delivery_time: str
    timezone: str
    daily_budget_usd: float
    max_tokens: int

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            telegram_token=os.getenv("TELEGRAM_TOKEN", ""),
            admin_token=os.getenv("ADMIN_TOKEN", ""),
            data_encryption_key=os.getenv("DATA_ENCRYPTION_KEY", ""),
            database_path=Path(os.getenv("DATABASE_PATH", "./data/brokoliny.sqlite3")),
            default_delivery_time=os.getenv("DEFAULT_DELIVERY_TIME", "09:00"),
            timezone=os.getenv("TIMEZONE", "Europe/Kyiv"),
            daily_budget_usd=float(os.getenv("AI_DAILY_BUDGET_USD", "5")),
            max_tokens=int(os.getenv("AI_MAX_TOKENS", "2500")),
        )

    def require_bot_configuration(self) -> None:
        missing = []
        if not self.telegram_token:
            missing.append("TELEGRAM_TOKEN")
        if not self.data_encryption_key:
            missing.append("DATA_ENCRYPTION_KEY")
        if missing:
            raise RuntimeError(f"Не заданы обязательные переменные: {', '.join(missing)}")
