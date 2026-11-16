from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    telegram_token: str = field(repr=False)
    admin_token: str = field(repr=False)
    data_encryption_key: str = field(repr=False)
    database_path: Path
    default_delivery_time: str
    timezone: str
    daily_budget_usd: float
    max_tokens: int
    app_mode: str = "local"
    cloudflare_access_team: str = ""
    cloudflare_access_audience: str = ""
    cloudflare_access_admin_emails: tuple[str, ...] = field(default=(), repr=False)
    cloudflare_access_clock_skew_seconds: int = 30
    cloudflare_access_jwks_cache_seconds: int = 300

    @classmethod
    def from_env(cls) -> Settings:
        raw_mode = os.getenv("APP_MODE", "local").strip().lower()
        raw_team = os.getenv("CLOUDFLARE_ACCESS_TEAM", "").strip().lower()
        raw_emails = os.getenv("CLOUDFLARE_ACCESS_ADMIN_EMAILS", "")
        return cls(
            telegram_token=os.getenv("TELEGRAM_TOKEN", ""),
            admin_token=os.getenv("ADMIN_TOKEN", ""),
            data_encryption_key=os.getenv("DATA_ENCRYPTION_KEY", ""),
            database_path=Path(os.getenv("DATABASE_PATH", "./data/brokoliny.sqlite3")),
            default_delivery_time=os.getenv("DEFAULT_DELIVERY_TIME", "09:00"),
            timezone=os.getenv("TIMEZONE", "Europe/Kyiv"),
            daily_budget_usd=float(os.getenv("AI_DAILY_BUDGET_USD", "5")),
            max_tokens=int(os.getenv("AI_MAX_TOKENS", "2500")),
            app_mode=raw_mode,
            cloudflare_access_team=raw_team,
            cloudflare_access_audience=os.getenv(
                "CLOUDFLARE_ACCESS_AUDIENCE", ""
            ).strip(),
            cloudflare_access_admin_emails=tuple(
                item.strip() for item in raw_emails.split(",") if item.strip()
            ),
            cloudflare_access_clock_skew_seconds=int(
                os.getenv("CLOUDFLARE_ACCESS_CLOCK_SKEW_SECONDS", "30")
            ),
            cloudflare_access_jwks_cache_seconds=int(
                os.getenv("CLOUDFLARE_ACCESS_JWKS_CACHE_SECONDS", "300")
            ),
        )

    @property
    def remote_mode(self) -> bool:
        return self.app_mode == "remote"

    @property
    def cloudflare_access_issuer(self) -> str:
        if not self.cloudflare_access_team:
            return ""
        return f"https://{self.cloudflare_access_team}.cloudflareaccess.com"

    def validate_remote_access(self) -> None:
        """Validate static trust configuration without doing network or file I/O."""
        if self.app_mode not in {"local", "remote"}:
            raise RuntimeError("APP_MODE должен быть local или remote.")
        if not self.remote_mode:
            return
        if not self.admin_token:
            raise RuntimeError("Для удалённого режима обязательно задайте ADMIN_TOKEN.")
        if not re.fullmatch(
            r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
            self.cloudflare_access_team,
        ):
            raise RuntimeError(
                "Для удалённого режима задайте CLOUDFLARE_ACCESS_TEAM "
                "(только имя команды Cloudflare)."
            )
        if not self.cloudflare_access_audience or len(
            self.cloudflare_access_audience
        ) > 512:
            raise RuntimeError(
                "Для удалённого режима задайте точный "
                "CLOUDFLARE_ACCESS_AUDIENCE приложения."
            )
        if not self.cloudflare_access_admin_emails:
            raise RuntimeError(
                "Для удалённого режима задайте непустой "
                "CLOUDFLARE_ACCESS_ADMIN_EMAILS allowlist."
            )
        if len(self.cloudflare_access_admin_emails) > 100:
            raise RuntimeError("В Access allowlist разрешено не более 100 адресов.")
        if not 0 <= self.cloudflare_access_clock_skew_seconds <= 60:
            raise RuntimeError(
                "CLOUDFLARE_ACCESS_CLOCK_SKEW_SECONDS должен быть от 0 до 60."
            )
        if not 30 <= self.cloudflare_access_jwks_cache_seconds <= 900:
            raise RuntimeError(
                "CLOUDFLARE_ACCESS_JWKS_CACHE_SECONDS должен быть от 30 до 900."
            )

    def require_bot_configuration(self) -> None:
        missing = []
        if not self.telegram_token:
            missing.append("TELEGRAM_TOKEN")
        if not self.data_encryption_key:
            missing.append("DATA_ENCRYPTION_KEY")
        if missing:
            raise RuntimeError(f"Не заданы обязательные переменные: {', '.join(missing)}")
