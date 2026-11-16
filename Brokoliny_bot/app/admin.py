from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, AsyncIterator
from zoneinfo import ZoneInfo
from io import BytesIO

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import httpx
from litellm.exceptions import APIError
from PIL import Image

from app.age import apply_current_age
from app.ai import AIBudgetExceeded, AIOrchestrator
from app.config import Settings
from app.content_library import ContentExhausted, ContentLibrary
from app.cloudflare_access import (
    CloudflareAccessMiddleware,
    CloudflareAccessVerifier,
)
from app.database import DEFAULT_ROUTES, Database
from app.image_cache import ImageCache
from app.plans import deliver_plan, get_skill_gaps
from app.prompt_templates import PROMPT_TEMPLATE_FIELDS, validate_prompt_template
from app.printables import make_worksheet
from app.schemas import (
    BudgetUpdate,
    PlanUpdate,
    PromptUpdate,
    PromptTestRequest,
    PromptTestResolution,
    RouteUpdate,
    SecretUpdate,
)
from app.security import SecretVault, valid_admin_token

logger = logging.getLogger(__name__)
VALID_PROVIDERS = {"gemini", "openai", "anthropic", "vertex_ai", "replicate"}
BASE_DIR = Path(__file__).resolve().parent.parent
ADMIN_CSS = Path(__file__).resolve().parent / "static" / "admin.css"


def _image_media_type(image_bytes: bytes) -> str:
    with Image.open(BytesIO(image_bytes)) as image:
        return Image.MIME.get(image.format or "", "application/octet-stream")


