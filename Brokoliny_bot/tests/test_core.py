from __future__ import annotations

import os
import tempfile
import asyncio
import base64
import hashlib
import io
import json
import sqlite3
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from app.ai import AIOrchestrator
from app.age import apply_current_age, completed_age_months, parse_birth_date
from app.bot import (
    regenerate_daily_plan,
    resend_daily_plan,
    _start_assessment,
    _send_assessment_question,
    parse_time,
    validate_identity_name,
)
from app.content_library import ContentExhausted, ContentLibrary
from app.config import Settings
from app.database import Database
from app.image_cache import ImageCache
from app.plans import deliver_plan, feedback_keyboard, plan_message
from app.printables import make_stimulus, make_worksheet
from app.schemas import AssessmentResponse, DailyPlan
from app.security import SecretVault, valid_admin_token


def _picture_png(center_x: int, color: str, *, optimize: bool = False) -> bytes:
    image = Image.new("RGB", (96, 64), "white")
    ImageDraw.Draw(image).ellipse(
        (center_x - 14, 18, center_x + 14, 46), fill=color, outline="black", width=3
    )
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=optimize)
    return output.getvalue()


class AgeParsingTests(unittest.TestCase):
    def test_birth_date_parser_accepts_supported_formats_and_age_range(self) -> None:
        today = date(2026, 11, 14)
        self.assertEqual(parse_birth_date("15.11.2023", today), date(2023, 11, 15))
        self.assertEqual(parse_birth_date("2023-11-15", today), date(2023, 11, 15))
        self.assertEqual(completed_age_months(date(2022, 11, 15), today), 47)
        self.assertEqual(completed_age_months(date(2022, 11, 15), date(2026, 11, 15)), 48)
        self.assertEqual(completed_age_months(date(2024, 1, 31), date(2024, 2, 29)), 1)

    def test_birth_date_parser_rejects_future_dates_and_out_of_range_ages(self) -> None:
        today = date(2026, 11, 14)
        for value in ("15.11.2026", "15.04.2026", "14.10.2019", "not a date"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_birth_date(value, today)

    def test_delivery_time_is_normalized_and_validated(self) -> None:
        self.assertEqual(parse_time(" 9:05 "), "09:05")
        with self.assertRaises(ValueError):
            parse_time("25:10")


class PersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary_directory.name) / "test.sqlite3")
        self.database.initialize()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_database_seeds_routes_and_versions_prompt_updates(self) -> None:
        self.assertEqual(self.database.get_route("diagnostics")["model"], "gemini-2.5-pro")
        self.assertIn("image", {prompt["name"] for prompt in self.database.list_prompts()})
        self.database.upsert_profile(78, 40, "Ж", "09:00", None)
        self.assertEqual(self.database.due_users("09:00", "2026-11-14"), [])
        self.database.activate_user(78)
        self.assertEqual(len(self.database.due_users("09:00", "2026-11-14")), 1)
        version = self.database.update_prompt(
            "diagnostics", "A replacement prompt with sufficient content for validation."
        )
        self.assertEqual(version, 2)
        self.assertEqual(self.database.get_prompt("diagnostics"), "A replacement prompt with sufficient content for validation.")
        self.assertEqual(self.database.prompt_history("diagnostics")[0]["version"], 1)

    def test_image_generation_reads_current_versioned_prompt(self) -> None:
        vault = SecretVault(Fernet.generate_key().decode("ascii"))
        ai = AIOrchestrator(
            self.database,
            vault,
            daily_budget_usd=1,
            max_tokens=1000,
        )
        response = SimpleNamespace(
            data=[
                SimpleNamespace(
                    b64_json=base64.b64encode(b"image-bytes").decode("ascii")
                )
            ],
            _hidden_params={},
        )
        default_prompt = self.database.get_prompt("image")
        replacement_prompt = (
            "A simple flat paper-cut illustration, centered on plain cream background."
        )
        with patch(
            "app.ai.litellm.aimage_generation",
            new_callable=AsyncMock,
            return_value=response,
        ) as generate_image:
            asyncio.run(ai.generate_image("a red apple"))
            self.database.update_prompt("image", replacement_prompt)
            asyncio.run(ai.generate_image("a blue cup"))

        self.assertEqual(
            generate_image.await_args_list[0].kwargs["prompt"],
            f"{default_prompt} a red apple",
        )
        self.assertEqual(
            generate_image.await_args_list[1].kwargs["prompt"],
            f"{replacement_prompt} a blue cup",
        )
        self.assertEqual(self.database.prompt_history("image")[0]["version"], 1)

    def test_profile_uses_latest_skill_result_and_admin_key_stays_encrypted(self) -> None:
        vault = SecretVault(Fernet.generate_key().decode("ascii"))
        raw_note = "не передавать без согласия"
        encrypted = vault.encrypt(raw_note)
        self.assertNotIn(raw_note, encrypted)
        self.database.upsert_profile(44, 40, "Ж", "09:00", encrypted)
        self.database.record_skill(44, "shape_sorting", "cognitive", "prompted")
        self.database.record_skill(44, "shape_sorting", "cognitive", "independent")
        profile = self.database.profile(44)
        self.assertEqual(profile["skills"][0]["result"], "independent")
        self.assertEqual(
            vault.decrypt(self.database.encrypted_diagnosis_notes(44)),
            raw_note,
        )

    def test_encrypted_birth_date_recalculates_age_for_each_plan_date(self) -> None:
        vault = SecretVault(Fernet.generate_key().decode("ascii"))
        encrypted_birth_date = vault.encrypt("2023-07-14")
        self.database.upsert_profile(
            42,
            40,
            "Ж",
            "09:00",
            None,
            encrypted_birth_date=encrypted_birth_date,
        )
        self.assertNotIn("birth_date_encrypted", self.database.profile(42))
        current_profile = self.database.profile(42)
        apply_current_age(
            current_profile,
            self.database.encrypted_birth_date(42),
            vault,
            date(2026, 11, 15),
        )
        self.assertEqual(current_profile["age_months"], 40)
        next_month_profile = self.database.profile(42)
        apply_current_age(
            next_month_profile,
            self.database.encrypted_birth_date(42),
            vault,
            date(2026, 12, 15),
        )
        self.assertEqual(next_month_profile["age_months"], 41)

    def test_selected_language_is_saved_and_retained_in_profile(self) -> None:
        self.database.set_language(43, "uk", "09:00")
        self.database.upsert_profile(43, 40, "Ж", "09:00", None)
        self.assertEqual(self.database.profile(43)["language"], "uk")
        self.assertEqual(self.database.list_users()[0]["language"], "uk")
        self.assertEqual(self.database.language(43), "uk")
        with self.assertRaises(ValueError):
            self.database.set_language(43, "en", "09:00")

    def test_identity_data_requires_consent_and_is_encrypted_until_revoked(self) -> None:
        vault = SecretVault(Fernet.generate_key().decode("ascii"))
        self.database.set_language(48, "ru", "09:00")
        self.database.upsert_profile(48, 40, "Ж", "09:00", None)
        self.assertEqual(self.database.identity_consent_status(48), "pending")
        identity = {
            "full_name": "Александр Иванов",
            "child_name": "Миша",
            "phone_number": "+380501234567",
        }
        encrypted = vault.encrypt(json.dumps(identity, ensure_ascii=False))
        self.assertNotIn("Александр Иванов", encrypted)
        with self.assertRaises(ValueError):
            self.database.save_admin_identity(48, encrypted)
        self.database.set_identity_consent_status(48, "collecting")
        self.database.save_admin_identity(48, encrypted)
        self.assertEqual(self.database.identity_consent_status(48), "granted")
        self.assertEqual(
            json.loads(vault.decrypt(self.database.admin_identity_encrypted(48))),
            identity,
        )
        self.assertNotIn("full_name", self.database.profile(48))
        self.database.set_identity_consent_status(48, "denied")
        self.assertIsNone(self.database.admin_identity_encrypted(48))

    def test_changing_language_discards_an_unsent_plan(self) -> None:
        self.database.upsert_profile(46, 40, "Ж", "09:00", None)
        plan = {
            "focus_task": {"title": "Считаем"},
            "activity": {"title": "Игра"},
            "parent_advice": "Предложите выбор.",
        }
        self.database.save_plan(46, "2026-11-15", plan, status="pending")
        self.database.set_language(46, "uk", "09:00")
        self.assertIsNone(self.database.get_plan(46, "2026-11-15"))

    def test_existing_database_migrates_user_language_to_russian(self) -> None:
        legacy_path = Path(self.temporary_directory.name) / "legacy.sqlite3"
        with sqlite3.connect(legacy_path) as connection:
            connection.execute(
                "CREATE TABLE users (telegram_id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, "
                "active INTEGER NOT NULL DEFAULT 1, subscription_status TEXT NOT NULL DEFAULT 'free', "
                "delivery_time TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO users(telegram_id, created_at, delivery_time) "
                "VALUES (99, '2026-01-01', '09:00')"
            )
            connection.execute(
                "CREATE TABLE child_profiles ("
                "telegram_id INTEGER PRIMARY KEY, age_months INTEGER NOT NULL, "
                "gender TEXT NOT NULL, diagnosis_notes_encrypted TEXT, "
                "interests TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO child_profiles(telegram_id, age_months, gender, updated_at) "
                "VALUES (99, 40, 'Ж', '2026-01-01')"
            )
        legacy_database = Database(legacy_path)
        legacy_database.initialize()
        self.assertEqual(legacy_database.language(99), "ru")
        with legacy_database.connect() as connection:
            row = connection.execute(
                "SELECT schedule_start_date, identity_consent FROM users "
                "WHERE telegram_id = 99"
            ).fetchone()
        self.assertEqual(row["schedule_start_date"], "0001-01-01")
        self.assertEqual(row["identity_consent"], "pending")
        self.assertIsNone(legacy_database.encrypted_birth_date(99))
        vault = SecretVault(Fernet.generate_key().decode("ascii"))
        legacy_database.upsert_profile(
            99,
            40,
            "Ж",
            "09:00",
            None,
            encrypted_birth_date=vault.encrypt("2023-07-14"),
        )
        self.assertEqual(
            vault.decrypt(legacy_database.encrypted_birth_date(99)),
            "2023-07-14",
        )

    def test_schedule_skips_registration_day_and_starts_on_configured_date(self) -> None:
        self.database.upsert_profile(47, 40, "Ж", "09:00", None)
        self.database.activate_user(47)
        self.database.set_schedule_start_date(47, "2026-11-15")
        self.assertEqual(self.database.due_users("09:00", "2026-11-14"), [])
        self.assertEqual(
            self.database.due_users("09:00", "2026-11-15")[0]["telegram_id"],
            47,
        )

    def test_deleting_user_cascades_personal_data_and_keeps_shared_content(self) -> None:
        telegram_id = 45
        self.database.upsert_profile(telegram_id, 40, "Ж", "09:00", "encrypted-note")
        self.database.record_skill(telegram_id, "shape_sorting", "cognitive", "independent")
        self.database.save_plan(
            telegram_id,
            "2026-11-14",
            {
                "focus_task": {"title": "Сортировка"},
                "activity": {"title": "Фигуры"},
                "parent_advice": "Предложите выбор.",
            },
        )
        self.database.set_generation_cooldown(telegram_id, "assessment")
        self.database.cache_content(
            "activity", "3-4y", "shared-item", "shared-hash", {"title": "Общая игра"}
        )

        self.assertTrue(self.database.delete_user(telegram_id))
        with self.database.connect() as connection:
            for table in (
                "users",
                "child_profiles",
                "skill_observations",
                "plans",
                "user_content_usage",
                "content_generation_cooldowns",
            ):
                count = connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE telegram_id = ?",
                    (telegram_id,),
                ).fetchone()[0]
                self.assertEqual(count, 0, table)
            shared_content_count = connection.execute(
                "SELECT COUNT(*) FROM content_library WHERE content_hash = ?",
                ("shared-hash",),
            ).fetchone()[0]
        self.assertEqual(shared_content_count, 1)
        self.assertFalse(self.database.delete_user(telegram_id))

    def test_pending_plan_is_reused_and_due_users_are_timezone_clock_based(self) -> None:
        self.database.upsert_profile(77, 36, "М", "08:45", None)
        self.database.activate_user(77)
        self.assertEqual(self.database.due_users("08:50", "2026-11-14")[0]["telegram_id"], 77)
        self.assertEqual(self.database.due_users("08:30", "2026-11-14"), [])
        plan = {
            "focus_task": {"title": "Редактированный план", "worksheet": {}},
            "activity": {"title": "Игра", "materials": [], "parent_script": "", "age_facts": [], "story_prompt": None},
            "parent_advice": "Предложите ребёнку выбор.",
        }
        self.database.save_plan(77, "2026-11-14", plan, status="pending")
        self.assertEqual(
            self.database.get_plan(77, "2026-11-14")["content"],
            plan,
        )

    def test_admin_token_comparison_rejects_missing_or_wrong_values(self) -> None:
        self.assertTrue(valid_admin_token("secret", "secret"))
        self.assertFalse(valid_admin_token("wrong", "secret"))
        self.assertFalse(valid_admin_token(None, "secret"))


