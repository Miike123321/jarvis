from __future__ import annotations

import hashlib
import json
import logging
import re
from difflib import SequenceMatcher
from typing import Any

from app.ai import AIOrchestrator
from app.database import Database

logger = logging.getLogger(__name__)
MAX_GENERATION_ATTEMPTS = 5
MAX_GENERATIONS_PER_REQUEST = 2
GENERATION_COOLDOWN_SECONDS = 3600


class ContentExhausted(RuntimeError):
    pass


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _age_cohort(age_months: int) -> str:
    return f"{age_months}m"


def _component_identity(category: str, content: Any) -> tuple[str, str]:
    serialized = _canonical_json(content)
    dedupe_key = f"sha256:{_hash(serialized)}"
    return dedupe_key, _hash(f"{category}:{serialized}")


def _descriptor(category: str, content: Any) -> str:
    if category == "focus_task" and isinstance(content, dict):
        worksheet = content.get("worksheet", {})
        values = (
            content.get("title", ""),
            worksheet.get("activity_type", ""),
            worksheet.get("shape", ""),
            str(worksheet.get("count", "")),
            worksheet.get("instructions", ""),
            " ".join(worksheet.get("questions", [])),
        )
    elif category == "activity" and isinstance(content, dict):
        values = (
            content.get("title", ""),
            " ".join(content.get("materials", [])),
            content.get("parent_script", ""),
            " ".join(content.get("age_facts", [])),
            content.get("story_prompt") or "",
        )
    elif category == "advice":
        values = (content.get("text", ""),) if isinstance(content, dict) else (str(content),)
    elif category == "assessment_question" and isinstance(content, dict):
        values = (
            content.get("skill_id", ""),
            " ".join(content.get("domains", [])),
            content.get("parent_script", ""),
        )
    else:
        values = (str(content),)
    return re.sub(r"\s+", " ", " ".join(str(value).casefold() for value in values)).strip()


def _similar_to_history(category: str, content: Any, history: list[dict[str, Any]]) -> bool:
    descriptor = _descriptor(category, content)
    if not descriptor:
        return False
    for previous in history:
        old = _descriptor(category, previous["content"])
        if old and SequenceMatcher(None, descriptor, old, autojunk=False).ratio() >= 0.88:
            return True
    return False


