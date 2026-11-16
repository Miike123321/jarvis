from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Generator


DEFAULT_ROUTES = {
    "diagnostics": {"provider": "gemini", "model": "gemini-2.5-pro", "max_tokens": 2500},
    "daily_plan": {"provider": "gemini", "model": "gemini-2.5-flash", "max_tokens": 2500},
    "image": {"provider": "vertex_ai", "model": "imagen-3.0-generate-002", "max_tokens": 1000},
}

DEFAULT_PROMPTS = {
    "safety": (
        "You create playful, inclusive home-learning activities for children. "
        "Use the zone of proximal development as a flexible educational principle, "
        "not a fixed developmental score. You are not a clinician: never diagnose, "
        "predict, or label a child. Do not claim to administer or score DDST-II. "
        "Use high-level public WHO child-development guidance cautiously, as approximate "
        "educational context rather than a pass/fail standard. Do not reproduce standardized "
        "test items. Advise families to consult a qualified pediatric professional about "
        "developmental concerns. Adapt tasks "
        "to caregiver-provided accessibility needs without treating them as diagnoses."
    ),
    "diagnostics": (
        "Create 4-8 short play-based observations across motor, language, cognitive, "
        "social, and daily-living domains. Include foundational skills below the child's "
        "age as well as age-level opportunities, without presenting these as a validated "
        "screening or diagnostic instrument. Each task must be safe, optional, and "
        "scorable as independent, prompted, or not yet. Avoid medical conclusions."
    ),
    "daily_plan": (
        "Create one safe, inclusive, age-appropriate component for a home-learning "
        "plan. Use the child's observed learning opportunities and recent difficulty "
        "feedback. Keep activities optional, accessible, and doable with ordinary "
        "household items. Never diagnose or present advice as treatment."
    ),
    "daily_plan_task1": (
        "Create one concrete cognitive worksheet task using the required FocusTask "
        "schema. Make the worksheet solvable and internally consistent with its count, "
        "shape, instructions, and questions. Include a concise image_prompt for a "
        "supporting illustration; request a simple white-background image with no text."
    ),
    "daily_plan_task2": (
        "Create one practical sensory or motor activity using the required Activity "
        "schema. Give the caregiver exact words to model, useful language or movement "
        "vocabulary, ordinary materials, and two to five accurate concrete age-appropriate "
        "facts. Set story_prompt to null; the illustration belongs to task 1."
    ),
    "daily_plan_advice": (
        "Create one concise, specific parent suggestion for today. Base it on the "
        "selected child's observed learning opportunities and make it practical, "
        "respectful, and non-clinical. Never diagnose or present advice as treatment."
    ),
    "image": (
        "Clean Montessori-inspired vector illustration for a children's learning activity, "
        "pure white background, crisp outlines, few clearly separated objects, no text, "
        "no decorative clutter."
    ),
}

