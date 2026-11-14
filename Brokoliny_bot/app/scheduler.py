from __future__ import annotations

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import Settings
from app.content_library import ContentExhausted, ContentLibrary
from app.database import Database
from app.image_cache import ImageCache
from app.plans import deliver_plan
from app.security import SecretVault
from app.age import apply_current_age

logger = logging.getLogger(__name__)


async def deliver_plan_for_user(
    bot: Bot,
    database: Database,
    image_cache: ImageCache,
    content_library: ContentLibrary,
    telegram_id: int,
    plan_date: str,
    vault: SecretVault,
) -> None:
    profile = database.profile(telegram_id)
    if profile is None:
        raise ValueError(f"User {telegram_id} has no child profile.")
    apply_current_age(
        profile,
        database.encrypted_birth_date(telegram_id),
        vault,
        date.fromisoformat(plan_date),
    )

    existing_plan = database.get_plan(telegram_id, plan_date)
    if existing_plan and existing_plan["status"] == "sent":
        return
    if existing_plan and existing_plan["status"] == "pending":
        plan = existing_plan["content"]
    else:
        plan = await content_library.daily_plan(
            telegram_id,
            profile,
            database.recent_feedback(telegram_id),
            plan_date,
        )

    repeated = content_library.repeated_plan_components(
        telegram_id,
        plan,
        excluding_plan_date=plan_date,
    )
    if repeated:
        raise ContentExhausted(
            f"Refusing to deliver repeated content: {', '.join(repeated)}."
        )

    await deliver_plan(
        bot,
        telegram_id,
        plan,
        plan_date,
        database,
        image_cache,
        str(profile.get("language", "ru")),
    )


async def send_due_plans(
    bot: Bot,
    database: Database,
    image_cache: ImageCache,
    content_library: ContentLibrary,
    settings: Settings,
) -> None:
    now = datetime.now(ZoneInfo(settings.timezone))
    today = now.date().isoformat()
    vault = SecretVault(settings.data_encryption_key)
    for user in database.due_users(now.strftime("%H:%M"), today):
        try:
            await deliver_plan_for_user(
                bot,
                database,
                image_cache,
                content_library,
                int(user["telegram_id"]),
                today,
                vault,
            )
        except Exception:
            logger.exception(
                "Could not deliver a fresh plan to user %s.",
                user["telegram_id"],
            )


def start_scheduler(
    bot: Bot,
    database: Database,
    image_cache: ImageCache,
    content_library: ContentLibrary,
    settings: Settings,
) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    scheduler.add_job(
        send_due_plans,
        "interval",
        minutes=1,
        args=[bot, database, image_cache, content_library, settings],
        id="daily-learning-plans",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    return scheduler