class AssessmentOnboardingTests(unittest.TestCase):
    def test_callback_assessment_saves_profile_for_the_user_not_bot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Database(Path(temporary_directory) / "assessment.sqlite3")
            database.initialize()
            user_id = 184127605
            bot_id = 9024589361
            today = date.today()
            birth_date = date(today.year - 2, 1, 1)
            settings = SimpleNamespace(
                timezone="Europe/Kyiv",
                default_delivery_time="09:00",
            )
            vault = SecretVault(Fernet.generate_key().decode("ascii"))
            message = SimpleNamespace(
                from_user=SimpleNamespace(id=bot_id),
                answer=AsyncMock(),
            )
            state = SimpleNamespace(
                update_data=AsyncMock(),
                set_state=AsyncMock(),
            )

            with (
                patch("app.bot.ContentLibrary") as content_library,
                patch("app.bot._send_assessment_question", new_callable=AsyncMock),
            ):
                content_library.return_value.assessment_questions = AsyncMock(
                    return_value=[]
                )
                asyncio.run(
                    _start_assessment(
                        message,
                        user_id,
                        state,
                        settings,
                        database,
                        vault,
                        object(),
                        birth_date,
                        "Ж",
                        "ru",
                        None,
                    )
                )

            self.assertIsNotNone(database.profile(user_id))
            self.assertIsNone(database.profile(bot_id))
            self.assertEqual(
                content_library.return_value.assessment_questions.await_args.args[0],
                user_id,
            )


