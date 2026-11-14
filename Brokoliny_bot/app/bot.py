from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from litellm.exceptions import APIError

from app.age import (
    MAX_AGE_MONTHS,
    apply_current_age,
    completed_age_months,
    parse_birth_date,
)
from app.ai import AIBudgetExceeded, AIOrchestrator
from app.config import Settings
from app.content_library import ContentExhausted, ContentLibrary
from app.database import Database
from app.image_cache import ImageCache
from app.localization import tr
from app.plans import deliver_plan
from app.scheduler import deliver_plan_for_user
from app.security import SecretVault

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
router = Router()


class Onboarding(StatesGroup):
    language = State()
    identity_consent = State()
    identity_full_name = State()
    identity_child_name = State()
    identity_phone = State()
    birth_date = State()
    gender = State()
    diagnosis = State()
    diagnosis_notes = State()
    diagnosis_consent = State()
    assessment = State()
    interests = State()
    delivery_time = State()


def parse_time(value: str) -> str:
    try:
        parsed = datetime.strptime(value.strip(), "%H:%M")
    except ValueError as exc:
        raise ValueError("Введите время в формате ЧЧ:ММ, например 09:00.") from exc
    return parsed.strftime("%H:%M")


def validate_identity_name(value: str) -> str | None:
    name = value.strip()
    if not name or len(name) > 120 or any(ord(character) < 32 for character in name):
        return None
    return name


def _services() -> tuple[Settings, Database, SecretVault, AIOrchestrator, ImageCache]:
    settings = Settings.from_env()
    settings.require_bot_configuration()
    database = Database(settings.database_path)
    database.initialize()
    vault = SecretVault(settings.data_encryption_key)
    ai = AIOrchestrator(
        database,
        vault,
        daily_budget_usd=settings.daily_budget_usd,
        max_tokens=settings.max_tokens,
    )
    return settings, database, vault, ai, ImageCache(database, ai)


def _language_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Українська 🇺🇦", callback_data="language:uk"),
                InlineKeyboardButton(text="Русский 🇷🇺", callback_data="language:ru"),
            ]
        ]
    )


def _identity_consent_keyboard(language: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=tr(language, "identity_consent_yes"),
                    callback_data="identity_consent:yes",
                ),
                InlineKeyboardButton(
                    text=tr(language, "identity_consent_no"),
                    callback_data="identity_consent:no",
                ),
            ]
        ]
    )


def _identity_phone_keyboard(language: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text=tr(language, "identity_phone_button"),
                    request_contact=True,
                )
            ],
            [KeyboardButton(text=tr(language, "identity_phone_skip"))],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


async def _begin_birth_date_flow(
    message: Message, state: FSMContext, language: str
) -> None:
    await state.set_state(Onboarding.birth_date)
    await message.answer(tr(language, "birth_date_prompt"))


def _gender_keyboard(language: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=tr(language, "boy"), callback_data="gender:М"),
                InlineKeyboardButton(text=tr(language, "girl"), callback_data="gender:Ж"),
            ]
        ]
    )


def _diagnosis_keyboard(language: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=tr(language, "yes"), callback_data="diagnosis:yes"),
                InlineKeyboardButton(text=tr(language, "no"), callback_data="diagnosis:no"),
            ]
        ]
    )


def _ai_sharing_keyboard(language: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=tr(language, "consent_yes"),
                    callback_data="diagnosis_consent:yes",
                ),
                InlineKeyboardButton(
                    text=tr(language, "consent_no"),
                    callback_data="diagnosis_consent:no",
                ),
            ]
        ]
    )


def _assessment_keyboard(index: int, language: str) -> InlineKeyboardMarkup:
    choices = (
        (tr(language, "assessment_independent"), "independent"),
        (tr(language, "assessment_prompted"), "prompted"),
        (tr(language, "assessment_not_yet"), "not_yet"),
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=label, callback_data=f"assessment:{index}:{result}"
                )
                for label, result in choices
            ]
        ]
    )


