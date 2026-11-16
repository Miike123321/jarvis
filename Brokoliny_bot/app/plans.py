from __future__ import annotations

from typing import Any

from aiogram import Bot
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from app.database import Database
from app.image_cache import ImageCache
from app.localization import tr
from app.printables import make_worksheet


def plan_message(plan: dict[str, Any], language: str = "ru") -> str:
    focus = plan["focus_task"]
    activity = plan["activity"]
    facts = "\n".join(f"• {fact}" for fact in activity["age_facts"])
    materials = ", ".join(activity["materials"])
    return (
        f"{tr(language, 'plan_focus', title=focus['title'])}\n"
        f"{tr(language, 'plan_open_worksheet')}\n\n"
        f"{tr(language, 'plan_activity', title=activity['title'])}\n"
        f"{tr(language, 'plan_materials', materials=materials)}\n"
        f"{activity['parent_script']}\n\n"
        f"{tr(language, 'plan_facts')}\n{facts}\n\n"
        f"{tr(language, 'plan_advice', advice=plan['parent_advice'])}\n\n"
        f"{tr(language, 'plan_feedback_question')}"
    )


def feedback_keyboard(plan_date: str, language: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=tr(language, "feedback_easy"),
                    callback_data=f"feedback:{plan_date}:easy",
                ),
                InlineKeyboardButton(
                    text=tr(language, "feedback_normal"),
                    callback_data=f"feedback:{plan_date}:normal",
                ),
                InlineKeyboardButton(
                    text=tr(language, "feedback_hard"),
                    callback_data=f"feedback:{plan_date}:hard",
                ),
            ]
        ]
    )


async def deliver_plan(
    bot: Bot,
    telegram_id: int,
    plan: dict[str, Any],
    plan_date: str,
    database: Database,
    image_cache: ImageCache,
    language: str = "ru",
    *,
    force_resend: bool = False,
) -> None:
    existing = database.get_plan(telegram_id, plan_date)
    if force_resend and (existing is None or existing["status"] != "sent"):
        raise ValueError(
            f"Only an already-sent plan can be resent for {telegram_id} on {plan_date}."
        )
    if existing is None:
        database.save_plan(telegram_id, plan_date, plan, status="pending")
        delivery_state: set[str] = set()
    else:
        if existing["content"] != plan:
            raise ValueError(
                f"Refusing to overwrite an existing plan for {telegram_id} on {plan_date}."
            )
        delivery_state = (
            set() if force_resend else set(existing["delivery_state"])
        )

    if "worksheet" not in delivery_state:
        worksheet = make_worksheet(plan)
        await bot.send_document(
            telegram_id,
            BufferedInputFile(worksheet, filename=f"brokoliny-{plan_date}.pdf"),
            caption=plan["focus_task"]["title"],
        )
        if not force_resend:
            database.mark_plan_part_sent(telegram_id, plan_date, "worksheet")

    image_prompt = plan["focus_task"].get("image_prompt")
    if image_prompt and (
        force_resend or "focus_image" not in delivery_state
    ):
        image_tag = ImageCache.task_image_tag(image_prompt)
        cached_image = (
            image_cache.get_cached_image(image_tag)
            if force_resend
            else await image_cache.get_or_generate(
                image_tag, image_prompt, telegram_id
            )
        )
        if cached_image is not None:
            image_bytes, telegram_file_id, tag_hash = cached_image
            if telegram_file_id:
                await bot.send_photo(telegram_id, telegram_file_id)
            else:
                sent = await bot.send_photo(
                    telegram_id,
                    BufferedInputFile(image_bytes, filename="focus-task.png"),
                )
                if not sent.photo:
                    raise RuntimeError("Telegram did not return the sent image metadata.")
                telegram_file_id = sent.photo[-1].file_id
            image_cache.remember_telegram_file(tag_hash, telegram_file_id, telegram_id)
            if not force_resend:
                database.mark_plan_part_sent(telegram_id, plan_date, "focus_image")

    story_prompt = plan["activity"]["story_prompt"]
    if story_prompt and (
        force_resend or "story_image" not in delivery_state
    ):
        cached_image = (
            image_cache.get_cached_image(story_prompt)
            if force_resend
            else await image_cache.get_or_generate(
                story_prompt, story_prompt, telegram_id
            )
        )
        if cached_image is not None:
            image_bytes, telegram_file_id, tag_hash = cached_image
            if telegram_file_id:
                await bot.send_photo(telegram_id, telegram_file_id)
            else:
                sent = await bot.send_photo(
                    telegram_id,
                    BufferedInputFile(image_bytes, filename="learning-story.png"),
                )
                if not sent.photo:
                    raise RuntimeError("Telegram did not return the sent image metadata.")
                telegram_file_id = sent.photo[-1].file_id
            image_cache.remember_telegram_file(tag_hash, telegram_file_id, telegram_id)
            if not force_resend:
                database.mark_plan_part_sent(telegram_id, plan_date, "story_image")

    if "message" not in delivery_state:
        await bot.send_message(
            telegram_id,
            plan_message(plan, language),
            reply_markup=feedback_keyboard(plan_date, language),
        )
        if not force_resend:
            database.mark_plan_part_sent(telegram_id, plan_date, "message")
    if not force_resend:
        database.mark_plan_sent(telegram_id, plan_date)


def get_skill_gaps(profile: dict[str, Any]) -> list[dict[str, Any]]:
    return [skill for skill in profile["skills"] if skill["result"] != "independent"]