class ImageCacheTests(unittest.TestCase):
    class NoAI:
        async def generate_image(self, _prompt: str) -> bytes:
            del _prompt
            raise AssertionError("Static stimulus generation must not call an AI model.")

    def test_images_are_reused_between_users_but_not_repeated_for_one_user(self) -> None:
        class FakeAI:
            calls = 0

            async def generate_image(self, _prompt: str) -> bytes:
                del _prompt
                self.calls += 1
                if self.calls == 1:
                    return _picture_png(20, "red")
                return _picture_png(72, "blue")

        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Database(Path(temporary_directory) / "cache.sqlite3")
            database.initialize()
            database.upsert_profile(10, 36, "М", "09:00", None)
            database.upsert_profile(11, 36, "Ж", "09:00", None)
            fake_ai = FakeAI()
            cache = ImageCache(database, fake_ai)
            first = asyncio.run(cache.get_or_generate("a cat", "A cat", 10))
            cache.remember_telegram_file(first[2], "telegram-file-id", 10)
            reused = asyncio.run(cache.get_or_generate("a cat", "A cat", 11))
            cache.remember_telegram_file(reused[2], reused[1], 11)
            second = asyncio.run(cache.get_or_generate("a cat", "A cat", 10))
            self.assertEqual(reused[1], "telegram-file-id")
            self.assertNotEqual(first[0], second[0])
            self.assertNotEqual(hashlib.sha256(first[0]).digest(), hashlib.sha256(second[0]).digest())
            self.assertEqual(fake_ai.calls, 2)

    def test_reencoded_but_visually_identical_picture_is_not_reused(self) -> None:
        class FakeAI:
            calls = 0

            async def generate_image(self, prompt: str) -> bytes:
                del prompt
                self.calls += 1
                if self.calls == 1:
                    return _picture_png(24, "green", optimize=False)
                if self.calls == 2:
                    return _picture_png(24, "green", optimize=True)
                return _picture_png(72, "blue")

        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Database(Path(temporary_directory) / "visual-dedupe.sqlite3")
            database.initialize()
            database.upsert_profile(13, 36, "М", "09:00", None)
            fake_ai = FakeAI()
            cache = ImageCache(database, fake_ai)
            first = asyncio.run(cache.get_or_generate("green circle", "green circle", 13))
            cache.remember_telegram_file(first[2], "file-1", 13)
            second = asyncio.run(
                cache.get_or_generate("same green circle", "same green circle", 13)
            )
            self.assertNotEqual(first[0], second[0])
            self.assertEqual(fake_ai.calls, 3)

    def test_vector_stimulus_cache_skips_a_picture_seen_by_same_user(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Database(Path(temporary_directory) / "stimuli.sqlite3")
            database.initialize()
            database.upsert_profile(12, 36, "М", "09:00", None)
            cache = ImageCache(database, self.NoAI())
            first = asyncio.run(cache.get_unique_stimulus(12, "shapes", "question-1"))
            cache.remember_telegram_file(first[2], "file-1", 12)
            second = asyncio.run(cache.get_unique_stimulus(12, "shapes", "question-2"))
            self.assertNotEqual(first[0], second[0])


class StructuredInputTests(unittest.TestCase):
    def test_assessment_schema_requires_cross_domain_observations(self) -> None:
        question = {
            "skill_id": "jump_and_name",
            "domains": ["motor", "language"],
            "parent_script": "Прыгните и назовите животное.",
            "stimulus_kind": "animals",
            "stimulus_prompt": None,
        }
        response = AssessmentResponse.model_validate({"questions": [question] * 4})
        self.assertEqual(response.questions[0].domains, ["motor", "language"])
        with self.assertRaises(ValueError):
            AssessmentResponse.model_validate(
                {"questions": [{**question, "domains": ["motor"]}] * 4}
            )

    def test_assessment_questions_allow_no_visual_stimulus(self) -> None:
        question = {
            "skill_id": "jump_and_name",
            "domains": ["motor", "language"],
            "parent_script": "Попросите ребёнка прыгнуть и назвать животное.",
            "stimulus_kind": None,
            "stimulus_prompt": None,
        }
        response = AssessmentResponse.model_validate({"questions": [question] * 4})
        self.assertIsNone(response.questions[0].stimulus_kind)

    def test_assessment_without_stimulus_sends_text_only(self) -> None:
        message = AsyncMock()
        question = {
            "parent_script": "Попросите ребёнка попрыгать и назвать животное.",
            "stimulus_kind": None,
        }
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "no-image.sqlite3")
            database.initialize()
            image_cache = ImageCache(database, ImageCacheTests.NoAI())
            asyncio.run(
                _send_assessment_question(
                    message, [question], 0, image_cache, 123, "uk"
                )
            )
        message.answer.assert_awaited_once()
        self.assertEqual(
            message.answer.await_args.args[0],
            question["parent_script"],
        )
        message.answer_photo.assert_not_awaited()

    def test_plan_and_feedback_are_localized(self) -> None:
        plan = {
            "focus_task": {"title": "Рахуємо фігури"},
            "activity": {
                "title": "Рухлива гра",
                "materials": ["кубики"],
                "parent_script": "Побудуйте вежу разом.",
                "age_facts": ["Факт один.", "Факт два."],
            },
            "parent_advice": "Запропонуйте дитині вибір.",
        }
        message = plan_message(plan, "uk")
        self.assertIn("Фокусне завдання", message)
        self.assertIn("Порада батькам", message)
        keyboard = feedback_keyboard("2026-11-14", "uk")
        self.assertEqual(
            [button.text for button in keyboard.inline_keyboard[0]],
            ["Легко", "Нормально", "Складно"],
        )


