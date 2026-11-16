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
from datetime import date, datetime, timedelta, timezone
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
from app.database import DEFAULT_PROMPTS, LEGACY_DAILY_PLAN_PROMPT, Database
from app.image_cache import ImageCache
from app.plans import deliver_plan, feedback_keyboard, plan_message
from app.printables import make_stimulus, make_worksheet
from app.prompt_templates import render_prompt_template, validate_prompt_template
from app.schemas import (
    Activity,
    AssessmentResponse,
    DailyPlan,
    FocusTask,
    ParentAdvice,
)
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


class PromptTemplateTests(unittest.TestCase):
    def test_prompt_template_renders_personal_context_and_rejects_unknown_fields(self) -> None:
        rendered = render_prompt_template(
            "Возраст: {{child.age_months}}; навыки: {{child.skills}}",
            {"child.age_months": 40, "child.skills": [{"skill_id": "sorting"}]},
        )
        self.assertIn("Возраст: 40", rendered)
        self.assertIn('"skill_id": "sorting"', rendered)
        with self.assertRaisesRegex(ValueError, "Unknown prompt placeholder"):
            render_prompt_template("Unknown: {{child.unknown}}", {})
        with self.assertRaisesRegex(ValueError, "Unknown prompt placeholder"):
            validate_prompt_template("Unknown: {{child.unknown}}", "daily_plan_task1")

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

    def test_ai_logs_are_encrypted_and_expired_rows_are_purged(self) -> None:
        vault = SecretVault(Fernet.generate_key().decode("ascii"))
        self.database.upsert_profile(79, 40, "Ж", "09:00", None)
        request = json.dumps({"prompt": "private accessibility detail"})
        response = json.dumps({"answer": "generated text"})
        log_id = self.database.create_ai_request_log(
            "diagnostics", 79, "gemini/gemini-2.5-pro", vault.encrypt(request)
        )
        self.database.finish_ai_request_log(log_id, "success", vault.encrypt(response))
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT request_encrypted, response_encrypted FROM ai_request_logs "
                "WHERE id = ?",
                (log_id,),
            ).fetchone()
            connection.execute(
                "UPDATE ai_request_logs SET created_at = ? WHERE id = ?",
                ((datetime.now(timezone.utc) - timedelta(days=31)).isoformat(), log_id),
            )
        self.assertNotIn("private accessibility detail", row["request_encrypted"])
        self.assertEqual(vault.decrypt(row["request_encrypted"]), request)
        self.assertEqual(vault.decrypt(row["response_encrypted"]), response)

        variant_id = self.database.create_prompt_test_variant(
            79,
            "advice",
            {"text": "Поддержите самостоятельный выбор ребёнка."},
            None,
        )
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE prompt_test_variants SET created_at = ? WHERE id = ?",
                ((datetime.now(timezone.utc) - timedelta(days=31)).isoformat(), variant_id),
            )
        self.assertEqual(self.database.purge_ai_request_logs(), 1)
        self.assertEqual(self.database.list_ai_request_logs(), [])
        self.assertIsNone(self.database.prompt_test_variant(variant_id))

    def test_legacy_daily_plan_prompt_is_migrated_without_losing_history(self) -> None:
        self.database.update_prompt("daily_plan", LEGACY_DAILY_PLAN_PROMPT)
        self.database.initialize()
        self.assertEqual(
            self.database.get_prompt("daily_plan"),
            DEFAULT_PROMPTS["daily_plan"],
        )
        self.assertEqual(
            self.database.prompt_history("daily_plan")[0]["prompt"],
            LEGACY_DAILY_PLAN_PROMPT,
        )

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
            asyncio.run(
                ai.generate_image(
                    "a red apple", image_cache_tag_hash="image-cache-key"
                )
            )
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
        logs = self.database.list_ai_request_logs(limit=2)
        first_response = json.loads(vault.decrypt(logs[-1]["response_encrypted"]))
        self.assertEqual(first_response["image_cache_tag_hash"], "image-cache-key")
        self.assertEqual(first_response["image_bytes"], len(b"image-bytes"))

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
        self.database.cache_content(
            "focus_task",
            f"personal:{telegram_id}:plan:ru:40m",
            "personal-item",
            "personal-hash",
            {"title": "Личный вариант"},
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
            personal_content_count = connection.execute(
                "SELECT COUNT(*) FROM content_library WHERE content_hash = ?",
                ("personal-hash",),
            ).fetchone()[0]
        self.assertEqual(shared_content_count, 1)
        self.assertEqual(personal_content_count, 0)
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
        async def generate_image(
            self,
            _prompt: str,
            *,
            telegram_id: int | None = None,
            image_cache_tag_hash: str | None = None,
        ) -> bytes:
            del _prompt, telegram_id, image_cache_tag_hash
            raise AssertionError("Static stimulus generation must not call an AI model.")

    def test_images_are_reused_between_users_but_not_repeated_for_one_user(self) -> None:
        class FakeAI:
            calls = 0

            async def generate_image(
                self,
                _prompt: str,
                *,
                telegram_id: int | None = None,
                image_cache_tag_hash: str | None = None,
            ) -> bytes:
                del _prompt, telegram_id, image_cache_tag_hash
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

            async def generate_image(
                self,
                prompt: str,
                *,
                telegram_id: int | None = None,
                image_cache_tag_hash: str | None = None,
            ) -> bytes:
                del prompt, telegram_id, image_cache_tag_hash
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


class AIRequestLogTests(unittest.TestCase):
    def test_component_generation_uses_its_prompt_and_logs_full_request_and_response(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Database(Path(temporary_directory) / "ai.sqlite3")
            database.initialize()
            database.upsert_profile(300, 40, "Ж", "09:00", None)
            vault = SecretVault(Fernet.generate_key().decode("ascii"))
            ai = AIOrchestrator(
                database,
                vault,
                daily_budget_usd=1,
                max_tokens=1000,
            )
            raw_response = '{"text":"Предложите ребёнку выбрать порядок игры."}'
            response = SimpleNamespace(
                choices=[
                    SimpleNamespace(message=SimpleNamespace(content=raw_response))
                ],
                usage=SimpleNamespace(prompt_tokens=21, completion_tokens=8),
                _hidden_params={"response_cost": 0.002},
                id="response-1",
            )
            with patch(
                "app.ai.litellm.acompletion",
                new_callable=AsyncMock,
                return_value=response,
            ) as completion:
                result = asyncio.run(
                    ai.generate_plan_component(
                        "advice",
                        {"age_months": 40, "language": "uk"},
                        [],
                        "normal",
                        telegram_id=300,
                    )
                )

            self.assertEqual(result.text, "Предложите ребёнку выбрать порядок игры.")
            request = completion.await_args.kwargs
            self.assertEqual(request["model"], "gemini/gemini-2.5-flash")
            self.assertEqual(
                request["messages"][2]["content"],
                database.get_prompt("daily_plan_advice"),
            )
            self.assertIn("Ukrainian", request["messages"][3]["content"])
            self.assertEqual(request["response_format"]["type"], "json_schema")
            stored = database.list_ai_request_logs(telegram_id=300)
            self.assertEqual(len(stored), 1)
            self.assertEqual(stored[0]["task"], "daily_plan_advice")
            logged_request = json.loads(
                vault.decrypt(stored[0]["request_encrypted"])
            )
            logged_response = json.loads(
                vault.decrypt(stored[0]["response_encrypted"])
            )
            self.assertEqual(logged_request["messages"], request["messages"])
            self.assertEqual(logged_response["content"], raw_response)
            self.assertEqual(logged_response["cost_usd"], 0.002)

    def test_prompt_override_renders_child_context_without_replacing_active_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Database(Path(temporary_directory) / "prompt-override.sqlite3")
            database.initialize()
            vault = SecretVault(Fernet.generate_key().decode("ascii"))
            ai = AIOrchestrator(
                database,
                vault,
                daily_budget_usd=1,
                max_tokens=1000,
            )
            response = SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content='{"text":"Предложите игру по интересам ребёнка."}'
                        )
                    )
                ],
                usage=None,
                _hidden_params={},
                id="response-without-metrics",
            )
            traces: list[dict[str, object]] = []
            with patch(
                "app.ai.litellm.acompletion",
                new_callable=AsyncMock,
                return_value=response,
            ) as completion:
                asyncio.run(
                    ai.generate_plan_component(
                        "advice",
                        {
                            "age_months": 40,
                            "gender": "Ж",
                            "language": "ru",
                            "interests": "конструкторы",
                            "skills": [{"skill_id": "sorting", "result": "prompted"}],
                        },
                        [{"skill_id": "sorting", "result": "prompted"}],
                        "hard",
                        prompt_override=(
                            "Учитывай возраст {{child.age_months}}, пол {{child.gender}}, "
                            "интересы {{child.interests}} и навыки {{child.skills}}."
                        ),
                        request_traces=traces,
                    )
                )

            active_prompt = database.get_prompt("daily_plan_advice")
            messages = completion.await_args.kwargs["messages"]
            self.assertIn("возраст 40", messages[2]["content"])
            self.assertIn("пол Ж", messages[2]["content"])
            self.assertIn("конструкторы", messages[2]["content"])
            self.assertIn('"skill_id": "sorting"', messages[2]["content"])
            self.assertEqual(database.get_prompt("daily_plan_advice"), active_prompt)
            self.assertEqual(len(traces), 1)
            stored = database.ai_request_logs_by_ids([int(traces[0]["id"])])[0]
            logged_response = json.loads(
                vault.decrypt(stored["response_encrypted"])
            )
            self.assertIsNone(logged_response["usage"]["prompt_tokens"])
            self.assertIsNone(logged_response["cost_usd"])
            self.assertIsInstance(logged_response["duration_ms"], int)


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
            self.component_calls = {
                "focus_task": 0,
                "activity": 0,
                "advice": 0,
            }

        async def generate_assessment(
            self,
            age_months: int,
            accessibility_notes: str = "",
            *,
            language: str = "ru",
            excluded_skill_ids: list[str] | None = None,
            excluded_questions: list[str] | None = None,
            telegram_id: int | None = None,
            child_context: dict[str, object] | None = None,
        ) -> AssessmentResponse:
            del age_months, accessibility_notes, language, excluded_skill_ids, excluded_questions, telegram_id, child_context
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

        async def generate_plan_component(
            self,
            component: str,
            profile: dict[str, object],
            gaps: list[dict[str, object]],
            feedback: str,
            *,
            avoid_recent: dict[str, list[str]] | None = None,
            telegram_id: int | None = None,
        ) -> FocusTask | Activity | ParentAdvice:
            del profile, gaps, feedback, avoid_recent, telegram_id
            self.component_calls[component] += 1
            if component == "focus_task":
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
            if component == "focus_task":
                return FocusTask.model_validate(
                    {
                        "title": variant["task"],
                        "worksheet": {
                            "title": variant["task"],
                            "activity_type": variant["type"],
                            "shape": variant["shape"],
                            "count": 3,
                            "instructions": variant["instructions"],
                            "questions": [variant["question"]],
                        },
                        "image_prompt": f"A simple worksheet illustration, variation {n}.",
                    }
                )
            if component == "activity":
                return Activity.model_validate(
                    {
                        "title": variant["activity"],
                        "materials": variant["materials"],
                        "parent_script": variant["script"],
                        "age_facts": [
                            f"Наблюдайте за изменениями во время занятия {n}.",
                            f"Сравните, что изменилось после игры {n}.",
                        ],
                        "story_prompt": None,
                    }
                )
            return ParentAdvice(text=variant["advice"])

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
        self.assertEqual(self.ai.component_calls, {
            "focus_task": 1,
            "activity": 1,
            "advice": 1,
        })
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
        self.assertEqual(self.ai.component_calls, {
            "focus_task": 2,
            "activity": 2,
            "advice": 2,
        })
        self.assertEqual(first["focus_task"], other_user["focus_task"])
        self.assertNotEqual(first["focus_task"], second["focus_task"])
        self.assertNotEqual(first["parent_advice"], second["parent_advice"])
        self.assertEqual(
            self.library.repeated_plan_components(
                20, first, excluding_plan_date="2026-11-14"
            ),
            [],
        )

    def test_accepted_personal_component_is_used_without_generating_it_again(self) -> None:
        profile = self.database.profile(20)
        focus_task = FocusTask.model_validate(
            {
                "title": "Личный вариант фокусного задания",
                "worksheet": {
                    "title": "Круги",
                    "activity_type": "counting",
                    "shape": "circle",
                    "count": 3,
                    "instructions": "Посчитайте круги на листе.",
                    "questions": ["Сколько кругов?"],
                },
                "image_prompt": "Three circles on a white background.",
            }
        ).model_dump()
        self.library.cache_personal_variant(
            20, profile, "focus_task", focus_task
        )

        plan = asyncio.run(
            self.library.daily_plan(20, profile, "normal", "2026-11-14")
        )

        self.assertEqual(plan["focus_task"], focus_task)
        self.assertEqual(self.ai.component_calls["focus_task"], 0)
        self.assertEqual(self.ai.component_calls["activity"], 1)
        self.assertEqual(self.ai.component_calls["advice"], 1)

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
                telegram_id: int | None = None,
                child_context: dict[str, object] | None = None,
            ) -> AssessmentResponse:
                del age_months, accessibility_notes, language, excluded_skill_ids, excluded_questions, telegram_id, child_context
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

    def test_focus_image_is_sent_cached_and_reused_on_resend(self) -> None:
        class FakeAI:
            calls = 0

            async def generate_image(
                self,
                prompt: str,
                *,
                telegram_id: int | None = None,
                image_cache_tag_hash: str | None = None,
            ) -> bytes:
                del prompt, telegram_id, image_cache_tag_hash
                self.calls += 1
                return _picture_png(48, "red")

        class FakeBot:
            def __init__(self) -> None:
                self.photos: list[object] = []

            async def send_document(self, *_args: object, **_kwargs: object) -> None:
                return None

            async def send_photo(self, *_args: object, **_kwargs: object) -> object:
                self.photos.append(_args[1])
                return SimpleNamespace(photo=[SimpleNamespace(file_id="file-1")])

            async def send_message(self, *_args: object, **_kwargs: object) -> None:
                return None

        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Database(Path(temporary_directory) / "focus-image.sqlite3")
            database.initialize()
            database.upsert_profile(31, 36, "М", "09:00", None)
            fake_ai = FakeAI()
            cache = ImageCache(database, fake_ai)
            bot = FakeBot()
            prompt = "Three red circles on a clean white background."
            plan = {
                "focus_task": {
                    "title": "Считаем круги",
                    "image_prompt": prompt,
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
            with patch("app.plans.make_worksheet", return_value=b"%PDF"):
                asyncio.run(
                    deliver_plan(bot, 31, plan, "2026-11-14", database, cache)
                )
                asyncio.run(
                    deliver_plan(
                        bot,
                        31,
                        plan,
                        "2026-11-14",
                        database,
                        cache,
                        force_resend=True,
                    )
                )

            self.assertEqual(fake_ai.calls, 1)
            self.assertEqual(len(bot.photos), 2)
            self.assertEqual(bot.photos[1], "file-1")
            self.assertIn(
                "focus_image",
                database.get_plan(31, "2026-11-14")["delivery_state"],
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


class AdminPromptTestTests(unittest.TestCase):
    def test_prompt_variant_can_be_previewed_saved_for_one_user_or_rejected(self) -> None:
        class FakeAI:
            def __init__(self) -> None:
                self.last_prompt: str | None = None
                self.last_context: dict[str, object] | None = None

            async def generate_plan_component(
                self,
                component: str,
                profile: dict[str, object],
                gaps: list[dict[str, object]],
                feedback: str,
                *,
                avoid_recent: dict[str, list[str]] | None = None,
                telegram_id: int | None = None,
                prompt_override: str | None = None,
                request_traces: list[dict[str, object]] | None = None,
            ) -> FocusTask | Activity | ParentAdvice:
                del gaps, feedback, avoid_recent, telegram_id, request_traces
                self.last_prompt = prompt_override
                self.last_context = profile
                if component == "focus_task":
                    return FocusTask.model_validate(
                        {
                            "title": "Считаем круги",
                            "worksheet": {
                                "title": "Круги",
                                "activity_type": "counting",
                                "shape": "circle",
                                "count": 3,
                                "instructions": "Посчитайте круги на листе.",
                                "questions": ["Сколько кругов?"],
                            },
                            "image_prompt": "A simple picture with three red circles.",
                        }
                    )
                if component == "activity":
                    return Activity(
                        title="Лепим башню",
                        materials=["пластилин"],
                        parent_script="Скатайте три шарика и сравните их размер.",
                        age_facts=["Факт первый.", "Факт второй."],
                        story_prompt=None,
                    )
                return ParentAdvice(
                    text="Предложите ребёнку выбрать порядок занятий."
                )

            async def generate_assessment(
                self,
                age_months: int,
                accessibility_notes: str = "",
                *,
                language: str = "ru",
                excluded_skill_ids: list[str] | None = None,
                excluded_questions: list[str] | None = None,
                telegram_id: int | None = None,
                child_context: dict[str, object] | None = None,
                prompt_override: str | None = None,
                request_traces: list[dict[str, object]] | None = None,
            ) -> AssessmentResponse:
                del accessibility_notes, language, excluded_questions, telegram_id, request_traces
                self.last_prompt = prompt_override
                self.last_context = {"age_months": age_months, **(child_context or {})}
                question = {
                    "skill_id": "test_skill",
                    "domains": ["motor", "language"],
                    "parent_script": "Попросите ребёнка пройти и назвать предмет.",
                    "stimulus_kind": None,
                    "stimulus_prompt": None,
                }
                return AssessmentResponse.model_validate({"questions": [question] * 4})

            async def generate_image(
                self,
                prompt: str,
                *,
                telegram_id: int | None = None,
                image_cache_tag_hash: str | None = None,
                request_traces: list[dict[str, object]] | None = None,
            ) -> bytes:
                del prompt, telegram_id, image_cache_tag_hash, request_traces
                return _picture_png(48, "red")

        with tempfile.TemporaryDirectory() as temporary_directory:
            key = Fernet.generate_key().decode("ascii")
            database = Database(Path(temporary_directory) / "admin-ai.sqlite3")
            database.initialize()
            database.upsert_profile(49, 40, "Ж", "09:00", None)
            database.upsert_profile(50, 40, "М", "09:00", None)
            database.record_skill(49, "sorting", "cognitive", "prompted")
            settings = Settings(
                telegram_token="test-token",
                admin_token="admin-token",
                data_encryption_key=key,
                database_path=database.path,
                default_delivery_time="09:00",
                timezone="UTC",
                daily_budget_usd=1,
                max_tokens=1000,
            )
            from app.admin import create_app

            fake_ai = FakeAI()
            client = TestClient(
                create_app(
                    settings=settings,
                    database=database,
                    vault=SecretVault(key),
                    ai=fake_ai,
                )
            )
            headers = {"Authorization": "Bearer admin-token"}
            preview = client.post(
                "/admin/prompt-tests/focus_task",
                headers=headers,
                json={
                    "telegram_id": 49,
                    "prompt": "Draft task prompt with enough characters.",
                },
            )
            self.assertEqual(preview.status_code, 200, preview.text)
            self.assertEqual(fake_ai.last_prompt, "Draft task prompt with enough characters.")
            self.assertEqual(database.get_prompt("daily_plan_task1"), DEFAULT_PROMPTS["daily_plan_task1"])
            variant = preview.json()
            image = client.get(variant["image_url"], headers=headers)
            self.assertEqual(image.status_code, 200)
            self.assertTrue(image.content.startswith(b"\x89PNG"))
            accepted = client.post(
                f"/admin/prompt-tests/variants/{variant['id']}/resolve",
                headers=headers,
                json={"status": "accepted"},
            )
            self.assertEqual(accepted.status_code, 200, accepted.text)
            personal_cohort = "personal:49:plan:ru:40m"
            self.assertEqual(
                len(database.available_content(49, "focus_task", personal_cohort)),
                1,
            )
            self.assertEqual(
                database.available_content(
                    50, "focus_task", "personal:50:plan:ru:40m"
                ),
                [],
            )

            rejected_preview = client.post(
                "/admin/prompt-tests/activity",
                headers=headers,
                json={"telegram_id": 49},
            )
            self.assertEqual(rejected_preview.status_code, 200)
            rejected_variant = rejected_preview.json()
            rejected = client.post(
                f"/admin/prompt-tests/variants/{rejected_variant['id']}/resolve",
                headers=headers,
                json={"status": "rejected"},
            )
            self.assertEqual(rejected.status_code, 200, rejected.text)
            self.assertEqual(
                database.available_content(49, "activity", personal_cohort),
                [],
            )

            context_response = client.get(
                "/admin/prompt-tests/context/49", headers=headers
            )
            self.assertEqual(context_response.status_code, 200)
            context = context_response.json()
            self.assertEqual(context["gender"], "Ж")
            self.assertEqual(context["age_months"], 40)
            self.assertEqual(context["diagnostic_results"][0]["skill_id"], "sorting")
            self.assertNotIn("diagnosis_notes", context)

            diagnostics = client.post(
                "/admin/prompt-tests/diagnostics",
                headers=headers,
                json={
                    "telegram_id": 49,
                    "prompt": "Draft diagnostic prompt with enough characters.",
                },
            )
            self.assertEqual(diagnostics.status_code, 200, diagnostics.text)
            self.assertEqual(diagnostics.json()["status"], "preview")
            self.assertEqual(fake_ai.last_context["gender"], "Ж")
            self.assertEqual(fake_ai.last_context["skills"][0]["skill_id"], "sorting")
            self.assertEqual(
                database.get_prompt("diagnostics"), DEFAULT_PROMPTS["diagnostics"]
            )

            invalid_prompt = client.put(
                "/admin/settings/prompts/daily_plan_task1",
                headers=headers,
                json={"prompt": "Prompt with {{child.unknown}} field."},
            )
            self.assertEqual(invalid_prompt.status_code, 422)
            self.assertEqual(
                database.get_prompt("daily_plan_task1"),
                DEFAULT_PROMPTS["daily_plan_task1"],
            )
            replacement = (
                "Создавай задание для ребёнка возраста {{child.age_months}} месяцев."
            )
            saved_prompt = client.put(
                "/admin/settings/prompts/daily_plan_task1",
                headers=headers,
                json={"prompt": replacement},
            )
            self.assertEqual(saved_prompt.status_code, 200, saved_prompt.text)
            self.assertEqual(saved_prompt.json()["version"], 2)
            self.assertIn("child.age_months", saved_prompt.json()["placeholders"])
            self.assertEqual(
                database.get_prompt("daily_plan_task1"), replacement
            )
            self.assertEqual(database.prompt_history("daily_plan_task1")[0]["version"], 1)

    def test_ai_log_endpoint_requires_admin_and_decrypts_full_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            key = Fernet.generate_key().decode("ascii")
            vault = SecretVault(key)
            database = Database(Path(temporary_directory) / "admin-logs.sqlite3")
            database.initialize()
            request = json.dumps(
                {"messages": [{"role": "user", "content": "full prompt"}]}
            )
            response = json.dumps({"content": '{"result":"complete answer"}'})
            log_id = database.create_ai_request_log(
                "diagnostics", None, "gemini/gemini-2.5-pro", vault.encrypt(request)
            )
            database.finish_ai_request_log(
                log_id, "success", vault.encrypt(response)
            )
            settings = Settings(
                telegram_token="test-token",
                admin_token="admin-token",
                data_encryption_key=key,
                database_path=database.path,
                default_delivery_time="09:00",
                timezone="UTC",
                daily_budget_usd=1,
                max_tokens=1000,
            )
            from app.admin import create_app

            client = TestClient(
                create_app(
                    settings=settings,
                    database=database,
                    vault=vault,
                    ai=object(),
                )
            )
            self.assertEqual(client.get("/admin/ai-logs").status_code, 401)
            result = client.get(
                "/admin/ai-logs",
                headers={"Authorization": "Bearer admin-token"},
            )
            self.assertEqual(result.status_code, 200, result.text)
            log = result.json()[0]
            self.assertEqual(log["id"], log_id)
            self.assertEqual(log["request"], json.loads(request))
            self.assertEqual(log["response"], json.loads(response))


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
