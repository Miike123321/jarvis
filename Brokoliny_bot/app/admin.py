from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo
from io import BytesIO

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from app.age import apply_current_age
from app.ai import AIOrchestrator
from app.config import Settings
from app.content_library import ContentExhausted, ContentLibrary
from app.database import DEFAULT_ROUTES, Database
from app.image_cache import ImageCache
from app.plans import deliver_plan
from app.printables import make_worksheet
from app.schemas import (
    BudgetUpdate,
    PlanUpdate,
    PromptUpdate,
    RouteUpdate,
    SecretUpdate,
)
from app.security import SecretVault, valid_admin_token

logger = logging.getLogger(__name__)
VALID_PROVIDERS = {"gemini", "openai", "anthropic", "vertex_ai", "replicate"}
BASE_DIR = Path(__file__).resolve().parent.parent


def create_app(
    settings: Settings | None = None,
    database: Database | None = None,
    vault: SecretVault | None = None,
    ai: AIOrchestrator | None = None,
) -> FastAPI:
    config = settings or Settings.from_env()
    db = database or Database(config.database_path)
    db.initialize()
    secret_vault = vault or SecretVault(config.data_encryption_key)
    orchestrator = ai or AIOrchestrator(
        db,
        secret_vault,
        daily_budget_usd=config.daily_budget_usd,
        max_tokens=config.max_tokens,
    )
    content_library = ContentLibrary(db, orchestrator)
    image_cache = ImageCache(db, orchestrator)
    application = FastAPI(title="Brokoliny Admin", docs_url=None, redoc_url=None)

    def attach_admin_identity(record: dict[str, Any]) -> dict[str, Any]:
        encrypted_birth_date = db.encrypted_birth_date(int(record["telegram_id"]))
        record["birth_date"] = None
        if encrypted_birth_date:
            birth_date = date.fromisoformat(
                secret_vault.decrypt(encrypted_birth_date)
            )
            record["birth_date"] = birth_date.isoformat()
            apply_current_age(
                record,
                encrypted_birth_date,
                secret_vault,
                datetime.now(ZoneInfo(config.timezone)).date(),
            )
        record["full_name"] = None
        record["child_name"] = None
        record["phone_number"] = None
        if record.get("identity_consent") != "granted":
            return record
        encrypted = db.admin_identity_encrypted(int(record["telegram_id"]))
        if encrypted is None:
            raise RuntimeError("Granted admin identity data is missing.")
        identity = json.loads(secret_vault.decrypt(encrypted))
        required_fields = {"full_name", "child_name", "phone_number"}
        if not isinstance(identity, dict) or not required_fields.issubset(identity):
            raise RuntimeError("Stored admin identity data is invalid.")
        for field in ("full_name", "child_name", "phone_number"):
            record[field] = identity.get(field)
        return record

    def authorize(authorization: str | None = Header(default=None)) -> None:
        token = authorization.removeprefix("Bearer ").strip() if authorization else None
        if not valid_admin_token(token, config.admin_token):
            raise HTTPException(status_code=401, detail="Invalid or missing admin token.")

    protected = [Depends(authorize)]

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/admin", include_in_schema=False)
    async def admin_page() -> FileResponse:
        return FileResponse(BASE_DIR / "admin.html")

    @application.get("/admin/users", dependencies=protected)
    async def users() -> list[dict[str, Any]]:
        return [attach_admin_identity(user) for user in db.list_users()]

    @application.delete("/admin/users/{telegram_id}", dependencies=protected)
    async def delete_user(telegram_id: int) -> dict[str, Any]:
        if not db.delete_user(telegram_id):
            raise HTTPException(status_code=404, detail="User not found.")
        return {"status": "deleted", "telegram_id": telegram_id}

    @application.get("/admin/users/{telegram_id}", dependencies=protected)
    async def user_detail(telegram_id: int) -> dict[str, Any]:
        profile = db.profile(telegram_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="User profile not found.")
        encrypted = db.encrypted_diagnosis_notes(telegram_id)
        profile["diagnosis_notes"] = secret_vault.decrypt(encrypted) if encrypted else None
        profile["has_diagnosis_notes"] = bool(encrypted)
        profile.pop("diagnosis_notes_encrypted", None)
        profile["identity_consent"] = db.identity_consent_status(telegram_id)
        return attach_admin_identity(profile)

    @application.get("/admin/users/{telegram_id}/plans", dependencies=protected)
    async def user_plans(telegram_id: int) -> list[dict[str, Any]]:
        if db.profile(telegram_id) is None:
            raise HTTPException(status_code=404, detail="User profile not found.")
        return db.list_plans(telegram_id)

    @application.get(
        "/admin/users/{telegram_id}/plans/{plan_date}/worksheet.pdf",
        dependencies=protected,
    )
    async def plan_worksheet(telegram_id: int, plan_date: str) -> StreamingResponse:
        try:
            normalized_date = date.fromisoformat(plan_date).isoformat()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Plan date must use YYYY-MM-DD.") from exc
        if normalized_date != plan_date:
            raise HTTPException(status_code=400, detail="Plan date must use YYYY-MM-DD.")
        plan = db.get_plan(telegram_id, plan_date)
        if plan is None:
            raise HTTPException(status_code=404, detail="Plan not found.")
        pdf = make_worksheet(plan["content"])
        return StreamingResponse(
            BytesIO(pdf),
            media_type="application/pdf",
            headers={"Content-Disposition": f'inline; filename="worksheet-{normalized_date}.pdf"'},
        )

    @application.post(
        "/admin/users/{telegram_id}/plans/{plan_date}/resend",
        dependencies=protected,
    )
    async def resend_plan(telegram_id: int, plan_date: str) -> dict[str, str]:
        try:
            normalized_date = date.fromisoformat(plan_date).isoformat()
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="Plan date must use YYYY-MM-DD."
            ) from exc
        if normalized_date != plan_date:
            raise HTTPException(status_code=400, detail="Plan date must use YYYY-MM-DD.")
        profile = db.profile(telegram_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="User profile not found.")
        if not profile["active"]:
            raise HTTPException(status_code=409, detail="User is not active.")
        plan = db.get_plan(telegram_id, plan_date)
        if plan is None:
            raise HTTPException(status_code=404, detail="Plan not found.")
        if plan["status"] != "sent":
            raise HTTPException(
                status_code=409, detail="Only a previously sent plan can be resent."
            )
        if not config.telegram_token:
            raise HTTPException(
                status_code=503, detail="Telegram delivery is not configured."
            )

        bot = Bot(token=config.telegram_token)
        try:
            await deliver_plan(
                bot,
                telegram_id,
                plan["content"],
                plan_date,
                db,
                image_cache,
                str(profile.get("language", "ru")),
                force_resend=True,
            )
        except TelegramAPIError as exc:
            logger.error(
                "Manual plan resend failed for user %s on %s with %s.",
                telegram_id,
                plan_date,
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=502, detail="Telegram could not deliver the plan."
            ) from exc
        finally:
            await bot.session.close()
        return {"status": "resent", "plan_date": plan_date}

    @application.put("/admin/users/{telegram_id}/plans/tomorrow", dependencies=protected)
    async def update_tomorrow_plan(telegram_id: int, body: PlanUpdate) -> dict[str, str]:
        if db.profile(telegram_id) is None:
            raise HTTPException(status_code=404, detail="User profile not found.")
        plan_date = (datetime.now(ZoneInfo(config.timezone)).date() + timedelta(days=1)).isoformat()
        plan = body.plan.model_dump()
        repeated = content_library.repeated_plan_components(
            telegram_id, plan, excluding_plan_date=plan_date
        )
        if repeated:
            raise HTTPException(
                status_code=409,
                detail=f"Plan repeats previously used content: {', '.join(repeated)}.",
            )
        db.save_plan(telegram_id, plan_date, plan, status="pending")
        return {"status": "saved", "plan_date": plan_date}

    @application.post(
        "/admin/users/{telegram_id}/plans/tomorrow/regenerate",
        dependencies=protected,
    )
    async def regenerate_tomorrow_plan(telegram_id: int) -> dict[str, Any]:
        profile = db.profile(telegram_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="User profile not found.")
        plan_date = (datetime.now(ZoneInfo(config.timezone)).date() + timedelta(days=1)).isoformat()
        apply_current_age(
            profile,
            db.encrypted_birth_date(telegram_id),
            secret_vault,
            date.fromisoformat(plan_date),
        )
        try:
            plan = await content_library.daily_plan(
                telegram_id,
                profile,
                db.recent_feedback(telegram_id),
                plan_date,
            )
        except ContentExhausted as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        db.save_plan(telegram_id, plan_date, plan, status="pending")
        return {"status": "generated", "plan_date": plan_date, "plan": plan}

    @application.get("/admin/settings/routes", dependencies=protected)
    async def routes() -> dict[str, Any]:
        return {task: db.get_route(task) for task in DEFAULT_ROUTES}

    @application.put("/admin/settings/routes/{task}", dependencies=protected)
    async def update_route(task: str, body: RouteUpdate) -> dict[str, Any]:
        if task not in DEFAULT_ROUTES:
            raise HTTPException(status_code=404, detail="Unknown AI task.")
        route = body.model_dump()
        db.set_route(task, route)
        return route

    @application.get("/admin/settings/prompts", dependencies=protected)
    async def prompts() -> list[dict[str, Any]]:
        return db.list_prompts()

    @application.put("/admin/settings/prompts/{name}", dependencies=protected)
    async def update_prompt(name: str, body: PromptUpdate) -> dict[str, Any]:
        try:
            version = db.update_prompt(name, body.prompt)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"name": name, "version": version}

    @application.get(
        "/admin/settings/prompts/{name}/history",
        dependencies=protected,
    )
    async def prompt_history(name: str) -> list[dict[str, Any]]:
        if name not in {item["name"] for item in db.list_prompts()}:
            raise HTTPException(status_code=404, detail="Unknown system prompt.")
        return db.prompt_history(name)

    @application.post("/admin/settings/secrets/{provider}", dependencies=protected)
    async def update_secret(provider: str, body: SecretUpdate) -> dict[str, str]:
        if provider not in VALID_PROVIDERS:
            raise HTTPException(status_code=404, detail="Unknown provider.")
        db.store_secret(provider, secret_vault.encrypt(body.api_key))
        return {"provider": provider, "status": "encrypted_and_saved"}

    @application.get("/admin/settings/budget", dependencies=protected)
    async def budget() -> dict[str, Any]:
        return {
            "daily_budget_usd": db.get_setting("daily_budget_usd", config.daily_budget_usd),
            "max_tokens": db.get_setting("max_tokens", config.max_tokens),
            "spent_today_usd": db.daily_ai_cost(),
        }

    @application.put("/admin/settings/budget", dependencies=protected)
    async def update_budget(body: BudgetUpdate) -> dict[str, Any]:
        db.set_setting("daily_budget_usd", body.daily_budget_usd)
        db.set_setting("max_tokens", body.max_tokens)
        return await budget()

    @application.get("/admin/templates", dependencies=protected)
    async def vector_templates() -> list[dict[str, str]]:
        return [
            {"id": "shapes", "name": "Фигуры", "kind": "static_vector"},
            {"id": "counting", "name": "Счёт", "kind": "static_vector"},
            {"id": "pattern", "name": "Последовательности", "kind": "static_vector"},
            {"id": "animals", "name": "Животные", "kind": "static_vector"},
        ]

    @application.get("/admin/images", dependencies=protected)
    async def images() -> list[dict[str, Any]]:
        return db.list_images()

    @application.get("/admin/content-cache", dependencies=protected)
    async def content_cache(telegram_id: int) -> list[dict[str, Any]]:
        if db.profile(telegram_id) is None:
            raise HTTPException(status_code=404, detail="User profile not found.")
        return db.user_content_library(telegram_id)

    @application.delete(
        "/admin/content-cache/{content_hash}", dependencies=protected
    )
    async def delete_cached_content(content_hash: str) -> dict[str, str]:
        if not db.delete_cached_content(content_hash):
            raise HTTPException(status_code=404, detail="Cached content not found.")
        return {"status": "deleted"}

    @application.delete("/admin/images/{tag_hash}", dependencies=protected)
    async def delete_image(tag_hash: str) -> dict[str, str]:
        if not db.delete_image(tag_hash):
            raise HTTPException(status_code=404, detail="Cached image not found.")
        return {"status": "deleted"}

    return application


app = create_app()