class ContentLibraryTests(unittest.TestCase):
    class FakeAI:
        def __init__(self) -> None:
            self.assessment_calls = 0
            self.plan_calls = 0

        async def generate_assessment(
            self,
            age_months: int,
            accessibility_notes: str = "",
            *,
            language: str = "ru",
            excluded_skill_ids: list[str] | None = None,
            excluded_questions: list[str] | None = None,
        ) -> AssessmentResponse:
            del age_months, accessibility_notes, language, excluded_skill_ids, excluded_questions
            self.assessment_calls += 1
            scripts = (
                (
                    "Прыгните через линию и назовите домашних животных.",
                    "Разложите кубики по цвету, считая каждый вслух.",
                    "Покажите на картинке самый большой предмет и опишите его.",
                    "Прокатите мяч партнёру и попросите его назвать форму.",
                )
                if self.assessment_calls % 2
                else (
                    "Пройдите по следам и назовите предметы одежды.",
                    "Соберите башню из деталей и сравните её высоту.",
                    "Повторите ритм хлопков и назовите знакомую песню.",
                    "Разложите игрушечную еду и расскажите, кому она нужна.",
                )
            )
            return AssessmentResponse.model_validate(
                {
                    "questions": [
                        {
                            "skill_id": f"skill_{self.assessment_calls}_{index}",
                            "domains": ["motor", "language"],
                            "parent_script": scripts[index],
                            "stimulus_kind": "animals",
                            "stimulus_prompt": None,
                        }
                        for index in range(4)
                    ]
                }
            )

        async def generate_plan(
            self,
            profile: dict[str, object],
            gaps: list[dict[str, object]],
            feedback: str,
            *,
            avoid_recent: dict[str, list[str]] | None = None,
        ) -> DailyPlan:
            del profile, gaps, feedback, avoid_recent
            self.plan_calls += 1
            n = self.plan_calls
            variants = (
                {
                    "task": "Считаем синие круги",
                    "type": "counting",
                    "shape": "circle",
                    "instructions": "Найдите все синие круги на листе.",
                    "question": "Сколько синих кругов получилось?",
                    "activity": "Переливаем воду",
                    "materials": ["ложка", "чашка"],
                    "script": "Перелейте воду ложкой и сравните полную и пустую чашки.",
                    "advice": "Предложите ребёнку выбрать, с какой чашки начать, и дождитесь ответа.",
                },
                {
                    "task": "Продолжаем узор",
                    "type": "pattern",
                    "shape": "mixed",
                    "instructions": "Посмотрите на ряд фигур и найдите, что идёт дальше.",
                    "question": "Какая фигура повторяет порядок?",
                    "activity": "Строим мост",
                    "materials": ["книги", "кубики"],
                    "script": "Постройте мост и проверьте, какая игрушка пройдёт под ним.",
                    "advice": "Опишите спокойным голосом чувство ребёнка, затем предложите два варианта решения.",
                },
                {
                    "task": "Сравниваем треугольники",
                    "type": "geometry",
                    "shape": "triangle",
                    "instructions": "Найдите фигуры с тремя углами и обведите их.",
                    "question": "У какого треугольника стороны длиннее?",
                    "activity": "Ищем звуки вокруг",
                    "materials": ["бумага", "карандаш"],
                    "script": "Послушайте звуки за окном и по очереди изобразите каждый движением.",
                    "advice": "Во время совместной игры замечайте усилие ребёнка и конкретно называйте, что у него вышло.",
                },
            )
            variant = variants[(n - 1) % len(variants)]
            return DailyPlan.model_validate(
                {
                    "focus_task": {
                        "title": variant["task"],
                        "worksheet": {
                            "title": variant["task"],
                            "activity_type": variant["type"],
                            "shape": variant["shape"],
                            "count": 3,
                            "instructions": variant["instructions"],
                            "questions": [variant["question"]],
                        },
                    },
                    "activity": {
                        "title": variant["activity"],
                        "materials": variant["materials"],
                        "parent_script": variant["script"],
                        "age_facts": [
                            f"Наблюдайте за изменениями во время занятия {n}.",
                            f"Сравните, что изменилось после игры {n}.",
                        ],
                        "story_prompt": f"Уникальная картинка для игры {n}.",
                    },
                    "parent_advice": variant["advice"],
                }
            )

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary_directory.name) / "content.sqlite3")
        self.database.initialize()
        self.database.upsert_profile(20, 40, "М", "09:00", None)
        self.database.upsert_profile(21, 40, "Ж", "09:00", None)
        self.ai = self.FakeAI()
        self.library = ContentLibrary(self.database, self.ai)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_assessment_questions_are_cached_shared_and_never_reissued(self) -> None:
        first = asyncio.run(self.library.assessment_questions(20, 40))
        shared = asyncio.run(self.library.assessment_questions(21, 40))
        next_set = asyncio.run(self.library.assessment_questions(20, 40))
        self.assertEqual(len(first), 4)
        self.assertEqual(len(shared), 4)
        self.assertEqual(len(next_set), 4)
        self.assertEqual(
            {question["skill_id"] for question in first},
            {question["skill_id"] for question in shared},
        )
        self.assertTrue(
            {question["skill_id"] for question in first}.isdisjoint(
                question["skill_id"] for question in next_set
            )
        )
        self.assertEqual(self.ai.assessment_calls, 2)

    def test_assessment_cache_and_plan_cohort_are_separated_by_language(self) -> None:
        self.database.upsert_profile(22, 40, "Ж", "09:00", None)
        asyncio.run(self.library.assessment_questions(22, 40, language="ru"))
        asyncio.run(self.library.assessment_questions(22, 40, language="uk"))
        self.assertEqual(self.ai.assessment_calls, 2)
        russian_profile = {"age_months": 40, "skills": [], "language": "ru"}
        ukrainian_profile = {"age_months": 40, "skills": [], "language": "uk"}
        self.assertNotEqual(
            self.library.plan_cohort(russian_profile, "normal"),
            self.library.plan_cohort(ukrainian_profile, "normal"),
        )
        younger_profile = {"age_months": 40, "skills": [], "language": "ru"}
        older_profile = {"age_months": 41, "skills": [], "language": "ru"}
        self.assertNotEqual(
            self.library.plan_cohort(younger_profile, "normal"),
            self.library.plan_cohort(older_profile, "normal"),
        )

    def test_daily_components_are_cached_and_only_generated_when_pool_is_empty(self) -> None:
        profile = self.database.profile(20)
        first = asyncio.run(
            self.library.daily_plan(20, profile, "normal", "2026-11-14")
        )
        self.database.save_plan(20, "2026-11-14", first, status="pending")
        self.assertEqual(
            set(
                self.library.repeated_plan_components(
                    20, first, excluding_plan_date="2026-11-15"
                )
            ),
            {"focus_task", "activity", "advice"},
        )
        other_user = asyncio.run(
            self.library.daily_plan(21, self.database.profile(21), "normal", "2026-11-14")
        )
        second = asyncio.run(
            self.library.daily_plan(20, profile, "normal", "2026-11-15")
        )
        self.assertEqual(self.ai.plan_calls, 2)
        self.assertEqual(first["focus_task"], other_user["focus_task"])
        self.assertNotEqual(first["focus_task"], second["focus_task"])
        self.assertNotEqual(first["parent_advice"], second["parent_advice"])
        self.assertEqual(
            self.library.repeated_plan_components(
                20, first, excluding_plan_date="2026-11-14"
            ),
            [],
        )

    def test_exhausted_question_pool_uses_cooldown_instead_of_repeated_ai_calls(self) -> None:
        class RepeatingAI:
            calls = 0

            async def generate_assessment(
                self,
                age_months: int,
                accessibility_notes: str = "",
                *,
                language: str = "ru",
                excluded_skill_ids: list[str] | None = None,
                excluded_questions: list[str] | None = None,
            ) -> AssessmentResponse:
                del age_months, accessibility_notes, language, excluded_skill_ids, excluded_questions
                self.calls += 1
                scripts = (
                    "Прыгните через верёвку и назовите птиц, которых знаете.",
                    "Сложите кубики в корзину, считая их по одному.",
                    "Покажите на рисунке большой предмет и опишите его цвет.",
                    "Прокатите мяч партнёру и попросите его придумать движение.",
                )
                return AssessmentResponse.model_validate(
                    {
                        "questions": [
                            {
                                "skill_id": f"fixed_skill_{index}",
                                "domains": ["motor", "language"],
                                "parent_script": scripts[index],
                                "stimulus_kind": "animals",
                                "stimulus_prompt": None,
                            }
                            for index in range(4)
                        ]
                    }
                )

        ai = RepeatingAI()
        library = ContentLibrary(self.database, ai)
        asyncio.run(library.assessment_questions(20, 40))
        with self.assertRaises(RuntimeError):
            asyncio.run(library.assessment_questions(20, 40))
        calls_after_exhaustion = ai.calls
        with self.assertRaises(RuntimeError):
            asyncio.run(library.assessment_questions(20, 40))
        self.assertEqual(ai.calls, calls_after_exhaustion)


