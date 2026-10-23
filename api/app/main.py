"""Создание FastAPI-приложения. Контракты — docs/04-спецификация.md §3."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import db
from .config import settings
from .errors import register_handlers


@asynccontextmanager
async def lifespan(app: FastAPI):
    if db.engine is None:  # dev-запуск: uvicorn app.main:app
        db.setup(settings.database_url)
        db.init_db()
    # Fail-fast: суммарный пул всех воркеров должен влезать в max_connections.
    db.validate_pool_budget()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Clinical Compass API", version="0.1.0", lifespan=lifespan)

    # F6: за nginx — доверяем X-Forwarded-For/X-Real-IP для audit_log.ip.
    from starlette.middleware.gzip import GZipMiddleware
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    register_handlers(app)

    from .routers import admin, assistant, auth, chat, checkins, clinician, consent, diary, drugs, emias_admin, escalations, kb_admin, notifications, profile, push, screenings

    app.include_router(auth.router, prefix="/api/v1")
    app.include_router(assistant.router, prefix="/api/v1")
    app.include_router(drugs.router, prefix="/api/v1")
    app.include_router(consent.router, prefix="/api/v1")
    app.include_router(admin.router, prefix="/api/v1")
    app.include_router(kb_admin.router, prefix="/api/v1/admin")
    app.include_router(emias_admin.router, prefix="/api/v1/admin")
    app.include_router(screenings.router, prefix="/api/v1")
    app.include_router(checkins.router, prefix="/api/v1")
    app.include_router(diary.router, prefix="/api/v1")
    app.include_router(escalations.router, prefix="/api/v1")
    app.include_router(chat.router, prefix="/api/v1")
    app.include_router(clinician.router, prefix="/api/v1")
    app.include_router(clinician.me_router, prefix="/api/v1")
    app.include_router(push.router, prefix="/api/v1")
    app.include_router(notifications.router, prefix="/api/v1")
    app.include_router(profile.router, prefix="/api/v1")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    return app


app = create_app()