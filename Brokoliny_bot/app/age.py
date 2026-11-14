from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime
from typing import Any

from app.security import SecretVault

MIN_AGE_MONTHS = 7
MAX_AGE_MONTHS = 84


def completed_age_months(birth_date: date, today: date) -> int:
    months = (today.year - birth_date.year) * 12 + today.month - birth_date.month
    birthday_day = min(birth_date.day, monthrange(today.year, today.month)[1])
    if today.day < birthday_day:
        months -= 1
    return months


def parse_birth_date(value: str, today: date | None = None) -> date:
    text = value.strip()
    parsed: date | None = None
    for pattern in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, pattern).date()
            break
        except ValueError:
            continue
    if parsed is None:
        raise ValueError("Enter the birth date as DD.MM.YYYY.")

    reference_date = today or date.today()
    if parsed > reference_date:
        raise ValueError("Birth date cannot be in the future.")
    age_months = completed_age_months(parsed, reference_date)
    if not MIN_AGE_MONTHS <= age_months <= MAX_AGE_MONTHS:
        raise ValueError("The bot supports children aged 7 months to 7 years.")
    return parsed


def apply_current_age(
    profile: dict[str, Any],
    encrypted_birth_date: str | None,
    vault: SecretVault,
    today: date,
) -> dict[str, Any]:
    if encrypted_birth_date:
        birth_date = date.fromisoformat(vault.decrypt(encrypted_birth_date))
        profile["age_months"] = min(
            completed_age_months(birth_date, today), MAX_AGE_MONTHS
        )
    return profile