class ResendCommandTests(unittest.TestCase):
    def test_resend_sends_only_saved_sent_plan_without_generating(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Database(Path(temporary_directory) / "resend.sqlite3")
            database.initialize()
            telegram_id = 63
            database.upsert_profile(telegram_id, 40, "Ж", "09:00", None)
            settings = SimpleNamespace(timezone="UTC")
            plan_date = datetime.now(ZoneInfo("UTC")).date().isoformat()
            plan = {
                "focus_task": {"title": "Считаем"},
                "activity": {
                    "title": "Играем",
                    "materials": [],
                    "parent_script": "Предложите игру.",
                    "age_facts": [],
                    "story_prompt": None,
                },
                "parent_advice": "Дайте ребёнку время.",
            }
            database.save_plan(telegram_id, plan_date, plan, status="sent")
            image_cache = ImageCache(database, object())
            message = SimpleNamespace(
                from_user=SimpleNamespace(id=telegram_id),
                chat=SimpleNamespace(type="private"),
                bot=object(),
                answer=AsyncMock(),
            )

            with (
                patch(
                    "app.bot._services",
                    return_value=(settings, database, None, None, image_cache),
                ),
                patch("app.bot.deliver_plan", new_callable=AsyncMock) as send_plan,
            ):
                asyncio.run(resend_daily_plan(message))

            send_plan.assert_awaited_once()
            self.assertEqual(send_plan.await_args.args[2], plan)
            self.assertTrue(send_plan.await_args.kwargs["force_resend"])
            message.answer.assert_awaited_once()

    def test_resend_reports_when_today_has_no_sent_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Database(Path(temporary_directory) / "resend.sqlite3")
            database.initialize()
            database.upsert_profile(64, 40, "Ж", "09:00", None)
            settings = SimpleNamespace(timezone="UTC")
            image_cache = ImageCache(database, object())
            message = SimpleNamespace(
                from_user=SimpleNamespace(id=64),
                chat=SimpleNamespace(type="private"),
                bot=object(),
                answer=AsyncMock(),
            )

            with (
                patch(
                    "app.bot._services",
                    return_value=(settings, database, None, None, image_cache),
                ),
                patch("app.bot.deliver_plan", new_callable=AsyncMock) as send_plan,
            ):
                asyncio.run(resend_daily_plan(message))

            send_plan.assert_not_awaited()
            message.answer.assert_awaited_once()
            self.assertIn(
                "/reganaratdayplan",
                message.answer.await_args.args[0],
            )

    def test_resend_prompts_for_registration_when_profile_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Database(Path(temporary_directory) / "resend.sqlite3")
            database.initialize()
            settings = SimpleNamespace(timezone="UTC")
            image_cache = ImageCache(database, object())
            message = SimpleNamespace(
                from_user=SimpleNamespace(id=67),
                chat=SimpleNamespace(type="private"),
                bot=object(),
                answer=AsyncMock(),
            )

            with (
                patch(
                    "app.bot._services",
                    return_value=(settings, database, None, None, image_cache),
                ),
                patch("app.bot.deliver_plan", new_callable=AsyncMock) as send_plan,
            ):
                asyncio.run(resend_daily_plan(message))

            send_plan.assert_not_awaited()
            self.assertIn("/start", message.answer.await_args.args[0])


class RegenerateCommandTests(unittest.TestCase):
    @staticmethod
    def _plan(title: str) -> dict[str, object]:
        return {
            "focus_task": {"title": title},
            "activity": {
                "title": f"Игра {title}",
                "materials": [],
                "parent_script": "Предложите игру.",
                "age_facts": [],
                "story_prompt": None,
            },
            "parent_advice": f"Совет {title}.",
        }

    def test_regeneration_replaces_today_plan_and_clears_old_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Database(Path(temporary_directory) / "regenerate.sqlite3")
            database.initialize()
            telegram_id = 65
            database.upsert_profile(telegram_id, 40, "Ж", "09:00", None)
            database.activate_user(telegram_id)
            settings = SimpleNamespace(timezone="UTC")
            plan_date = datetime.now(ZoneInfo("UTC")).date().isoformat()
            original_plan = self._plan("Старое задание")
            new_plan = self._plan("Новое задание")
            database.save_plan(telegram_id, plan_date, original_plan, status="sent")
            database.update_plan_feedback(telegram_id, plan_date, "hard")
            image_cache = ImageCache(database, object())
            vault = SecretVault(Fernet.generate_key().decode("ascii"))
            message = SimpleNamespace(
                from_user=SimpleNamespace(id=telegram_id),
                chat=SimpleNamespace(type="private"),
                bot=object(),
                answer=AsyncMock(),
            )
            library = SimpleNamespace(
                daily_plan=AsyncMock(return_value=new_plan),
                repeated_plan_components=lambda *_args, **_kwargs: [],
            )

            async def mark_delivered(
                _bot: object,
                _telegram_id: int,
                _plan: dict[str, object],
                delivery_date: str,
                database: Database,
                _image_cache: ImageCache,
                _language: str,
            ) -> None:
                database.mark_plan_sent(_telegram_id, delivery_date)

            with (
                patch(
                    "app.bot._services",
                    return_value=(settings, database, vault, object(), image_cache),
                ),
                patch("app.bot.ContentLibrary", return_value=library),
                patch(
                    "app.bot.deliver_plan",
                    new_callable=AsyncMock,
                    side_effect=mark_delivered,
                ),
            ):
                asyncio.run(regenerate_daily_plan(message))

            regenerated = database.get_plan(telegram_id, plan_date)
            self.assertEqual(regenerated["content"], new_plan)
            self.assertEqual(regenerated["status"], "sent")
            self.assertIsNone(regenerated["feedback"])
            message.answer.assert_awaited_once()

    def test_regeneration_creates_todays_first_plan_if_profile_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Database(Path(temporary_directory) / "regenerate.sqlite3")
            database.initialize()
            telegram_id = 68
            database.upsert_profile(telegram_id, 40, "Ж", "09:00", None)
            database.activate_user(telegram_id)
            settings = SimpleNamespace(timezone="UTC")
            plan_date = datetime.now(ZoneInfo("UTC")).date().isoformat()
            new_plan = self._plan("Первое задание")
            image_cache = ImageCache(database, object())
            vault = SecretVault(Fernet.generate_key().decode("ascii"))
            message = SimpleNamespace(
                from_user=SimpleNamespace(id=telegram_id),
                chat=SimpleNamespace(type="private"),
                bot=object(),
                answer=AsyncMock(),
            )
            library = SimpleNamespace(
                daily_plan=AsyncMock(return_value=new_plan),
                repeated_plan_components=lambda *_args, **_kwargs: [],
            )

            async def mark_delivered(
                _bot: object,
                _telegram_id: int,
                _plan: dict[str, object],
                delivery_date: str,
                database: Database,
                _image_cache: ImageCache,
                _language: str,
            ) -> None:
                database.mark_plan_sent(_telegram_id, delivery_date)

            with (
                patch(
                    "app.bot._services",
                    return_value=(settings, database, vault, object(), image_cache),
                ),
                patch("app.bot.ContentLibrary", return_value=library),
                patch(
                    "app.bot.deliver_plan",
                    new_callable=AsyncMock,
                    side_effect=mark_delivered,
                ),
            ):
                asyncio.run(regenerate_daily_plan(message))

            plan = database.get_plan(telegram_id, plan_date)
            self.assertEqual(plan["content"], new_plan)
            self.assertEqual(plan["status"], "sent")
            library.daily_plan.assert_awaited_once()

    def test_generation_failure_keeps_previous_plan_intact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Database(Path(temporary_directory) / "regenerate.sqlite3")
            database.initialize()
            telegram_id = 66
            database.upsert_profile(telegram_id, 40, "Ж", "09:00", None)
            database.activate_user(telegram_id)
            settings = SimpleNamespace(timezone="UTC")
            plan_date = datetime.now(ZoneInfo("UTC")).date().isoformat()
            original_plan = self._plan("Сохранённое задание")
            database.save_plan(telegram_id, plan_date, original_plan, status="sent")
            image_cache = ImageCache(database, object())
            vault = SecretVault(Fernet.generate_key().decode("ascii"))
            message = SimpleNamespace(
                from_user=SimpleNamespace(id=telegram_id),
                chat=SimpleNamespace(type="private"),
                bot=object(),
                answer=AsyncMock(),
            )
            library = SimpleNamespace(
                daily_plan=AsyncMock(side_effect=ContentExhausted("No fresh content")),
                repeated_plan_components=lambda *_args, **_kwargs: [],
            )

            with (
                patch(
                    "app.bot._services",
                    return_value=(settings, database, vault, object(), image_cache),
                ),
                patch("app.bot.ContentLibrary", return_value=library),
                patch("app.bot.deliver_plan", new_callable=AsyncMock) as send_plan,
            ):
                asyncio.run(regenerate_daily_plan(message))

            self.assertEqual(database.get_plan(telegram_id, plan_date)["content"], original_plan)
            send_plan.assert_not_awaited()
            message.answer.assert_awaited_once()


class PlanDeliveryTests(unittest.TestCase):
    def test_successful_plan_parts_are_not_resent_after_partial_delivery(self) -> None:
        class FakeBot:
            documents = 0
            messages = 0

            async def send_document(self, *_args: object, **_kwargs: object) -> None:
                del _args, _kwargs
                self.documents += 1

            async def send_message(self, *_args: object, **_kwargs: object) -> None:
                del _args, _kwargs
                self.messages += 1
                if self.messages == 1:
                    raise RuntimeError("simulated Telegram failure")

        font_candidates = (
            os.getenv("FONT_PATH"),
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/Library/Fonts/Arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "C:/Windows/Fonts/arial.ttf",
        )
        if not any(path and Path(path).is_file() for path in font_candidates):
            self.skipTest("No supported Cyrillic font is installed.")
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Database(Path(temporary_directory) / "delivery.sqlite3")
            database.initialize()
            database.upsert_profile(30, 36, "М", "09:00", None)
            plan = {
                "focus_task": {
                    "title": "Считаем круги",
                    "worksheet": {
                        "title": "Круги",
                        "activity_type": "counting",
                        "shape": "circle",
                        "count": 3,
                        "instructions": "Посчитайте круги на листе.",
                        "questions": ["Сколько кругов?"],
                    },
                },
                "activity": {
                    "title": "Лепим",
                    "materials": ["пластилин"],
                    "parent_script": "Скатайте шар и назовите его форму.",
                    "age_facts": ["Факт первый.", "Факт второй."],
                    "story_prompt": None,
                },
                "parent_advice": "Предложите ребёнку выбрать цвет пластилина.",
            }
            bot = FakeBot()
            cache = ImageCache(database, ImageCacheTests.NoAI())
            with self.assertRaisesRegex(RuntimeError, "simulated Telegram failure"):
                asyncio.run(deliver_plan(bot, 30, plan, "2026-11-14", database, cache))
            asyncio.run(deliver_plan(bot, 30, plan, "2026-11-14", database, cache))
            self.assertEqual(bot.documents, 1)
            self.assertEqual(bot.messages, 2)
            self.assertEqual(
                database.get_plan(30, "2026-11-14")["status"],
                "sent",
            )

    def test_vector_stimulus_is_a_valid_png(self) -> None:
        image_bytes = make_stimulus("shapes")
        self.assertTrue(image_bytes.startswith(b"\x89PNG\r\n\x1a\n"))


class AdminPlanResendTests(unittest.TestCase):
    def test_admin_resends_only_an_existing_sent_plan_without_changing_its_state(self) -> None:
        class FakeBot:
            def __init__(self) -> None:
                self.documents = 0
                self.messages = 0
                self.photos = 0
                self.session = SimpleNamespace(close=AsyncMock())

            async def send_document(self, *_args: object, **_kwargs: object) -> None:
                self.documents += 1

            async def send_message(self, *_args: object, **_kwargs: object) -> None:
                self.messages += 1

            async def send_photo(self, *_args: object, **_kwargs: object) -> None:
                self.photos += 1

        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "admin.sqlite3"
            key = Fernet.generate_key().decode("ascii")
            settings = Settings(
                telegram_token="test-token",
                admin_token="admin-token",
                data_encryption_key=key,
                database_path=database_path,
                default_delivery_time="09:00",
                timezone="UTC",
                daily_budget_usd=1,
                max_tokens=1000,
            )
            database = Database(database_path)
            database.initialize()
            database.upsert_profile(49, 40, "Ж", "09:00", None)
            database.activate_user(49)
            plan = {
                "focus_task": {
                    "title": "Считаем круги",
                    "worksheet": {
                        "title": "Круги",
                        "activity_type": "counting",
                        "shape": "circle",
                        "count": 3,
                        "instructions": "Посчитайте круги на листе.",
                        "questions": ["Сколько кругов?"],
                    },
                },
                "activity": {
                    "title": "Лепим",
                    "materials": ["пластилин"],
                    "parent_script": "Скатайте шар и назовите его форму.",
                    "age_facts": ["Факт первый.", "Факт второй."],
                    "story_prompt": "vector cat story",
                },
                "parent_advice": "Предложите ребёнку выбрать цвет пластилина.",
            }
            plan_date = "2026-11-14"
            database.save_plan(49, plan_date, plan, status="sent")
            database.mark_plan_part_sent(49, plan_date, "worksheet")
            database.mark_plan_part_sent(49, plan_date, "message")
            database.mark_plan_part_sent(49, plan_date, "story_image")
            database.mark_plan_sent(49, plan_date)
            image_tag_hash = ImageCache.hash_tag("vector cat story")
            database.save_image(
                image_tag_hash,
                "vector cat story",
                _picture_png(48, "black"),
            )
            database.save_telegram_file_id(image_tag_hash, "cached-image-file-id")
            original = database.get_plan(49, plan_date)
            bot = FakeBot()

            with patch.dict(
                os.environ,
                {
                    "DATABASE_PATH": str(database_path),
                    "TELEGRAM_TOKEN": "test-token",
                    "ADMIN_TOKEN": "admin-token",
                    "DATA_ENCRYPTION_KEY": key,
                },
            ):
                from app.admin import create_app

            application = create_app(
                settings=settings,
                database=database,
                vault=SecretVault(key),
                ai=object(),
            )
            client = TestClient(application)
            database.set_language(50, "ru", "09:00")
            hidden_identity_response = client.get(
                "/admin/users",
                headers={"Authorization": "Bearer admin-token"},
            )
            hidden_user = next(
                user for user in hidden_identity_response.json()
                if user["telegram_id"] == 50
            )
            self.assertIsNone(hidden_user["full_name"])
            database.set_identity_consent_status(49, "collecting")
            database.save_admin_identity(
                49,
                SecretVault(key).encrypt(
                    json.dumps(
                        {
                            "full_name": "Александр Иванов",
                            "child_name": "Миша",
                            "phone_number": "+380501234567",
                        },
                        ensure_ascii=False,
                    )
                ),
            )
            visible_users_response = client.get(
                "/admin/users",
                headers={"Authorization": "Bearer admin-token"},
            )
            visible_user = next(
                user for user in visible_users_response.json()
                if user["telegram_id"] == 49
            )
            self.assertEqual(visible_user["full_name"], "Александр Иванов")
            self.assertEqual(visible_user["child_name"], "Миша")
            self.assertEqual(visible_user["phone_number"], "+380501234567")
            detail_response = client.get(
                "/admin/users/49",
                headers={"Authorization": "Bearer admin-token"},
            )
            self.assertEqual(detail_response.json()["full_name"], "Александр Иванов")
            with (
                patch("app.admin.Bot", return_value=bot),
                patch("app.plans.make_worksheet", return_value=b"%PDF"),
            ):
                response = client.post(
                    f"/admin/users/49/plans/{plan_date}/resend",
                    headers={"Authorization": "Bearer admin-token"},
                )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json(), {"status": "resent", "plan_date": plan_date})
            self.assertEqual((bot.documents, bot.messages, bot.photos), (1, 1, 1))
            resent = database.get_plan(49, plan_date)
            self.assertEqual(resent["status"], "sent")
            self.assertEqual(resent["delivery_state"], original["delivery_state"])
            bot.session.close.assert_awaited_once()
            database.save_plan(49, "2026-11-15", plan, status="pending")
            pending_resend = client.post(
                "/admin/users/49/plans/2026-11-15/resend",
                headers={"Authorization": "Bearer admin-token"},
            )
            self.assertEqual(pending_resend.status_code, 409)

    def test_identity_name_validation_rejects_empty_or_overlong_names(self) -> None:
        self.assertEqual(validate_identity_name("  Мария Петрова "), "Мария Петрова")
        self.assertIsNone(validate_identity_name("  "))
        self.assertIsNone(validate_identity_name("A" * 121))