LEGACY_DAILY_PLAN_PROMPT = (
    "Create exactly three age-appropriate blocks: a printable cognitive task, a "
    "sensory/motor activity with useful parent language and accurate, concrete "
    "age-appropriate facts, and a concise parent tip. Keep tasks safe, accessible, "
    "playful, and doable with ordinary household items. The printable may be "
    "counting, geometry, pattern completion, or a deterministic find-the-difference "
    "worksheet. Use the child's observed learning needs and recent difficulty "
    "feedback. Never present advice as treatment."
)


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Generator[sqlite3.Connection, None, None]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            created_at TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            subscription_status TEXT NOT NULL DEFAULT 'free',
            delivery_time TEXT NOT NULL,
            language TEXT NOT NULL DEFAULT 'ru' CHECK(language IN ('ru', 'uk')),
            schedule_start_date TEXT NOT NULL DEFAULT '0001-01-01',
            identity_consent TEXT NOT NULL DEFAULT 'pending'
                CHECK(identity_consent IN ('pending', 'collecting', 'granted', 'denied')),
            identity_data_encrypted TEXT
        );
        CREATE TABLE IF NOT EXISTS child_profiles (
            telegram_id INTEGER PRIMARY KEY REFERENCES users(telegram_id) ON DELETE CASCADE,
            age_months INTEGER NOT NULL CHECK(age_months BETWEEN 7 AND 84),
            gender TEXT NOT NULL CHECK(gender IN ('М', 'Ж')),
            diagnosis_notes_encrypted TEXT,
            interests TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            birth_date_encrypted TEXT
        );
        CREATE TABLE IF NOT EXISTS skill_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
            skill_id TEXT NOT NULL,
            domain TEXT NOT NULL,
            result TEXT NOT NULL CHECK(result IN ('independent', 'prompted', 'not_yet')),
            observed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
            plan_date TEXT NOT NULL,
            content_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'sent',
            feedback TEXT,
            created_at TEXT NOT NULL,
            sent_at TEXT,
            delivery_state_json TEXT NOT NULL DEFAULT '[]',
            UNIQUE(telegram_id, plan_date)
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS prompts (
            name TEXT PRIMARY KEY,
            prompt TEXT NOT NULL,
            version INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS prompt_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            prompt TEXT NOT NULL,
            version INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS image_cache (
            tag_hash TEXT PRIMARY KEY,
            tag TEXT NOT NULL,
            image_bytes BLOB NOT NULL,
            telegram_file_id TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS content_library (
            content_hash TEXT PRIMARY KEY,
            category TEXT NOT NULL,
            cohort TEXT NOT NULL,
            dedupe_key TEXT NOT NULL,
            content_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(category, cohort, dedupe_key)
        );
        CREATE INDEX IF NOT EXISTS idx_content_library_lookup
            ON content_library(category, cohort, created_at);
        CREATE TABLE IF NOT EXISTS user_content_usage (
            telegram_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
            category TEXT NOT NULL,
            dedupe_key TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            content_json TEXT NOT NULL,
            plan_date TEXT,
            used_at TEXT NOT NULL,
            PRIMARY KEY(telegram_id, category, dedupe_key)
        );
        CREATE INDEX IF NOT EXISTS idx_user_content_usage_history
            ON user_content_usage(telegram_id, category, used_at);
        CREATE TABLE IF NOT EXISTS content_generation_cooldowns (
            telegram_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
            scope TEXT NOT NULL,
            retry_after TEXT NOT NULL,
            PRIMARY KEY(telegram_id, scope)
        );
        CREATE TABLE IF NOT EXISTS ai_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task TEXT NOT NULL,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            cost_usd REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ai_request_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task TEXT NOT NULL,
            telegram_id INTEGER REFERENCES users(telegram_id) ON DELETE CASCADE,
            model TEXT NOT NULL,
            status TEXT NOT NULL,
            request_encrypted TEXT NOT NULL,
            response_encrypted TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_ai_request_logs_created
            ON ai_request_logs(created_at);
        CREATE INDEX IF NOT EXISTS idx_ai_request_logs_user
            ON ai_request_logs(telegram_id, created_at);
        CREATE TABLE IF NOT EXISTS prompt_test_variants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
            component TEXT NOT NULL CHECK(component IN ('focus_task', 'activity', 'advice')),
            content_json TEXT NOT NULL,
            image_tag_hash TEXT,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending', 'accepted', 'rejected')),
            created_at TEXT NOT NULL
        );
        """
        with self.connect() as connection:
            connection.executescript(schema)
            plan_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(plans)").fetchall()
            }
            if "delivery_state_json" not in plan_columns:
                connection.execute(
                    "ALTER TABLE plans ADD COLUMN delivery_state_json TEXT NOT NULL DEFAULT '[]'"
                )
            profile_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(child_profiles)"
                ).fetchall()
            }
            if "birth_date_encrypted" not in profile_columns:
                connection.execute(
                    "ALTER TABLE child_profiles ADD COLUMN birth_date_encrypted TEXT"
                )
            user_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(users)").fetchall()
            }
            if "language" not in user_columns:
                connection.execute(
                    "ALTER TABLE users ADD COLUMN language TEXT NOT NULL DEFAULT 'ru' "
                    "CHECK(language IN ('ru', 'uk'))"
                )
            if "schedule_start_date" not in user_columns:
                connection.execute(
                    "ALTER TABLE users ADD COLUMN schedule_start_date TEXT NOT NULL "
                    "DEFAULT '0001-01-01'"
                )
            if "identity_consent" not in user_columns:
                connection.execute(
                    "ALTER TABLE users ADD COLUMN identity_consent TEXT NOT NULL "
                    "DEFAULT 'pending' CHECK(identity_consent IN "
                    "('pending', 'collecting', 'granted', 'denied'))"
                )
            if "identity_data_encrypted" not in user_columns:
                connection.execute(
                    "ALTER TABLE users ADD COLUMN identity_data_encrypted TEXT"
                )
            for key, route in DEFAULT_ROUTES.items():
                connection.execute(
                    "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                    (f"route:{key}", json.dumps(route)),
                )
            for name, prompt in DEFAULT_PROMPTS.items():
                connection.execute(
                    "INSERT OR IGNORE INTO prompts(name, prompt, version, updated_at) "
                    "VALUES (?, ?, 1, ?)",
                    (name, prompt, datetime.now().astimezone().isoformat()),
                )
        if self.get_prompt("daily_plan") == LEGACY_DAILY_PLAN_PROMPT:
            self.update_prompt("daily_plan", DEFAULT_PROMPTS["daily_plan"])
        self.purge_ai_request_logs()

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)),
            )

    def get_route(self, task: str) -> dict[str, Any]:
        route = self.get_setting(f"route:{task}")
        if route is None:
            raise ValueError(f"Unknown AI task route: {task}")
        return route

    def set_route(self, task: str, route: dict[str, Any]) -> None:
        if task not in DEFAULT_ROUTES:
            raise ValueError(f"Unknown AI task route: {task}")
        self.set_setting(f"route:{task}", route)

    def get_prompt(self, name: str) -> str:
        with self.connect() as connection:
            row = connection.execute("SELECT prompt FROM prompts WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise ValueError(f"Unknown system prompt: {name}")
        return str(row["prompt"])

    def list_prompts(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT name, prompt, version, updated_at FROM prompts ORDER BY name"
            ).fetchall()
        return [dict(row) for row in rows]

    def update_prompt(self, name: str, prompt: str) -> int:
        now = datetime.now().astimezone().isoformat()
        with self.connect() as connection:
            current = connection.execute(
                "SELECT version FROM prompts WHERE name = ?", (name,)
            ).fetchone()
            if current is None:
                raise ValueError(f"Unknown system prompt: {name}")
            version = int(current["version"]) + 1
            connection.execute(
                "INSERT INTO prompt_history(name, prompt, version, updated_at) "
                "SELECT name, prompt, version, updated_at FROM prompts WHERE name = ?",
                (name,),
            )
            connection.execute(
                "UPDATE prompts SET prompt = ?, version = ?, updated_at = ? WHERE name = ?",
                (prompt, version, now, name),
            )
        return version

    def prompt_history(self, name: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT version, prompt, updated_at FROM prompt_history "
                "WHERE name = ? ORDER BY version DESC",
                (name,),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_ai_request_log(
        self,
        task: str,
        telegram_id: int | None,
        model: str,
        request_encrypted: str,
    ) -> int:
        self.purge_ai_request_logs()
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO ai_request_logs"
                "(task, telegram_id, model, status, request_encrypted, created_at) "
                "VALUES (?, ?, ?, 'pending', ?, ?)",
                (
                    task,
                    telegram_id,
                    model,
                    request_encrypted,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            return int(cursor.lastrowid)

    def finish_ai_request_log(
        self, log_id: int, status: str, response_encrypted: str
    ) -> None:
        if status not in {"success", "error", "invalid"}:
            raise ValueError(f"Unsupported AI request log status: {status}")
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE ai_request_logs SET status = ?, response_encrypted = ? WHERE id = ?",
                (status, response_encrypted, log_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"AI request log {log_id} does not exist.")

    def list_ai_request_logs(
        self, *, limit: int = 100, telegram_id: int | None = None
    ) -> list[dict[str, Any]]:
        self.purge_ai_request_logs()
        with self.connect() as connection:
            if telegram_id is None:
                rows = connection.execute(
                    "SELECT id, task, telegram_id, model, status, request_encrypted, "
                    "response_encrypted, created_at FROM ai_request_logs "
                    "ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT id, task, telegram_id, model, status, request_encrypted, "
                    "response_encrypted, created_at FROM ai_request_logs "
                    "WHERE telegram_id = ? ORDER BY id DESC LIMIT ?",
                    (telegram_id, limit),
                ).fetchall()
        return [dict(row) for row in rows]

    def ai_request_logs_by_ids(self, log_ids: list[int]) -> list[dict[str, Any]]:
        if not log_ids:
            return []
        placeholders = ",".join("?" for _ in log_ids)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, task, telegram_id, model, status, request_encrypted, "
                "response_encrypted, created_at FROM ai_request_logs "
                f"WHERE id IN ({placeholders}) ORDER BY id",
                log_ids,
            ).fetchall()
        return [dict(row) for row in rows]

    def purge_ai_request_logs(self) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        with self.connect() as connection:
            logs = connection.execute(
                "DELETE FROM ai_request_logs WHERE created_at < ?", (cutoff,)
            )
            connection.execute(
                "DELETE FROM prompt_test_variants WHERE created_at < ?", (cutoff,)
            )
        return int(logs.rowcount)

    def create_prompt_test_variant(
        self,
        telegram_id: int,
        component: str,
        content: dict[str, Any],
        image_tag_hash: str | None,
    ) -> int:
        if component not in {"focus_task", "activity", "advice"}:
            raise ValueError(f"Unsupported prompt test component: {component}")
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO prompt_test_variants"
                "(telegram_id, component, content_json, image_tag_hash, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    telegram_id,
                    component,
                    json.dumps(content, ensure_ascii=False),
                    image_tag_hash,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            return int(cursor.lastrowid)

    def prompt_test_variant(self, variant_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM prompt_test_variants WHERE id = ?", (variant_id,)
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["content"] = json.loads(result.pop("content_json"))
        return result

    def resolve_prompt_test_variant(self, variant_id: int, status: str) -> bool:
        if status not in {"accepted", "rejected"}:
            raise ValueError(f"Unsupported prompt test variant status: {status}")
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE prompt_test_variants SET status = ? "
                "WHERE id = ? AND status = 'pending'",
                (status, variant_id),
            )
        return cursor.rowcount == 1

    def store_secret(self, provider: str, encrypted_key: str) -> None:
        self.set_setting(f"secret:{provider}", encrypted_key)

    def get_secret(self, provider: str) -> str | None:
        return self.get_setting(f"secret:{provider}")

    def upsert_profile(
        self,
        telegram_id: int,
        age_months: int,
        gender: str,
        delivery_time: str,
        encrypted_diagnosis_notes: str | None,
        *,
        encrypted_birth_date: str | None = None,
    ) -> None:
        now = datetime.now().astimezone().isoformat()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO users(telegram_id, created_at, delivery_time, active) "
                "VALUES (?, ?, ?, 0) ON CONFLICT(telegram_id) DO UPDATE SET "
                "delivery_time = excluded.delivery_time, active = 0",
                (telegram_id, now, delivery_time),
            )
            connection.execute(
                "INSERT INTO child_profiles(telegram_id, age_months, gender, "
                "diagnosis_notes_encrypted, updated_at, birth_date_encrypted) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(telegram_id) DO UPDATE SET age_months = excluded.age_months, "
                "gender = excluded.gender, diagnosis_notes_encrypted = excluded.diagnosis_notes_encrypted, "
                "updated_at = excluded.updated_at, "
                "birth_date_encrypted = COALESCE(excluded.birth_date_encrypted, "
                "child_profiles.birth_date_encrypted)",
                (
                    telegram_id,
                    age_months,
                    gender,
                    encrypted_diagnosis_notes,
                    now,
                    encrypted_birth_date,
                ),
            )

    def update_interests(self, telegram_id: int, interests: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE child_profiles SET interests = ?, updated_at = ? WHERE telegram_id = ?",
                (interests, datetime.now().astimezone().isoformat(), telegram_id),
            )

    def set_delivery_time(self, telegram_id: int, delivery_time: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE users SET delivery_time = ? WHERE telegram_id = ?",
                (delivery_time, telegram_id),
            )

    def set_language(
        self, telegram_id: int, language: str, default_delivery_time: str
    ) -> None:
        if language not in {"ru", "uk"}:
            raise ValueError(f"Unsupported language: {language}")
        now = datetime.now().astimezone().isoformat()
        with self.connect() as connection:
            current = connection.execute(
                "SELECT language FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
            connection.execute(
                "INSERT INTO users(telegram_id, created_at, delivery_time, language) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(telegram_id) DO UPDATE SET "
                "language = excluded.language",
                (telegram_id, now, default_delivery_time, language),
            )
            if current and current["language"] != language:
                connection.execute(
                    "DELETE FROM plans WHERE telegram_id = ? AND status = 'pending'",
                    (telegram_id,),
                )

    def language(self, telegram_id: int) -> str:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT language FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
        return str(row["language"]) if row else "ru"

    def identity_consent_status(self, telegram_id: int) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT identity_consent FROM users WHERE telegram_id = ?",
                (telegram_id,),
            ).fetchone()
        return str(row["identity_consent"]) if row else None

    def set_identity_consent_status(self, telegram_id: int, status: str) -> None:
        if status not in {"pending", "collecting", "denied"}:
            raise ValueError(f"Unsupported identity consent status: {status}")
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE users SET identity_consent = ?, identity_data_encrypted = NULL "
                "WHERE telegram_id = ?",
                (status, telegram_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"User {telegram_id} does not exist.")

    def save_admin_identity(self, telegram_id: int, encrypted_data: str) -> None:
        if not encrypted_data:
            raise ValueError("Encrypted identity data must not be empty.")
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE users SET identity_consent = 'granted', identity_data_encrypted = ? "
                "WHERE telegram_id = ? AND identity_consent = 'collecting'",
                (encrypted_data, telegram_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(
                    f"User {telegram_id} has not consented to identity data collection."
                )

    def admin_identity_encrypted(self, telegram_id: int) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT identity_data_encrypted FROM users "
                "WHERE telegram_id = ? AND identity_consent = 'granted'",
                (telegram_id,),
            ).fetchone()
        return (
            str(row["identity_data_encrypted"])
            if row and row["identity_data_encrypted"]
            else None
        )

    def set_schedule_start_date(self, telegram_id: int, start_date: str) -> None:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE users SET schedule_start_date = ? WHERE telegram_id = ?",
                (start_date, telegram_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"User {telegram_id} does not exist.")

    def activate_user(self, telegram_id: int) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE users SET active = 1 WHERE telegram_id = ?",
                (telegram_id,),
            )

    def record_skill(
        self, telegram_id: int, skill_id: str, domain: str, result: str
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO skill_observations(telegram_id, skill_id, domain, result, observed_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (telegram_id, skill_id, domain, result, datetime.now().astimezone().isoformat()),
            )

    def profile(self, telegram_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT u.telegram_id, u.created_at, u.active, u.subscription_status, "
                "u.delivery_time, u.language, c.age_months, c.gender, c.diagnosis_notes_encrypted, "
                "c.interests FROM users u JOIN child_profiles c USING(telegram_id) "
                "WHERE u.telegram_id = ?",
                (telegram_id,),
            ).fetchone()
            skills = connection.execute(
                "SELECT s.skill_id, s.domain, s.result, s.observed_at "
                "FROM skill_observations s WHERE s.telegram_id = ? AND s.id = "
                "(SELECT latest.id FROM skill_observations latest "
                "WHERE latest.telegram_id = s.telegram_id AND latest.skill_id = s.skill_id "
                "ORDER BY latest.observed_at DESC, latest.id DESC LIMIT 1) "
                "ORDER BY s.domain, s.skill_id",
                (telegram_id,),
            ).fetchall()
        if row is None:
            return None
        profile = dict(row)
        profile["skills"] = [dict(skill) for skill in skills]
        profile["diagnosis_notes_encrypted"] = bool(profile["diagnosis_notes_encrypted"])
        return profile

    def encrypted_diagnosis_notes(self, telegram_id: int) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT diagnosis_notes_encrypted FROM child_profiles WHERE telegram_id = ?",
                (telegram_id,),
            ).fetchone()
        return str(row["diagnosis_notes_encrypted"]) if row and row["diagnosis_notes_encrypted"] else None

    def encrypted_birth_date(self, telegram_id: int) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT birth_date_encrypted FROM child_profiles WHERE telegram_id = ?",
                (telegram_id,),
            ).fetchone()
        return (
            str(row["birth_date_encrypted"])
            if row and row["birth_date_encrypted"]
            else None
        )

    def list_users(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT u.telegram_id, u.created_at, u.active, u.subscription_status, "
                "u.delivery_time, u.language, u.identity_consent, "
                "c.age_months, c.gender FROM users u "
                "LEFT JOIN child_profiles c USING(telegram_id) ORDER BY u.created_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_user(self, telegram_id: int) -> bool:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM content_library WHERE cohort LIKE ?",
                (f"personal:{telegram_id}:%",),
            )
            cursor = connection.execute(
                "DELETE FROM users WHERE telegram_id = ?", (telegram_id,)
            )
        return cursor.rowcount > 0

    def due_users(self, current_time: str, plan_date: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT u.telegram_id, u.language, c.age_months, c.gender, c.interests "
                "FROM users u JOIN child_profiles c USING(telegram_id) "
                "WHERE u.active = 1 AND u.delivery_time <= ? AND u.schedule_start_date <= ? "
                "AND NOT EXISTS "
                "                (SELECT 1 FROM plans p WHERE p.telegram_id = u.telegram_id AND p.plan_date = ? "
                "AND p.status = 'sent')",
                (current_time, plan_date, plan_date),
            ).fetchall()
        return [dict(row) for row in rows]

    def recent_feedback(self, telegram_id: int) -> str:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT feedback FROM plans WHERE telegram_id = ? AND feedback IS NOT NULL "
                "ORDER BY plan_date DESC LIMIT 1",
                (telegram_id,),
            ).fetchone()
        return str(row["feedback"]) if row else "no previous feedback"

    def save_plan(
        self,
        telegram_id: int,
        plan_date: str,
        content: dict[str, Any],
        *,
        status: str = "sent",
        sent_at: str | None = None,
    ) -> None:
        now = datetime.now().astimezone().isoformat()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO plans(telegram_id, plan_date, content_json, status, created_at, sent_at, "
                "delivery_state_json) VALUES (?, ?, ?, ?, ?, ?, '[]') "
                "ON CONFLICT(telegram_id, plan_date) DO UPDATE SET "
                "content_json = excluded.content_json, status = excluded.status, "
                "created_at = excluded.created_at, sent_at = excluded.sent_at, "
                "delivery_state_json = '[]', feedback = NULL",
                (telegram_id, plan_date, json.dumps(content, ensure_ascii=False), status, now, sent_at),
            )
        self.mark_plan_content_used(telegram_id, content, plan_date)

    def update_plan_feedback(self, telegram_id: int, plan_date: str, feedback: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE plans SET feedback = ? WHERE telegram_id = ? AND plan_date = ?",
                (feedback, telegram_id, plan_date),
            )

    def get_plan(self, telegram_id: int, plan_date: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM plans WHERE telegram_id = ? AND plan_date = ?",
                (telegram_id, plan_date),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["content"] = json.loads(result.pop("content_json"))
        result["delivery_state"] = json.loads(result.pop("delivery_state_json"))
        return result

    def list_plans(self, telegram_id: int) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT plan_date, content_json, status, feedback, created_at, sent_at "
                "FROM plans WHERE telegram_id = ? ORDER BY plan_date DESC",
                (telegram_id,),
            ).fetchall()
        return [
            {**dict(row), "content": json.loads(row["content_json"])}
            for row in rows
        ]

    def save_image(self, tag_hash: str, tag: str, image_bytes: bytes) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO image_cache(tag_hash, tag, image_bytes, created_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(tag_hash) DO NOTHING",
                (tag_hash, tag, image_bytes, datetime.now().astimezone().isoformat()),
            )

    def get_image(self, tag_hash: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT tag_hash, tag, image_bytes, telegram_file_id FROM image_cache "
                "WHERE tag_hash = ?",
                (tag_hash,),
            ).fetchone()
        return dict(row) if row else None

    def save_telegram_file_id(self, tag_hash: str, file_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE image_cache SET telegram_file_id = ? WHERE tag_hash = ?",
                (file_id, tag_hash),
            )

    def list_images(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT tag_hash, tag, telegram_file_id, created_at FROM image_cache "
                "ORDER BY created_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_image(self, tag_hash: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute("DELETE FROM image_cache WHERE tag_hash = ?", (tag_hash,))
        return cursor.rowcount > 0

    def record_ai_usage(
        self, task: str, input_tokens: int, output_tokens: int, cost_usd: float
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO ai_usage(task, input_tokens, output_tokens, cost_usd, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    task,
                    input_tokens,
                    output_tokens,
                    max(0.0, cost_usd),
                    datetime.now().astimezone().isoformat(),
                ),
            )

    def daily_ai_cost(self) -> float:
        start = datetime.now().astimezone().date().isoformat()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM ai_usage "
                "WHERE substr(created_at, 1, 10) = ?",
                (start,),
            ).fetchone()
        return float(row["total"])

    def cache_content(
        self,
        category: str,
        cohort: str,
        dedupe_key: str,
        content_hash: str,
        content: dict[str, Any],
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO content_library"
                "(content_hash, category, cohort, dedupe_key, content_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    content_hash,
                    category,
                    cohort,
                    dedupe_key,
                    json.dumps(content, ensure_ascii=False, sort_keys=True),
                    datetime.now().astimezone().isoformat(),
                ),
            )

    def available_content(
        self,
        telegram_id: int,
        category: str,
        cohort: str,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT c.content_hash, c.category, c.cohort, c.dedupe_key, c.content_json "
                "FROM content_library c LEFT JOIN user_content_usage u "
                "ON u.telegram_id = ? AND u.category = c.category "
                "AND u.dedupe_key = c.dedupe_key "
                "WHERE c.category = ? AND c.cohort = ? AND u.telegram_id IS NULL "
                "ORDER BY c.created_at, c.content_hash LIMIT ?",
                (telegram_id, category, cohort, limit),
            ).fetchall()
        return [
            {**dict(row), "content": json.loads(row["content_json"])}
            for row in rows
        ]

    def claim_content(
        self,
        telegram_id: int,
        category: str,
        content_hash: str,
        *,
        plan_date: str | None = None,
    ) -> bool:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            item = connection.execute(
                "SELECT dedupe_key, content_json FROM content_library "
                "WHERE content_hash = ? AND category = ?",
                (content_hash, category),
            ).fetchone()
            if item is None:
                return False
            cursor = connection.execute(
                "INSERT OR IGNORE INTO user_content_usage"
                "(telegram_id, category, dedupe_key, content_hash, content_json, plan_date, used_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    telegram_id,
                    category,
                    item["dedupe_key"],
                    content_hash,
                    item["content_json"],
                    plan_date,
                    datetime.now().astimezone().isoformat(),
                ),
            )
        return cursor.rowcount == 1

    def claim_content_batch(
        self,
        telegram_id: int,
        items: dict[str, str],
        *,
        plan_date: str | None = None,
    ) -> bool:
        now = datetime.now().astimezone().isoformat()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows: list[tuple[str, str, str, str]] = []
            for category, content_hash in items.items():
                item = connection.execute(
                    "SELECT dedupe_key, content_json FROM content_library "
                    "WHERE content_hash = ? AND category = ?",
                    (content_hash, category),
                ).fetchone()
                if item is None:
                    return False
                already_used = connection.execute(
                    "SELECT 1 FROM user_content_usage "
                    "WHERE telegram_id = ? AND category = ? AND dedupe_key = ?",
                    (telegram_id, category, item["dedupe_key"]),
                ).fetchone()
                if already_used:
                    return False
                rows.append((category, content_hash, item["dedupe_key"], item["content_json"]))
            connection.executemany(
                "INSERT INTO user_content_usage"
                "(telegram_id, category, dedupe_key, content_hash, content_json, plan_date, used_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (telegram_id, category, dedupe_key, content_hash, content_json, plan_date, now)
                    for category, content_hash, dedupe_key, content_json in rows
                ],
            )
        return True

    def mark_content_used(
        self,
        telegram_id: int,
        category: str,
        dedupe_key: str,
        content_hash: str,
        content: Any,
        *,
        plan_date: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO user_content_usage"
                "(telegram_id, category, dedupe_key, content_hash, content_json, plan_date, used_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    telegram_id,
                    category,
                    dedupe_key,
                    content_hash,
                    json.dumps(content, ensure_ascii=False, sort_keys=True),
                    plan_date,
                    datetime.now().astimezone().isoformat(),
                ),
            )

    def has_used_content(
        self,
        telegram_id: int,
        category: str,
        dedupe_key: str,
        *,
        excluding_plan_date: str | None = None,
    ) -> bool:
        with self.connect() as connection:
            query = (
                "SELECT 1 FROM user_content_usage "
                "WHERE telegram_id = ? AND category = ? AND dedupe_key = ?"
            )
            parameters: tuple[Any, ...] = (telegram_id, category, dedupe_key)
            if excluding_plan_date is not None:
                query += " AND (plan_date IS NULL OR plan_date != ?)"
                parameters += (excluding_plan_date,)
            row = connection.execute(query, parameters).fetchone()
        return row is not None

    def has_seen_image(
        self,
        telegram_id: int,
        sha256_hash: str,
        difference_hash: str,
        *,
        max_distance: int = 4,
    ) -> bool:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT dedupe_key FROM user_content_usage "
                "WHERE telegram_id = ? AND category = 'image' "
                "AND (dedupe_key = ? OR dedupe_key LIKE 'dhash:%')",
                (telegram_id, f"sha256:{sha256_hash}"),
            ).fetchall()
        candidate = int(difference_hash, 16)
        for row in rows:
            key = row["dedupe_key"]
            if key == f"sha256:{sha256_hash}":
                return True
            previous = key.removeprefix("dhash:")
            if previous and (candidate ^ int(previous, 16)).bit_count() <= max_distance:
                return True
        return False

    def used_content(
        self,
        telegram_id: int,
        category: str,
        *,
        excluding_plan_date: str | None = None,
    ) -> list[dict[str, Any]]:
        with self.connect() as connection:
            query = (
                "SELECT dedupe_key, content_hash, content_json FROM user_content_usage "
                "WHERE telegram_id = ? AND category = ?"
            )
            parameters: tuple[Any, ...] = (telegram_id, category)
            if excluding_plan_date is not None:
                query += " AND (plan_date IS NULL OR plan_date != ?)"
                parameters += (excluding_plan_date,)
            rows = connection.execute(query + " ORDER BY used_at", parameters).fetchall()
        return [
            {**dict(row), "content": json.loads(row["content_json"])}
            for row in rows
        ]

    def user_content_library(self, telegram_id: int) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT c.content_hash, c.category, c.cohort, c.dedupe_key, "
                "c.content_json, c.created_at FROM content_library c "
                "ORDER BY c.created_at DESC LIMIT 200"
            ).fetchall()
            usage = connection.execute(
                "SELECT category, dedupe_key FROM user_content_usage WHERE telegram_id = ?",
                (telegram_id,),
            ).fetchall()
        seen = {(row["category"], row["dedupe_key"]) for row in usage}
        return [
            {
                **dict(row),
                "content": json.loads(row["content_json"]),
                "used_by_user": (row["category"], row["dedupe_key"]) in seen,
            }
            for row in rows
        ]

    def delete_cached_content(self, content_hash: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM content_library WHERE content_hash = ?", (content_hash,)
            )
        return cursor.rowcount > 0

    def generation_cooling_down(self, telegram_id: int, scope: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT retry_after FROM content_generation_cooldowns "
                "WHERE telegram_id = ? AND scope = ?",
                (telegram_id, scope),
            ).fetchone()
        if row is None:
            return False
        retry_after = datetime.fromisoformat(row["retry_after"])
        if retry_after > datetime.now().astimezone():
            return True
        self.clear_generation_cooldown(telegram_id, scope)
        return False

    def set_generation_cooldown(
        self, telegram_id: int, scope: str, *, seconds: int = 3600
    ) -> None:
        retry_after = (
            datetime.now().astimezone().timestamp() + seconds
        )
        value = datetime.fromtimestamp(retry_after).astimezone().isoformat()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO content_generation_cooldowns(telegram_id, scope, retry_after) "
                "VALUES (?, ?, ?) ON CONFLICT(telegram_id, scope) "
                "DO UPDATE SET retry_after = excluded.retry_after",
                (telegram_id, scope, value),
            )

    def clear_generation_cooldown(self, telegram_id: int, scope: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM content_generation_cooldowns "
                "WHERE telegram_id = ? AND scope = ?",
                (telegram_id, scope),
            )

    def mark_plan_content_used(
        self, telegram_id: int, plan: dict[str, Any], plan_date: str
    ) -> None:
        components = (
            ("focus_task", plan["focus_task"]),
            ("activity", plan["activity"]),
            ("advice", plan["parent_advice"]),
        )
        for category, content in components:
            content_json = json.dumps(
                content, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            dedupe_key = f"sha256:{hashlib.sha256(content_json.encode('utf-8')).hexdigest()}"
            self.mark_content_used(
                telegram_id,
                category,
                dedupe_key,
                dedupe_key,
                content,
                plan_date=plan_date,
            )

    def mark_plan_part_sent(self, telegram_id: int, plan_date: str, part: str) -> None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT delivery_state_json FROM plans WHERE telegram_id = ? AND plan_date = ?",
                (telegram_id, plan_date),
            ).fetchone()
            if row is None:
                raise ValueError(f"Plan {plan_date} for user {telegram_id} does not exist.")
            state = json.loads(row["delivery_state_json"])
            if part not in state:
                state.append(part)
                connection.execute(
                    "UPDATE plans SET delivery_state_json = ? "
                    "WHERE telegram_id = ? AND plan_date = ?",
                    (json.dumps(state), telegram_id, plan_date),
                )

    def mark_plan_sent(self, telegram_id: int, plan_date: str) -> None:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE plans SET status = 'sent', sent_at = ? "
                "WHERE telegram_id = ? AND plan_date = ?",
                (
                    datetime.now().astimezone().isoformat(),
                    telegram_id,
                    plan_date,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Plan {plan_date} for user {telegram_id} does not exist.")