class ContentLibrary:
    def __init__(self, database: Database, ai: AIOrchestrator) -> None:
        self.database = database
        self.ai = ai

    def _cache_component(self, category: str, cohort: str, content: Any) -> None:
        if category == "assessment_question":
            dedupe_key = f"skill:{str(content['skill_id']).casefold()}"
            content_hash = _hash(f"{category}:{cohort}:{_canonical_json(content)}")
        else:
            dedupe_key, component_hash = _component_identity(category, content)
            content_hash = _hash(f"{cohort}:{component_hash}")
        self.database.cache_content(
            category,
            cohort,
            dedupe_key,
            content_hash,
            content if isinstance(content, dict) else {"text": content},
        )

    async def assessment_questions(
        self,
        telegram_id: int,
        age_months: int,
        accessibility_notes: str = "",
        language: str = "ru",
    ) -> list[dict[str, Any]]:
        age_cohort = _age_cohort(age_months)
        cohort = f"assessment:{language}:{age_cohort}"
        if accessibility_notes:
            cohort = f"{cohort}:private:{telegram_id}"

        claimed: list[dict[str, Any]] = []
        history = self.database.used_content(telegram_id, "assessment_question")
        generated_count = 0
        for _ in range(MAX_GENERATION_ATTEMPTS):
            available = self.database.available_content(
                telegram_id, "assessment_question", cohort, limit=10000
            )
            for item in available:
                if _similar_to_history(
                    "assessment_question", item["content"], history
                ):
                    continue
                if self.database.claim_content(
                    telegram_id, "assessment_question", item["content_hash"]
                ):
                    claimed.append(
                        {**item["content"], "_content_hash": item["content_hash"]}
                    )
                    history.append({"content": item["content"]})
                    if len(claimed) == 4:
                        self.database.clear_generation_cooldown(
                            telegram_id, cohort
                        )
                        return claimed

            if generated_count >= MAX_GENERATIONS_PER_REQUEST:
                self.database.set_generation_cooldown(
                    telegram_id, cohort, seconds=GENERATION_COOLDOWN_SECONDS
                )
                raise ContentExhausted(
                    "Сейчас нет достаточного количества новых вопросов. "
                    "Мы не будем повторять старые; попробуйте позже."
                )
            if self.database.generation_cooling_down(telegram_id, cohort):
                raise ContentExhausted(
                    "Вопросы для вас недавно уже запрашивались, но новых пока нет. "
                    "Попробуйте позже — старые вопросы повторно не отправляются."
                )
            used = self.database.used_content(telegram_id, "assessment_question")
            excluded = [
                row["dedupe_key"].removeprefix("skill:")
                for row in used
                if row["dedupe_key"].startswith("skill:")
            ]
            excluded_questions = [
                _descriptor("assessment_question", row["content"])[:180]
                for row in used[-20:]
            ]
            try:
                generated = await self.ai.generate_assessment(
                    age_months,
                    accessibility_notes,
                    language=language,
                    excluded_skill_ids=excluded,
                    excluded_questions=excluded_questions,
                )
            except Exception:
                self.database.set_generation_cooldown(
                    telegram_id, cohort, seconds=GENERATION_COOLDOWN_SECONDS
                )
                raise
            generated_count += 1
            before = len(available)
            for question in generated.questions:
                self._cache_component(
                    "assessment_question", cohort, question.model_dump()
                )
            after = len(
                self.database.available_content(
                    telegram_id, "assessment_question", cohort, limit=10000
                )
            )
            if after <= before and not available:
                logger.warning(
                    "Assessment provider generated no unseen questions for user %s.",
                    telegram_id,
                )

        raise ContentExhausted(
            "Не удалось подготовить достаточно новых вопросов без повторов. "
            "Попробуйте позже или обратитесь к администратору."
        )

    @staticmethod
    def plan_cohort(profile: dict[str, Any], feedback: str) -> str:
        domains = sorted(
            {
                domain
                for skill in profile.get("skills", [])
                if skill["result"] != "independent"
                for domain in str(skill["domain"]).split(",")
            }
        )
        return (
            f"plan:{profile.get('language', 'ru')}:"
            f"{_age_cohort(int(profile['age_months']))}:"
            f"{','.join(domains) or 'general'}:{feedback}"
        )

    async def daily_plan(
        self,
        telegram_id: int,
        profile: dict[str, Any],
        feedback: str,
        plan_date: str,
    ) -> dict[str, Any]:
        cohort = self.plan_cohort(profile, feedback)
        categories = ("focus_task", "activity", "advice")
        generated_count = 0

        for _ in range(MAX_GENERATION_ATTEMPTS):
            available = {
                category: self._fresh_available(telegram_id, category, cohort)
                for category in categories
            }
            if any(not items for items in available.values()):
                if generated_count >= MAX_GENERATIONS_PER_REQUEST:
                    self.database.set_generation_cooldown(
                        telegram_id, cohort, seconds=GENERATION_COOLDOWN_SECONDS
                    )
                    raise ContentExhausted(
                        "Пул новых заданий пока исчерпан. Старые задания и советы "
                        "повторно не отправляются; попробуйте позже."
                    )
                if self.database.generation_cooling_down(telegram_id, cohort):
                    raise ContentExhausted(
                        "Для вас недавно уже создавали новые задания. "
                        "Попробуйте позже — прежний контент не повторится."
                    )
                history = {
                    category: [
                        _descriptor(category, row["content"])[:180]
                        for row in self.database.used_content(telegram_id, category)[-12:]
                    ]
                    for category in categories
                }
                try:
                    generated = await self.ai.generate_plan(
                        profile,
                        [
                            skill
                            for skill in profile.get("skills", [])
                            if skill["result"] != "independent"
                        ],
                        feedback,
                        avoid_recent=history,
                    )
                except Exception:
                    self.database.set_generation_cooldown(
                        telegram_id, cohort, seconds=GENERATION_COOLDOWN_SECONDS
                    )
                    raise
                generated_count += 1
                self._cache_daily_plan(cohort, generated.model_dump())
                continue

            selected_items: dict[str, dict[str, Any]] = {}
            for category in categories:
                selected_items[category] = available[category][0]
            if self.database.claim_content_batch(
                telegram_id,
                {
                    category: item["content_hash"]
                    for category, item in selected_items.items()
                },
                plan_date=plan_date,
            ):
                self.database.clear_generation_cooldown(telegram_id, cohort)
                selected = {
                    category: item["content"]
                    for category, item in selected_items.items()
                }
                advice = selected["advice"]
                return {
                    "focus_task": selected["focus_task"],
                    "activity": selected["activity"],
                    "parent_advice": (
                        advice["text"] if isinstance(advice, dict) else advice
                    ),
                }

        raise ContentExhausted(
            "Не удалось подготовить новый план без повторения заданий или совета. "
            "Ничего повторного пользователю не отправлено."
        )

    def _fresh_available(
        self, telegram_id: int, category: str, cohort: str
    ) -> list[dict[str, Any]]:
        available = self.database.available_content(
            telegram_id, category, cohort, limit=10000
        )
        history = self.database.used_content(telegram_id, category)
        return [
            item
            for item in available
            if not _similar_to_history(category, item["content"], history)
        ]

    def repeated_plan_components(
        self,
        telegram_id: int,
        plan: dict[str, Any],
        *,
        excluding_plan_date: str | None = None,
    ) -> list[str]:
        repeated: list[str] = []
        components = (
            ("focus_task", plan["focus_task"]),
            ("activity", plan["activity"]),
            ("advice", plan["parent_advice"]),
        )
        for category, content in components:
            dedupe_key, _ = _component_identity(category, content)
            history = self.database.used_content(
                telegram_id,
                category,
                excluding_plan_date=excluding_plan_date,
            )
            if self.database.has_used_content(
                telegram_id,
                category,
                dedupe_key,
                excluding_plan_date=excluding_plan_date,
            ) or _similar_to_history(category, content, history):
                repeated.append(category)
        return repeated

    def _cache_daily_plan(self, cohort: str, plan: dict[str, Any]) -> None:
        for category in ("focus_task", "activity", "advice"):
            content = (
                plan["parent_advice"]
                if category == "advice"
                else plan[category]
            )
            self._cache_component(category, cohort, content)