class PrintableTests(unittest.TestCase):
    def test_worksheet_is_a_pdf_with_a4_content(self) -> None:
        font_candidates = (
            os.getenv("FONT_PATH"),
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/Library/Fonts/Arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "C:/Windows/Fonts/arial.ttf",
        )
        if not any(path and Path(path).is_file() for path in font_candidates):
            self.skipTest("No supported Cyrillic font is installed.")
        data = make_worksheet(
            {
                "focus_task": {
                    "worksheet": {
                        "title": "Фигуры",
                        "activity_type": "geometry",
                        "shape": "mixed",
                        "count": 3,
                        "instructions": "Посчитайте фигуры.",
                        "questions": ["Какая фигура круглая?"],
                    }
                }
            }
        )
        self.assertTrue(data.startswith(b"%PDF-"))
        self.assertGreater(len(data), 1000)
        difference_pdf = make_worksheet(
            {
                "focus_task": {
                    "worksheet": {
                        "title": "Найди различия",
                        "activity_type": "find_differences",
                        "shape": "mixed",
                        "count": 4,
                        "instructions": "Сравните два ряда.",
                        "questions": ["Что изменилось?"],
                    }
                }
            }
        )
        self.assertTrue(difference_pdf.startswith(b"%PDF-"))


if __name__ == "__main__":
    unittest.main()