async def _start_assessment(
    message: Message,
    telegram_id: int,
    state: FSMContext,
    settings: Settings,
    database: Database,
    vault: SecretVault,
    ai: AIOrchestrator,
    birth_date: date,
    gender: str,
    language: str,
    diagnosis_notes: str | None,
    *,
    share_notes_with_ai: bool = True,
) -> None:
    encrypted_notes = vault.encrypt(diagnosis_notes) if diagnosis_notes else None
    age_months = min(
        completed_age_months(
            birth_date, datetime.now(ZoneInfo(settings.timezone)).date()
        ),
        MAX_AGE_MONTHS,
    )
    database.upsert_profile(
        telegram_id,
        age_months,
        gender,
        settings.default_delivery_time,
        encrypted_notes,
        encrypted_birth_date=vault.encrypt(birth_date.isoformat()),
    )
    await message.answer(tr(language, "assessment_intro"))
    try:
        image_cache = ImageCache(database, ai)
        questions = await ContentLibrary(database, ai).assessment_questions(
            telegram_id,
            age_months,
            diagnosis_notes if share_notes_with_ai and diagnosis_notes else "",
            language,
        )
    except AIBudgetExceeded:
        await message.answer(tr(language, "budget_exhausted"))
        return
    except APIError as exc:
        logger.error("Assessment provider failed with %s.", type(exc).__name__)
        await message.answer(tr(language, "provider_error"))
        return
    except ContentExhausted:
        await message.answer(tr(language, "content_exhausted"))
        return
    await state.update_data(questions=questions, assessment_index=0)
    await state.set_state(Onboarding.assessment)
    await _send_assessment_question(
        message, questions, 0, image_cache, telegram_id, language
    )


async def _send_assessment_question(
    message: Message,
    questions: list[dict],
    index: int,
    image_cache: ImageCache,
    telegram_id: int,
    language: str,
) -> None:
    question = questions[index]
    keyboard = _assessment_keyboard(index, language)
    if not question["stimulus_kind"]:
        await message.answer(question["parent_script"], reply_markup=keyboard)
        return
    image, telegram_file_id, tag_hash = await image_cache.get_unique_stimulus(
        telegram_id,
        question["stimulus_kind"],
        question["_content_hash"],
    )
    sent = await message.answer_photo(
        telegram_file_id or BufferedInputFile(image, filename="game-stimulus.png"),
        caption=question["parent_script"],
        reply_markup=keyboard,
    )
    if sent.photo:
        image_cache.remember_telegram_file(
            tag_hash, sent.photo[-1].file_id, telegram_id
        )
    else:
        raise RuntimeError("Telegram did not return the sent stimulus metadata.")


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    if message.from_user is None:
        return
    settings = Settings.from_env()
    database = Database(settings.database_path)
    database.initialize()
    profile = database.profile(message.from_user.id)
    first_registration = profile is None or not bool(profile["active"])
    await state.update_data(first_registration=first_registration)
    await state.set_state(Onboarding.language)
    await message.answer(
        tr("ru", "choose_language"),
        reply_markup=_language_keyboard(),
    )


@router.callback_query(Onboarding.language, F.data.startswith("language:"))
async def choose_language(callback: CallbackQuery, state: FSMContext) -> None:
    if not isinstance(callback.message, Message):
        await callback.answer("Choose a language / Выберите язык.", show_alert=True)
        return
    language = callback.data.split(":", 1)[1]
    if language not in {"ru", "uk"}:
        await callback.answer("Невідома мова / Неизвестный язык.", show_alert=True)
        return
    settings = Settings.from_env()
    database = Database(settings.database_path)
    database.initialize()
    database.set_language(
        callback.from_user.id, language, settings.default_delivery_time
    )
    await state.update_data(language=language)
    identity_status = database.identity_consent_status(callback.from_user.id)
    if callback.message.chat.type == "private" and identity_status == "pending":
        await state.update_data(identity_continue_onboarding=True)
        await state.set_state(Onboarding.identity_consent)
        await callback.message.answer(
            tr(language, "identity_consent_prompt"),
            reply_markup=_identity_consent_keyboard(language),
        )
    elif callback.message.chat.type == "private" and identity_status == "collecting":
        await state.update_data(identity_continue_onboarding=True)
        await state.set_state(Onboarding.identity_full_name)
        await callback.message.answer(tr(language, "identity_full_name_prompt"))
    else:
        await _begin_birth_date_flow(callback.message, state, language)
    await callback.answer()


