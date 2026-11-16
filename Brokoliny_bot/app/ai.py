from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from typing import TypeVar

import httpx
import litellm
from pydantic import BaseModel

from app.database import Database
from app.prompt_templates import render_prompt_template
from app.schemas import (
    Activity,
    AssessmentResponse,
    DailyPlan,
    FocusTask,
    ParentAdvice,
)
from app.security import SecretVault

T = TypeVar("T", bound=BaseModel)

PROVIDER_PREFIXES = {
    "gemini": "gemini",
    "openai": "openai",
    "anthropic": "anthropic",
    "vertex_ai": "vertex_ai",
    "replicate": "replicate",
}
PROVIDER_ENV_VARS = {
    "gemini": "GEMINI_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "replicate": "REPLICATE_API_TOKEN",
}


class AIBudgetExceeded(RuntimeError):
    pass


class AIOrchestrator:
    def __init__(
        self,
        database: Database,
        vault: SecretVault,
        *,
        daily_budget_usd: float,
        max_tokens: int,
    ) -> None:
        self.database = database
        self.vault = vault
        self.daily_budget_usd = daily_budget_usd
        self.max_tokens = max_tokens

    def _model_name(self, provider: str, model: str) -> str:
        prefix = PROVIDER_PREFIXES.get(provider)
        if prefix is None:
            raise ValueError(f"Unsupported AI provider: {provider}")
        return model if model.startswith(f"{prefix}/") else f"{prefix}/{model}"

    def _api_key(self, provider: str) -> str | None:
        encrypted = self.database.get_secret(provider)
        if encrypted:
            return self.vault.decrypt(encrypted)
        env_name = PROVIDER_ENV_VARS.get(provider)
        return os.getenv(env_name) if env_name else None

    async def _structured_completion(
        self,
        task: str,
        prompt: str,
        schema: type[T],
        *,
        route_task: str | None = None,
        prompt_name: str | None = None,
        telegram_id: int | None = None,
        prompt_override: str | None = None,
        prompt_context: dict[str, object] | None = None,
        request_traces: list[dict[str, object]] | None = None,
    ) -> T:
        budget = float(
            self.database.get_setting("daily_budget_usd", self.daily_budget_usd)
        )
        if self.database.daily_ai_cost() >= budget:
            raise AIBudgetExceeded("Daily AI budget has been reached.")

        route = self.database.get_route(route_task or task)
        configured_max_tokens = int(
            self.database.get_setting("max_tokens", self.max_tokens)
        )
        max_tokens = min(int(route["max_tokens"]), configured_max_tokens)
        schema_name = schema.__name__.lower()
        messages = [{"role": "system", "content": self.database.get_prompt("safety")}]
        if route_task == "daily_plan":
            messages.append(
                {"role": "system", "content": self.database.get_prompt("daily_plan")}
            )
        prompt_template = (
            prompt_override
            if prompt_override is not None
            else self.database.get_prompt(prompt_name or task)
        )
        messages.extend(
            [
                {
                    "role": "system",
                    "content": render_prompt_template(
                        prompt_template, prompt_context or {}
                    ),
                },
                {"role": "user", "content": prompt},
            ]
        )
        model_name = self._model_name(route["provider"], route["model"])
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "strict": True,
                "schema": schema.model_json_schema(),
            },
        }
        request_log_id = self.database.create_ai_request_log(
            task,
            telegram_id,
            model_name,
            self.vault.encrypt(
                json.dumps(
                    {
                        "model": model_name,
                        "max_tokens": max_tokens,
                        "messages": messages,
                        "response_format": response_format,
                    },
                    ensure_ascii=False,
                )
            ),
        )
        trace_entry: dict[str, object] = {
            "id": request_log_id,
            "task": task,
        }
        if request_traces is not None:
            request_traces.append(trace_entry)
        started_at = time.perf_counter()
        raw_content: str | None = None
        validation_succeeded = False
        try:
            response = await litellm.acompletion(
                model=model_name,
                messages=messages,
                response_format=response_format,
                max_tokens=max_tokens,
                api_key=self._api_key(route["provider"]),
            )
            raw_content = response.choices[0].message.content
            if not isinstance(raw_content, str) or not raw_content:
                raise ValueError(f"The {task} model returned an empty structured response.")
            parsed = schema.model_validate_json(raw_content)
            validation_succeeded = True
            usage = getattr(response, "usage", None)
            hidden = getattr(response, "_hidden_params", {}) or {}
            prompt_tokens_value = getattr(usage, "prompt_tokens", None)
            completion_tokens_value = getattr(usage, "completion_tokens", None)
            response_cost_value = hidden.get("response_cost")
            duration_ms = int((time.perf_counter() - started_at) * 1000)
            self.database.record_ai_usage(
                task,
                int(prompt_tokens_value or 0),
                int(completion_tokens_value or 0),
                float(response_cost_value or 0),
            )
            response_data = {
                "content": raw_content,
                "usage": {
                    "prompt_tokens": (
                        int(prompt_tokens_value)
                        if prompt_tokens_value is not None
                        else None
                    ),
                    "completion_tokens": (
                        int(completion_tokens_value)
                        if completion_tokens_value is not None
                        else None
                    ),
                },
                "cost_usd": (
                    float(response_cost_value)
                    if response_cost_value is not None
                    else None
                ),
                "duration_ms": duration_ms,
                "response_id": getattr(response, "id", None),
            }
            self.database.finish_ai_request_log(
                request_log_id,
                "success",
                self.vault.encrypt(
                    json.dumps(response_data, ensure_ascii=False)
                ),
            )
            trace_entry.update({"status": "success", **response_data})
            return parsed
        except Exception as exc:
            duration_ms = int((time.perf_counter() - started_at) * 1000)
            error_data = {
                "content": raw_content,
                "error": f"{type(exc).__name__}: {exc}",
                "duration_ms": duration_ms,
            }
            self.database.finish_ai_request_log(
                request_log_id,
                "invalid" if raw_content and not validation_succeeded else "error",
                self.vault.encrypt(
                    json.dumps(error_data, ensure_ascii=False)
                ),
            )
            trace_entry.update({"status": "error", **error_data})
            raise

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
        exclusions = ", ".join(excluded_skill_ids or []) or "none"
        previous_questions = " | ".join(excluded_questions or []) or "none"
        response_language = {"ru": "Russian", "uk": "Ukrainian"}.get(language, "Russian")
        prompt = (
            f"Prepare play-based observations for a child aged {age_months} months. "
            f"Child gender: {(child_context or {}).get('gender') or 'not provided'}. "
            f"Previously observed skills: {(child_context or {}).get('skills') or 'none recorded'}. "
            f"Child interests: {(child_context or {}).get('interests') or 'not provided'}. "
            "Include several foundational skills from earlier age ranges and current "
            "age opportunities (cumulative, not an age pass/fail test). Every question "
            "must combine at least two developmental domains in one short playful task. "
            f"Write all user-facing text in {response_language}. "
            "Use a clear, respectful caregiver script with safe optional adaptations. "
            "A visual stimulus is optional: set stimulus_kind and stimulus_prompt to null "
            "unless a picture is genuinely useful to perform that question. "
            f"Accessibility adaptations requested: {accessibility_notes or 'none supplied'}. "
            f"Do not reuse these previously asked skill IDs: {exclusions}. "
            f"Do not repeat or closely paraphrase these previous question scripts: "
            f"{previous_questions}. "
            "Do not repeat or imitate any standardized test item."
        )
        context = {
            "child.age_months": age_months,
            "child.gender": (child_context or {}).get("gender"),
            "child.language": language,
            "child.interests": (child_context or {}).get("interests"),
            "child.skills": (child_context or {}).get("skills", []),
            "child.skill_gaps": (child_context or {}).get("skill_gaps", []),
            "child.diagnostic_results": (child_context or {}).get(
                "diagnostic_results", []
            ),
            "diagnostics.excluded_skill_ids": excluded_skill_ids or [],
            "diagnostics.excluded_questions": excluded_questions or [],
        }
        return await self._structured_completion(
            "diagnostics",
            prompt,
            AssessmentResponse,
            telegram_id=telegram_id,
            prompt_override=prompt_override,
            prompt_context=context,
            request_traces=request_traces,
        )

    async def generate_plan(
        self,
        profile: dict[str, object],
        gaps: list[dict[str, object]],
        feedback: str,
        *,
        avoid_recent: dict[str, list[str]] | None = None,
    ) -> DailyPlan:
        focus_task = await self.generate_plan_component(
            "focus_task", profile, gaps, feedback, avoid_recent=avoid_recent
        )
        activity = await self.generate_plan_component(
            "activity", profile, gaps, feedback, avoid_recent=avoid_recent
        )
        advice = await self.generate_plan_component(
            "advice", profile, gaps, feedback, avoid_recent=avoid_recent
        )
        if not isinstance(focus_task, FocusTask):
            raise TypeError("The focus task generator returned an invalid component.")
        if not isinstance(activity, Activity):
            raise TypeError("The activity generator returned an invalid component.")
        if not isinstance(advice, ParentAdvice):
            raise TypeError("The advice generator returned an invalid component.")
        return DailyPlan(
            focus_task=focus_task,
            activity=activity,
            parent_advice=advice.text,
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
        prompt_override: str | None = None,
        request_traces: list[dict[str, object]] | None = None,
    ) -> FocusTask | Activity | ParentAdvice:
        schemas: dict[str, tuple[str, type[BaseModel]]] = {
            "focus_task": ("daily_plan_task1", FocusTask),
            "activity": ("daily_plan_task2", Activity),
            "advice": ("daily_plan_advice", ParentAdvice),
        }
        if component not in schemas:
            raise ValueError(f"Unsupported daily plan component: {component}")
        task, schema = schemas[component]
        avoided = avoid_recent or {}
        language = str(profile.get("language", "ru"))
        response_language = {"ru": "Russian", "uk": "Ukrainian"}.get(language, "Russian")
        prompt = (
            f"Write all user-facing text in {response_language}. "
            f"Child age: {profile['age_months']} months. "
            f"Child gender: {profile.get('gender') or 'not provided'}. "
            f"Child interests: {profile.get('interests') or 'not provided'}. "
            f"All latest observed skills and diagnostic results: {profile.get('skills', [])}. "
            f"Observed learning opportunities: {gaps or 'not yet observed'}. "
            f"Previous plan difficulty: {feedback}. "
            f"Generate only the {component} component. Do not infer a diagnosis or "
            "abilities from gender; use interests only when they were provided. "
            f"Do not repeat or closely paraphrase prior components: {avoided}. "
        )
        prompt_context = {
            "child.age_months": profile.get("age_months"),
            "child.gender": profile.get("gender"),
            "child.language": language,
            "child.interests": profile.get("interests"),
            "child.skills": profile.get("skills", []),
            "child.skill_gaps": gaps,
            "child.diagnostic_results": profile.get("skills", []),
            "plan.previous_difficulty": feedback,
            "plan.recent_components": avoided,
        }
        return await self._structured_completion(
            task,
            prompt,
            schema,
            route_task="daily_plan",
            prompt_name=task,
            telegram_id=telegram_id,
            prompt_override=prompt_override,
            prompt_context=prompt_context,
            request_traces=request_traces,
        )

    async def generate_image(
        self,
        prompt: str,
        *,
        telegram_id: int | None = None,
        image_cache_tag_hash: str | None = None,
        request_traces: list[dict[str, object]] | None = None,
    ) -> bytes:
        route = self.database.get_route("image")
        budget = float(
            self.database.get_setting("daily_budget_usd", self.daily_budget_usd)
        )
        if self.database.daily_ai_cost() >= budget:
            raise AIBudgetExceeded("Daily AI budget has been reached.")
        model_name = self._model_name(route["provider"], route["model"])
        full_prompt = f"{self.database.get_prompt('image').rstrip()} {prompt}"
        request_log_id = self.database.create_ai_request_log(
            "image",
            telegram_id,
            model_name,
            self.vault.encrypt(
                json.dumps(
                    {
                        "model": model_name,
                        "prompt": full_prompt,
                        "image_cache_tag_hash": image_cache_tag_hash,
                    },
                    ensure_ascii=False,
                )
            ),
        )
        trace_entry: dict[str, object] = {"id": request_log_id, "task": "image"}
        if request_traces is not None:
            request_traces.append(trace_entry)
        started_at = time.perf_counter()
        try:
            response = await litellm.aimage_generation(
                model=model_name,
                prompt=full_prompt,
                api_key=self._api_key(route["provider"]),
            )
            data = response.data[0]
            encoded = getattr(data, "b64_json", None)
            if encoded:
                image_bytes = base64.b64decode(encoded, validate=True)
            else:
                image_url = getattr(data, "url", None)
                if not image_url:
                    raise ValueError(
                        "The image model returned neither image data nor a URL."
                    )
                async with httpx.AsyncClient(timeout=45) as client:
                    result = await client.get(image_url)
                    result.raise_for_status()
                    image_bytes = result.content
            if not image_bytes:
                raise ValueError("The image model returned an empty image.")
            hidden = getattr(response, "_hidden_params", {}) or {}
            response_cost_value = hidden.get("response_cost")
            duration_ms = int((time.perf_counter() - started_at) * 1000)
            self.database.record_ai_usage(
                "image",
                0,
                0,
                float(hidden.get("response_cost", 0) or 0),
            )
            self.database.finish_ai_request_log(
                request_log_id,
                "success",
                self.vault.encrypt(
                    json.dumps(
                        {
                            "response_id": getattr(response, "id", None),
                            "image_count": len(getattr(response, "data", [])),
                            "image_sha256": hashlib.sha256(image_bytes).hexdigest(),
                            "image_bytes": len(image_bytes),
                            "image_cache_tag_hash": image_cache_tag_hash,
                            "cost_usd": (
                                float(response_cost_value)
                                if response_cost_value is not None
                                else None
                            ),
                            "duration_ms": duration_ms,
                            "note": "Image binary is stored in the image cache.",
                        },
                        ensure_ascii=False,
                    )
                ),
            )
            trace_entry.update(
                {
                    "status": "success",
                    "response_id": getattr(response, "id", None),
                    "image_count": len(getattr(response, "data", [])),
                    "image_sha256": hashlib.sha256(image_bytes).hexdigest(),
                    "image_bytes": len(image_bytes),
                    "image_cache_tag_hash": image_cache_tag_hash,
                    "cost_usd": (
                        float(response_cost_value)
                        if response_cost_value is not None
                        else None
                    ),
                    "duration_ms": duration_ms,
                    "note": "Image binary is stored in the image cache.",
                }
            )
            return image_bytes
        except Exception as exc:
            duration_ms = int((time.perf_counter() - started_at) * 1000)
            error_data = {
                "error": f"{type(exc).__name__}: {exc}",
                "duration_ms": duration_ms,
            }
            self.database.finish_ai_request_log(
                request_log_id,
                "error",
                self.vault.encrypt(
                    json.dumps(error_data, ensure_ascii=False)
                ),
            )
            trace_entry.update({"status": "error", **error_data})
            raise
