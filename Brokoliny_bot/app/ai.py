from __future__ import annotations

import base64
import os
from typing import TypeVar

import httpx
import litellm
from pydantic import BaseModel

from app.database import Database
from app.schemas import AssessmentResponse, DailyPlan
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

    async def _structured_completion(self, task: str, prompt: str, schema: type[T]) -> T:
        budget = float(
            self.database.get_setting("daily_budget_usd", self.daily_budget_usd)
        )
        if self.database.daily_ai_cost() >= budget:
            raise AIBudgetExceeded("Daily AI budget has been reached.")

        route = self.database.get_route(task)
        configured_max_tokens = int(
            self.database.get_setting("max_tokens", self.max_tokens)
        )
        max_tokens = min(int(route["max_tokens"]), configured_max_tokens)
        schema_name = schema.__name__.lower()
        messages = [
            {"role": "system", "content": self.database.get_prompt("safety")},
            {"role": "system", "content": self.database.get_prompt(task)},
            {"role": "user", "content": prompt},
        ]
        response = await litellm.acompletion(
            model=self._model_name(route["provider"], route["model"]),
            messages=messages,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": schema.model_json_schema(),
                },
            },
            max_tokens=max_tokens,
            api_key=self._api_key(route["provider"]),
        )
        content = response.choices[0].message.content
        if not isinstance(content, str) or not content:
            raise ValueError(f"The {task} model returned an empty structured response.")
        parsed = schema.model_validate_json(content)
        usage = response.usage
        hidden = getattr(response, "_hidden_params", {}) or {}
        self.database.record_ai_usage(
            task,
            int(getattr(usage, "prompt_tokens", 0) or 0),
            int(getattr(usage, "completion_tokens", 0) or 0),
            float(hidden.get("response_cost", 0) or 0),
        )
        return parsed

    async def generate_assessment(
        self,
        age_months: int,
        accessibility_notes: str = "",
        *,
        language: str = "ru",
        excluded_skill_ids: list[str] | None = None,
        excluded_questions: list[str] | None = None,
    ) -> AssessmentResponse:
        exclusions = ", ".join(excluded_skill_ids or []) or "none"
        previous_questions = " | ".join(excluded_questions or []) or "none"
        response_language = {"ru": "Russian", "uk": "Ukrainian"}.get(language, "Russian")
        prompt = (
            f"Prepare play-based observations for a child aged {age_months} months. "
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
        return await self._structured_completion("diagnostics", prompt, AssessmentResponse)

    async def generate_plan(
        self,
        profile: dict[str, object],
        gaps: list[dict[str, object]],
        feedback: str,
        *,
        avoid_recent: dict[str, list[str]] | None = None,
    ) -> DailyPlan:
        avoided = avoid_recent or {}
        language = str(profile.get("language", "ru"))
        response_language = {"ru": "Russian", "uk": "Ukrainian"}.get(language, "Russian")
        prompt = (
            f"Write all user-facing text in {response_language}. "
            f"Child age: {profile['age_months']} months. "
            f"Observed learning opportunities: {gaps or 'not yet observed'}. "
            f"Previous plan difficulty: {feedback}. "
            "Return a plan with the required printable task, practical sensory/motor "
            "activity, accurate facts, and parent advice. The activity script must give "
            "the caregiver concrete words to model, including shape or movement vocabulary. "
            "Explain real facts simply without baby talk. The worksheet must be solvable "
            "with the stated count and shapes. Do not infer a diagnosis or assumptions "
            f"about gender or interests. Do not repeat or closely paraphrase prior "
            f"focus tasks, activities, or advice: {avoided}. "
            "Default story_prompt to null. Include an image prompt only when an "
            "illustration is genuinely necessary to understand this specific activity; "
            "do not generate images for routine tasks. "
        )
        return await self._structured_completion("daily_plan", prompt, DailyPlan)

    async def generate_image(self, prompt: str) -> bytes:
        route = self.database.get_route("image")
        budget = float(
            self.database.get_setting("daily_budget_usd", self.daily_budget_usd)
        )
        if self.database.daily_ai_cost() >= budget:
            raise AIBudgetExceeded("Daily AI budget has been reached.")
        response = await litellm.aimage_generation(
            model=self._model_name(route["provider"], route["model"]),
            prompt=f"{self.database.get_prompt('image').rstrip()} {prompt}",
            api_key=self._api_key(route["provider"]),
        )
        data = response.data[0]
        encoded = getattr(data, "b64_json", None)
        if encoded:
            image_bytes = base64.b64decode(encoded, validate=True)
        else:
            image_url = getattr(data, "url", None)
            if not image_url:
                raise ValueError("The image model returned neither image data nor a URL.")
            async with httpx.AsyncClient(timeout=45) as client:
                result = await client.get(image_url)
                result.raise_for_status()
                image_bytes = result.content
        if not image_bytes:
            raise ValueError("The image model returned an empty image.")
        hidden = getattr(response, "_hidden_params", {}) or {}
        self.database.record_ai_usage(
            "image",
            0,
            0,
            float(hidden.get("response_cost", 0) or 0),
        )
        return image_bytes