def create_app(
    settings: Settings | None = None,
    database: Database | None = None,
    vault: SecretVault | None = None,
    ai: AIOrchestrator | None = None,
    access_verifier: CloudflareAccessVerifier | None = None,
) -> FastAPI:
    config = settings or Settings.from_env()
    # Validate remote trust configuration before opening SQLite or constructing
    # any service whose initialization can have local side effects.
    config.validate_remote_access()
    verifier = access_verifier
    if config.remote_mode and verifier is None:
        verifier = CloudflareAccessVerifier(
            team=config.cloudflare_access_team,
            issuer=config.cloudflare_access_issuer,
            audience=config.cloudflare_access_audience,
            allowed_emails=config.cloudflare_access_admin_emails,
            clock_skew_seconds=config.cloudflare_access_clock_skew_seconds,
            cache_ttl_seconds=config.cloudflare_access_jwks_cache_seconds,
        )
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
    retention_scheduler = AsyncIOScheduler(timezone=config.timezone)
    retention_scheduler.add_job(
        db.purge_ai_request_logs,
        "interval",
        minutes=1,
        id="purge-ai-request-logs",
        max_instances=1,
        coalesce=True,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if config.remote_mode:
            # Do not accept remote traffic until a trusted key set was fetched
            # successfully over the pinned HTTPS endpoint.
            assert verifier is not None
            await asyncio.to_thread(verifier.ensure_ready)
        retention_scheduler.start()
        try:
            yield
        finally:
            if retention_scheduler.running:
                retention_scheduler.shutdown(wait=False)

    application = FastAPI(
        title="Brokoliny Admin",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    if config.remote_mode:
        assert verifier is not None
        application.add_middleware(
            CloudflareAccessMiddleware,
            verifier=verifier,
        )

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

    @application.get("/static/admin.css", include_in_schema=False)
    async def admin_css() -> FileResponse:
        if not ADMIN_CSS.is_file():
            raise HTTPException(status_code=404, detail="Admin stylesheet not found.")
        return FileResponse(ADMIN_CSS, media_type="text/css")

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
        return [
            {
                **prompt,
                "placeholders": sorted(
                    PROMPT_TEMPLATE_FIELDS.get(prompt["name"], set())
                ),
            }
            for prompt in db.list_prompts()
        ]

    @application.put("/admin/settings/prompts/{name}", dependencies=protected)
    async def update_prompt(name: str, body: PromptUpdate) -> dict[str, Any]:
        try:
            validate_prompt_template(body.prompt, name)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            version = db.update_prompt(name, body.prompt)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            "name": name,
            "version": version,
            "placeholders": sorted(PROMPT_TEMPLATE_FIELDS.get(name, set())),
        }

    @application.get(
        "/admin/settings/prompts/{name}/history",
        dependencies=protected,
    )
    async def prompt_history(name: str) -> list[dict[str, Any]]:
        if name not in {item["name"] for item in db.list_prompts()}:
            raise HTTPException(status_code=404, detail="Unknown system prompt.")
        return db.prompt_history(name)

    @application.post(
        "/admin/prompt-tests/{component}", dependencies=protected
    )
    async def generate_prompt_test(
        component: str, body: PromptTestRequest
    ) -> dict[str, Any]:
        if component not in {"diagnostics", "focus_task", "activity", "advice"}:
            raise HTTPException(status_code=404, detail="Unknown plan component.")
        profile = db.profile(body.telegram_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="User profile not found.")
        apply_current_age(
            profile,
            db.encrypted_birth_date(body.telegram_id),
            secret_vault,
            datetime.now(ZoneInfo(config.timezone)).date(),
        )
        gaps = get_skill_gaps(profile)
        feedback = db.recent_feedback(body.telegram_id)
        avoid_recent = (
            content_library.recent_component_descriptions(body.telegram_id)
            if component != "diagnostics"
            else {}
        )
        request_traces: list[dict[str, object]] = []
        excluded_skill_ids, excluded_questions = (
            content_library.assessment_exclusions(body.telegram_id)
        )

        def decrypted_traces() -> list[dict[str, Any]]:
            result = []
            for log in db.ai_request_logs_by_ids(
                [int(item["id"]) for item in request_traces]
            ):
                request = json.loads(
                    secret_vault.decrypt(log.pop("request_encrypted"))
                )
                encrypted_response = log.pop("response_encrypted")
                response = (
                    json.loads(secret_vault.decrypt(encrypted_response))
                    if encrypted_response
                    else None
                )
                result.append(
                    {
                        **log,
                        "request": request,
                        "response": response,
                    }
                )
            return result

        try:
            if component == "diagnostics":
                generated = await orchestrator.generate_assessment(
                    int(profile["age_months"]),
                    language=str(profile["language"]),
                    excluded_skill_ids=excluded_skill_ids,
                    excluded_questions=excluded_questions,
                    child_context={
                        "gender": profile.get("gender"),
                        "interests": profile.get("interests"),
                        "skills": profile.get("skills", []),
                        "skill_gaps": gaps,
                        "diagnostic_results": profile.get("skills", []),
                    },
                    telegram_id=body.telegram_id,
                    prompt_override=body.prompt,
                    request_traces=request_traces,
                )
                content = generated.model_dump()
                variant_id = None
                image_tag_hash = None
                image_source = None
            else:
                generated = await orchestrator.generate_plan_component(
                    component,
                    profile,
                    gaps,
                    feedback,
                    avoid_recent=avoid_recent,
                    telegram_id=body.telegram_id,
                    prompt_override=body.prompt,
                    request_traces=request_traces,
                )
                content = generated.model_dump()
                image_tag_hash = None
                image_source = None
                image_trace_count = len(request_traces)
                if component == "focus_task":
                    _, _, image_tag_hash = await image_cache.get_or_generate(
                        ImageCache.task_image_tag(content["image_prompt"]),
                        content["image_prompt"],
                        body.telegram_id,
                        request_traces=request_traces,
                    )
                    image_source = (
                        "generated"
                        if len(request_traces) > image_trace_count
                        else "cache"
                    )
                variant_id = db.create_prompt_test_variant(
                    body.telegram_id, component, content, image_tag_hash
                )
        except AIBudgetExceeded as exc:
            raise HTTPException(
                status_code=429,
                detail={"message": str(exc), "trace": decrypted_traces()},
            ) from exc
        except ContentExhausted as exc:
            raise HTTPException(
                status_code=409,
                detail={"message": str(exc), "trace": decrypted_traces()},
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail={"message": str(exc), "trace": decrypted_traces()},
            ) from exc
        except (APIError, httpx.HTTPError) as exc:
            raise HTTPException(
                status_code=502,
                detail={
                    "message": f"AI request failed: {type(exc).__name__}",
                    "trace": decrypted_traces(),
                },
            ) from exc
        return {
            "id": variant_id,
            "telegram_id": body.telegram_id,
            "component": component,
            "content": content,
            "image_source": image_source,
            "trace": decrypted_traces(),
            "image_url": (
                f"/admin/prompt-tests/variants/{variant_id}/image"
                if image_tag_hash and variant_id is not None
                else None
            ),
            "status": "pending" if variant_id is not None else "preview",
        }

    @application.get(
        "/admin/prompt-tests/context/{telegram_id}", dependencies=protected
    )
    async def prompt_test_context(telegram_id: int) -> dict[str, Any]:
        profile = db.profile(telegram_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="User profile not found.")
        apply_current_age(
            profile,
            db.encrypted_birth_date(telegram_id),
            secret_vault,
            datetime.now(ZoneInfo(config.timezone)).date(),
        )
        gaps = get_skill_gaps(profile)
        skills = profile.get("skills", [])
        recent_components = content_library.recent_component_descriptions(
            telegram_id
        )
        excluded_skill_ids, excluded_questions = (
            content_library.assessment_exclusions(telegram_id)
        )
        return {
            "age_months": profile.get("age_months"),
            "gender": profile.get("gender"),
            "language": profile.get("language"),
            "interests": profile.get("interests") or None,
            "skills": skills,
            "skill_gaps": gaps,
            "diagnostic_results": skills,
            "excluded_skill_ids": excluded_skill_ids,
            "excluded_questions": excluded_questions,
            "previous_difficulty": db.recent_feedback(telegram_id),
            "recent_components": recent_components,
            "privacy_note": (
                "Диагностические заметки и контакты не передаются в тестовый запрос."
            ),
        }

    @application.get(
        "/admin/prompt-tests/variants/{variant_id}/image",
        dependencies=protected,
    )
    async def prompt_test_image(variant_id: int) -> StreamingResponse:
        variant = db.prompt_test_variant(variant_id)
        if variant is None or not variant["image_tag_hash"]:
            raise HTTPException(status_code=404, detail="Variant image not found.")
        cached = db.get_image(variant["image_tag_hash"])
        if cached is None:
            raise HTTPException(status_code=404, detail="Variant image not found.")
        return StreamingResponse(
            BytesIO(cached["image_bytes"]),
            media_type=_image_media_type(cached["image_bytes"]),
            headers={"Cache-Control": "no-store"},
        )

    @application.post(
        "/admin/prompt-tests/variants/{variant_id}/resolve",
        dependencies=protected,
    )
    async def resolve_prompt_test(
        variant_id: int, body: PromptTestResolution
    ) -> dict[str, Any]:
        variant = db.prompt_test_variant(variant_id)
        if variant is None:
            raise HTTPException(status_code=404, detail="Prompt test variant not found.")
        if variant["status"] != "pending":
            raise HTTPException(status_code=409, detail="Variant is already resolved.")
        if body.status == "accepted":
            profile = db.profile(int(variant["telegram_id"]))
            if profile is None:
                raise HTTPException(status_code=404, detail="User profile not found.")
            apply_current_age(
                profile,
                db.encrypted_birth_date(int(variant["telegram_id"])),
                secret_vault,
                datetime.now(ZoneInfo(config.timezone)).date(),
            )
            if (
                variant["component"] == "focus_task"
                and (
                    not variant["image_tag_hash"]
                    or db.get_image(variant["image_tag_hash"]) is None
                )
            ):
                raise HTTPException(
                    status_code=409,
                    detail="The focus-task image is no longer available.",
                )
            try:
                content_library.cache_personal_variant(
                    int(variant["telegram_id"]),
                    profile,
                    str(variant["component"]),
                    variant["content"],
                )
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not db.resolve_prompt_test_variant(variant_id, body.status):
            raise HTTPException(status_code=409, detail="Variant is already resolved.")
        return {"id": variant_id, "status": body.status}

    @application.get("/admin/ai-logs", dependencies=protected)
    async def ai_logs(
        limit: int = Query(default=100, ge=1, le=500),
        telegram_id: int | None = Query(default=None, gt=0),
    ) -> list[dict[str, Any]]:
        logs = db.list_ai_request_logs(limit=limit, telegram_id=telegram_id)
        for log in logs:
            log["request"] = json.loads(
                secret_vault.decrypt(log.pop("request_encrypted"))
            )
            encrypted_response = log.pop("response_encrypted")
            log["response"] = (
                json.loads(secret_vault.decrypt(encrypted_response))
                if encrypted_response
                else None
            )
        return logs

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

    @application.get(
        "/admin/images/{tag_hash}/file", dependencies=protected
    )
    async def cached_image_file(tag_hash: str) -> StreamingResponse:
        image = db.get_image(tag_hash)
        if image is None:
            raise HTTPException(status_code=404, detail="Cached image not found.")
        return StreamingResponse(
            BytesIO(image["image_bytes"]),
            media_type=_image_media_type(image["image_bytes"]),
            headers={"Cache-Control": "no-store"},
        )

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
