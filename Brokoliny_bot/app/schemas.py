from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AssessmentQuestion(StrictModel):
    skill_id: str = Field(min_length=2, max_length=80)
    domains: list[Literal["motor", "language", "cognitive", "social", "daily_living"]] = Field(
        min_length=2, max_length=3
    )
    parent_script: str = Field(min_length=8, max_length=700)
    stimulus_kind: Literal["shapes", "counting", "pattern", "animals"] | None = None
    stimulus_prompt: str | None = Field(default=None, max_length=400)


class AssessmentResponse(StrictModel):
    questions: list[AssessmentQuestion] = Field(min_length=4, max_length=8)


class Worksheet(StrictModel):
    title: str = Field(min_length=2, max_length=100)
    activity_type: Literal["counting", "geometry", "pattern", "find_differences"]
    shape: Literal["circle", "square", "triangle", "mixed"]
    count: int = Field(ge=1, le=12)
    instructions: str = Field(min_length=8, max_length=500)
    questions: list[str] = Field(min_length=1, max_length=4)


class FocusTask(StrictModel):
    title: str = Field(min_length=2, max_length=100)
    worksheet: Worksheet
    image_prompt: str = Field(min_length=8, max_length=400)


class Activity(StrictModel):
    title: str = Field(min_length=2, max_length=100)
    materials: list[str] = Field(min_length=1, max_length=8)
    parent_script: str = Field(min_length=8, max_length=700)
    age_facts: list[str] = Field(min_length=2, max_length=5)
    story_prompt: str | None = Field(default=None, max_length=400)


class DailyPlan(StrictModel):
    focus_task: FocusTask
    activity: Activity
    parent_advice: str = Field(min_length=8, max_length=700)


class ParentAdvice(StrictModel):
    text: str = Field(min_length=8, max_length=700)


class RouteUpdate(StrictModel):
    provider: Literal["gemini", "openai", "anthropic", "vertex_ai", "replicate"]
    model: str = Field(min_length=2, max_length=120)
    max_tokens: int = Field(ge=100, le=16000)


class PromptUpdate(StrictModel):
    prompt: str = Field(min_length=20, max_length=12000)


class PromptTestRequest(StrictModel):
    telegram_id: int = Field(gt=0)
    prompt: str | None = Field(default=None, min_length=20, max_length=12000)


class PromptTestResolution(StrictModel):
    status: Literal["accepted", "rejected"]


class SecretUpdate(StrictModel):
    api_key: str = Field(min_length=8, max_length=500)


class PlanUpdate(StrictModel):
    plan: DailyPlan


class BudgetUpdate(StrictModel):
    daily_budget_usd: float = Field(ge=0, le=10000)
    max_tokens: int = Field(ge=100, le=16000)