@router.message(Command("identity"))
async def identity_settings(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    settings = Settings.from_env()
    database = Database(settings.database_path)
    database.initialize()
    language = database.language(message.from_user.id)
    if message.chat.type != "private":
        await message.answer(tr(language, "identity_private_only"))
        return
    database.set_language(
        message.from_user.id, language, settings.default_delivery_time
    )
    await state.update_data(
        language=language,
        identity_continue_onboarding=False,
    )
    await state.set_state(Onboarding.identity_consent)
    await message.answer(
        tr(language, "identity_consent_prompt"),
        reply_markup=_identity_consent_keyboard(language),
    )


@router.callback_query(
    Onboarding.identity_consent, F.data.startswith("identity_consent:")
)
async def identity_consent(callback: CallbackQuery, state: FSMContext) -> None:
    if not isinstance(callback.message, Message):
        await callback.answer("Open this action in a private chat.", show_alert=True)
        return
    values = await state.get_data()
    language = str(values.get("language", "ru"))
    settings = Settings.from_env()
    database = Database(settings.database_path)
    database.initialize()
    if callback.data.endswith(":no"):
        database.set_identity_consent_status(callback.from_user.id, "denied")
        await callback.answer()
        if values.get("identity_continue_onboarding"):
            await callback.message.answer(tr(language, "identity_data_declined"))
            await _begin_birth_date_flow(callback.message, state, language)
        else:
            await state.clear()
            await callback.message.answer(tr(language, "identity_data_declined"))
        return
    if not callback.data.endswith(":yes"):
        await callback.answer(tr(language, "restart_error"), show_alert=True)
        return
    database.set_identity_consent_status(callback.from_user.id, "collecting")
    await state.set_state(Onboarding.identity_full_name)
    await callback.message.answer(tr(language, "identity_full_name_prompt"))
    await callback.answer()


@router.message(Onboarding.identity_full_name)
async def receive_identity_full_name(message: Message, state: FSMContext) -> None:
    values = await state.get_data()
    language = str(values.get("language", "ru"))
    full_name = validate_identity_name(message.text or "")
    if full_name is None:
        await message.answer(tr(language, "identity_name_invalid"))
        return
    await state.update_data(identity_full_name=full_name)
    await state.set_state(Onboarding.identity_child_name)
    await message.answer(tr(language, "identity_child_name_prompt"))


@router.message(Onboarding.identity_child_name)
async def receive_identity_child_name(message: Message, state: FSMContext) -> None:
    values = await state.get_data()
    language = str(values.get("language", "ru"))
    child_name = validate_identity_name(message.text or "")
    if child_name is None:
        await message.answer(tr(language, "identity_name_invalid"))
        return
    await state.update_data(identity_child_name=child_name)
    await state.set_state(Onboarding.identity_phone)
    await message.answer(
        tr(language, "identity_phone_prompt"),
        reply_markup=_identity_phone_keyboard(language),
    )


@router.message(Onboarding.identity_phone)
async def receive_identity_phone(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    values = await state.get_data()
    language = str(values.get("language", "ru"))
    phone_number: str | None = None
    if message.text and message.text.strip() in {
        "/skip",
        tr(language, "identity_phone_skip"),
    }:
        pass
    elif (
        message.contact is not None
        and message.contact.user_id == message.from_user.id
        and message.contact.phone_number
    ):
        phone_number = message.contact.phone_number
    else:
        await message.answer(
            tr(language, "identity_phone_invalid"),
            reply_markup=_identity_phone_keyboard(language),
        )
        return

    settings = Settings.from_env()
    database = Database(settings.database_path)
    database.initialize()
    vault = SecretVault(settings.data_encryption_key)
    identity = {
        "full_name": str(values["identity_full_name"]),
        "child_name": str(values["identity_child_name"]),
        "phone_number": phone_number,
    }
    database.save_admin_identity(
        message.from_user.id,
        vault.encrypt(json.dumps(identity, ensure_ascii=False)),
    )
    await message.answer(
        tr(language, "identity_data_saved"),
        reply_markup=ReplyKeyboardRemove(),
    )
    if values.get("identity_continue_onboarding"):
        await _begin_birth_date_flow(message, state, language)
    else:
        await state.clear()


@router.message(Onboarding.birth_date)
async def receive_birth_date(message: Message, state: FSMContext) -> None:
    values = await state.get_data()
    language = str(values.get("language", "ru"))
    settings = Settings.from_env()
    try:
        birth_date = parse_birth_date(
            message.text or "",
            today=datetime.now(ZoneInfo(settings.timezone)).date(),
        )
    except ValueError:
        await message.answer(tr(language, "birth_date_invalid"))
        return
    await state.update_data(birth_date=birth_date.isoformat())
    await state.set_state(Onboarding.gender)
    await message.answer(
        tr(language, "gender_prompt"),
        reply_markup=_gender_keyboard(language),
    )


@router.callback_query(Onboarding.gender, F.data.startswith("gender:"))
async def receive_gender(
    callback: CallbackQuery, state: FSMContext
) -> None:
    if not isinstance(callback.message, Message):
        values = await state.get_data()
        await callback.answer(
            tr(str(values.get("language", "ru")), "restart_error"),
            show_alert=True,
        )
        return
    values = await state.get_data()
    language = str(values.get("language", "ru"))
    await state.update_data(gender=callback.data.split(":", 1)[1])
    await state.set_state(Onboarding.diagnosis)
    await callback.message.answer(
        tr(language, "diagnosis_prompt"),
        reply_markup=_diagnosis_keyboard(language),
    )
    await callback.answer()


@router.callback_query(Onboarding.diagnosis, F.data.startswith("diagnosis:"))
async def receive_diagnosis(
    callback: CallbackQuery, state: FSMContext
) -> None:
    if not isinstance(callback.message, Message):
        values = await state.get_data()
        await callback.answer(
            tr(str(values.get("language", "ru")), "restart_error"),
            show_alert=True,
        )
        return
    values = await state.get_data()
    language = str(values.get("language", "ru"))
    if callback.data.endswith(":yes"):
        await state.set_state(Onboarding.diagnosis_notes)
        await callback.message.answer(
            tr(language, "diagnosis_notes_prompt")
        )
        await callback.answer()
        return
    settings, database, vault, ai, _ = _services()
    await callback.message.answer(tr(language, "thanks"))
    await callback.answer()
    await _start_assessment(
        callback.message,
        callback.from_user.id,
        state,
        settings,
        database,
        vault,
        ai,
        date.fromisoformat(str(values["birth_date"])),
        str(values["gender"]),
        language,
        None,
    )


@router.message(Onboarding.diagnosis_notes, Command("skip"))
async def skip_diagnosis_notes(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    values = await state.get_data()
    settings, database, vault, ai, _ = _services()
    await _start_assessment(
        message,
        message.from_user.id,
        state,
        settings,
        database,
        vault,
        ai,
        date.fromisoformat(str(values["birth_date"])),
        str(values["gender"]),
        str(values.get("language", "ru")),
        None,
    )


@router.message(Onboarding.diagnosis_notes)
async def receive_diagnosis_notes(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    values = await state.get_data()
    language = str(values.get("language", "ru"))
    if not message.text:
        await message.answer(tr(language, "notes_invalid"))
        return
    await state.update_data(diagnosis_notes=message.text)
    await state.set_state(Onboarding.diagnosis_consent)
    await message.answer(
        tr(language, "consent_prompt"),
        reply_markup=_ai_sharing_keyboard(language),
    )


@router.callback_query(Onboarding.diagnosis_consent, F.data.startswith("diagnosis_consent:"))
async def diagnosis_ai_consent(callback: CallbackQuery, state: FSMContext) -> None:
    if not isinstance(callback.message, Message):
        values = await state.get_data()
        await callback.answer(
            tr(str(values.get("language", "ru")), "restart_error"),
            show_alert=True,
        )
        return
    values = await state.get_data()
    language = str(values.get("language", "ru"))
    settings, database, vault, ai, _ = _services()
    stored_note = str(values["diagnosis_notes"])
    await callback.answer()
    await _start_assessment(
        callback.message,
        callback.from_user.id,
        state,
        settings,
        database,
        vault,
        ai,
        date.fromisoformat(str(values["birth_date"])),
        str(values["gender"]),
        language,
        stored_note,
        share_notes_with_ai=callback.data.endswith(":yes"),
    )


@router.callback_query(Onboarding.assessment, F.data.startswith("assessment:"))
async def assess_result(callback: CallbackQuery, state: FSMContext) -> None:
    values = await state.get_data()
    language = str(values.get("language", "ru"))
    if not isinstance(callback.message, Message):
        await callback.answer(
            tr(language, "assessment_save_error"),
            show_alert=True,
        )
        return
    _, raw_index, result = callback.data.split(":", 2)
    index = int(raw_index)
    questions = values["questions"]
    if index != int(values["assessment_index"]) or index >= len(questions):
        await callback.answer(
            tr(language, "assessment_already_answered"),
            show_alert=True,
        )
        return
    question = questions[index]
    _, database, _, ai, _ = _services()
    image_cache = ImageCache(database, ai)
    database.record_skill(
        callback.from_user.id,
        question["skill_id"],
        ",".join(question["domains"]),
        result,
    )
    await callback.answer(tr(language, "answer_saved"))
    next_index = index + 1
    if next_index < len(questions):
        await state.update_data(assessment_index=next_index)
        await _send_assessment_question(
            callback.message,
            questions,
            next_index,
            image_cache,
            callback.from_user.id,
            language,
        )
        return
    await state.set_state(Onboarding.interests)
    await callback.message.answer(tr(language, "final_question"))


@router.message(Onboarding.interests)
async def receive_interests(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    settings = Settings.from_env()
    database = Database(settings.database_path)
    database.initialize()
    database.update_interests(message.from_user.id, (message.text or "").strip())
    values = await state.get_data()
    language = str(values.get("language", database.language(message.from_user.id)))
    await state.set_state(Onboarding.delivery_time)
    await message.answer(
        tr(language, "delivery_time_prompt", time=settings.default_delivery_time)
    )


@router.message(Onboarding.delivery_time)
async def receive_delivery_time(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    values = await state.get_data()
    language = str(values.get("language", "ru"))
    try:
        delivery_time = parse_time(message.text or "")
    except ValueError:
        await message.answer(tr(language, "time_invalid"))
        return
    settings, database, vault, ai, image_cache = _services()
    database.set_delivery_time(message.from_user.id, delivery_time)
    first_registration = bool(values.get("first_registration", False))
    plan_date = datetime.now(ZoneInfo(settings.timezone)).date()
    if first_registration:
        database.set_schedule_start_date(
            message.from_user.id, (plan_date + timedelta(days=1)).isoformat()
        )
    database.activate_user(message.from_user.id)
    await state.clear()
    await message.answer(tr(language, "onboarding_complete"))
    if first_registration:
        await message.answer(tr(language, "first_plan_intro"))
        try:
            await deliver_plan_for_user(
                message.bot,
                database,
                image_cache,
                ContentLibrary(database, ai),
                message.from_user.id,
                plan_date.isoformat(),
                vault,
            )
        except (AIBudgetExceeded, ContentExhausted, APIError, TelegramAPIError) as exc:
            logger.error(
                "Immediate first plan failed for user %s with %s.",
                message.from_user.id,
                type(exc).__name__,
            )
            await message.answer(tr(language, "first_plan_failed"))


@router.message(Command("time"))
async def change_delivery_time(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    settings = Settings.from_env()
    database = Database(settings.database_path)
    database.initialize()
    language = database.language(message.from_user.id)
    await state.update_data(language=language)
    await state.set_state(Onboarding.delivery_time)
    await message.answer(tr(language, "change_time_prompt"))


@router.message(Command("resend"))
async def resend_daily_plan(message: Message) -> None:
    if message.from_user is None:
        return
    settings, database, _, _, image_cache = _services()
    language = database.language(message.from_user.id)
    if message.chat.type != "private":
        await message.answer(tr(language, "resend_private_only"))
        return
    if database.profile(message.from_user.id) is None:
        await message.answer(tr(language, "resend_profile_missing"))
        return
    plan_date = datetime.now(ZoneInfo(settings.timezone)).date().isoformat()
    plan = database.get_plan(message.from_user.id, plan_date)
    if plan is None or plan["status"] != "sent":
        await message.answer(tr(language, "resend_no_plan"))
        return
    try:
        await deliver_plan(
            message.bot,
            message.from_user.id,
            plan["content"],
            plan_date,
            database,
            image_cache,
            language,
            force_resend=True,
        )
    except TelegramAPIError as exc:
        logger.error(
            "User plan resend failed for %s on %s with %s.",
            message.from_user.id,
            plan_date,
            type(exc).__name__,
        )
        await message.answer(tr(language, "resend_failed"))
        return
    await message.answer(tr(language, "resend_success"))


@router.message(Command("reganaratdayplan"))
async def regenerate_daily_plan(message: Message) -> None:
    if message.from_user is None:
        return
    settings, database, vault, ai, image_cache = _services()
    language = database.language(message.from_user.id)
    if message.chat.type != "private":
        await message.answer(tr(language, "regenerate_private_only"))
        return
    telegram_id = message.from_user.id
    profile = database.profile(telegram_id)
    if profile is None or not profile["active"]:
        await message.answer(tr(language, "regenerate_profile_missing"))
        return
    plan_date = datetime.now(ZoneInfo(settings.timezone)).date().isoformat()

    apply_current_age(
        profile,
        database.encrypted_birth_date(telegram_id),
        vault,
        date.fromisoformat(plan_date),
    )
    content_library = ContentLibrary(database, ai)
    try:
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
                f"Refusing to regenerate repeated content: {', '.join(repeated)}."
            )
    except (AIBudgetExceeded, ContentExhausted, APIError) as exc:
        logger.error(
            "Daily plan regeneration failed for user %s with %s.",
            telegram_id,
            type(exc).__name__,
        )
        await message.answer(tr(language, "regenerate_generation_failed"))
        return

    database.save_plan(telegram_id, plan_date, plan, status="pending")
    try:
        await deliver_plan(
            message.bot,
            telegram_id,
            plan,
            plan_date,
            database,
            image_cache,
            language,
        )
    except (AIBudgetExceeded, APIError, TelegramAPIError) as exc:
        logger.error(
            "Regenerated plan delivery failed for user %s with %s.",
            telegram_id,
            type(exc).__name__,
        )
        await message.answer(tr(language, "regenerate_delivery_failed"))
        return
    await message.answer(tr(language, "regenerate_success"))


@router.callback_query(F.data.startswith("feedback:"))
async def receive_feedback(callback: CallbackQuery) -> None:
    _, plan_date, difficulty = callback.data.split(":", 2)
    if difficulty not in {"easy", "normal", "hard"}:
        settings = Settings.from_env()
        database = Database(settings.database_path)
        database.initialize()
        await callback.answer(
            tr(database.language(callback.from_user.id), "unknown_feedback"),
            show_alert=True,
        )
        return
    settings = Settings.from_env()
    database = Database(settings.database_path)
    database.initialize()
    language = database.language(callback.from_user.id)
    database.update_plan_feedback(callback.from_user.id, plan_date, difficulty)
    await callback.answer(tr(language, "feedback_saved"))


@router.message(Command("privacy"))
async def privacy(message: Message) -> None:
    if message.from_user is None:
        return
    settings = Settings.from_env()
    database = Database(settings.database_path)
    database.initialize()
    await message.answer(tr(database.language(message.from_user.id), "privacy"))


async def main() -> None:
    settings, database, _, ai, image_cache = _services()
    bot = Bot(token=settings.telegram_token)
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(router)
    from app.scheduler import start_scheduler

    content_library = ContentLibrary(database, ai)
    scheduler = start_scheduler(bot, database, image_cache, content_library, settings)
    try:
        await dispatcher.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
