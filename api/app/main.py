"""Создание FastAPI-приложения. Контракты — docs/04-спецификация.md §3."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import db, ratelimit
from .config import settings
from .errors import register_handlers

log = logging.getLogger(__name__)


def _validate_kb_index() -> None:
    """Fail-fast при старте: профиль эмбеддера и совместимость с индексом KB.

    Нет опциональных зависимостей (CC_EMBEDDER=bge-m3 без extra `embeddings`)
    или dense-размерность коллекции не та — это ошибка конфигурации: падаем
    сразу с инструкцией, а не 500 на первом запросе к ассистенту (docs/08 §4).
    Недоступный Qdrant ошибкой конфигурации не считаем.
    """
    from .services import kb
    from .services.agent import qdrant_client
    from .services.embeddings import get_embedder

    embedder = get_embedder()
    try:
        kb.check_embedder_compatibility(qdrant_client(), embedder.dense_size)
    except kb.IndexConfigError:
        raise
    except Exception as exc:  # noqa: BLE001 — связь с Qdrant, а не конфиг
        log.warning("Проверка KB-индекса пропущена (Qdrant недоступен): %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if db.engine is None:  # dev-запуск: uvicorn app.main:app
        db.setup(settings.database_url)
        # Alembic/DDL — синхронно и блокирующе: в loop нельзя, поэтому в поток.
        await asyncio.to_thread(db.init_db)
    try:
        # Fail-fast: суммарный пул воркеров и фоновых сервисов влезает в max_connections.
        db.validate_pool_budget()
        # Оффлоадим в поток: сборка эмбеддера (torch) и запрос к Qdrant — не в loop.
        await asyncio.to_thread(_validate_kb_index)
        # Общий счётчик логинов (docs/03 §12): PING Redis на старте фиксирует
        # режим до первого запроса; недоступный Redis — WARNING и memory, не 500.
        await asyncio.to_thread(ratelimit.init_backend)
        # Задачи расшифровки не переживают процесс: строки, застрявшие в
        # `transcribing`, возвращаем в failed (docs/18 §4.3).
        await _reap_stale_sessions()
        # Открытый текст аудио не должен переживать рестарт процесса (§4.6).
        await _purge_artifacts_tmp()
        await _sweep_orphan_artifacts()
        yield
    finally:
        # Закрываем пулы явно: иначе при рестарте/перезагрузке процесса воркеры
        # (uvicorn --reload, тесты) оставляют серверные соединения в Postgres.
        if db.engine is not None:
            await db.engine.dispose()
        if db.sync_engine is not None:
            await asyncio.to_thread(db.sync_engine.dispose)


async def _sweep_orphan_artifacts() -> None:
    """Старт приложения: удалить шифротекст, на который нет строки в БД (§4.6)."""
    from sqlalchemy import select

    from .db import AsyncSessionLocal
    from .models import DocumentArtifact
    from .services import artifacts

    if AsyncSessionLocal is None:
        return
    try:
        async with AsyncSessionLocal() as db:
            known = set(
                (await db.execute(select(DocumentArtifact.path))).scalars().all()
            )
        removed = await asyncio.to_thread(artifacts.sweep_orphans, known)
    except Exception:  # noqa: BLE001 — уборка не должна блокировать старт
        logging.getLogger(__name__).exception("Свип осиротевших артефактов не отработал")
        return
    if removed:
        logging.getLogger(__name__).warning(
            "Удалено %d файлов артефактов без строки в БД", removed
        )


async def _purge_artifacts_tmp() -> None:
    """Старт приложения: убрать осиротевшие расшифрованные файлы из `tmp`."""
    from .services import artifacts

    # Только заведомо брошенные файлы: чужой воркер может прямо сейчас кормить
    # сайдкар из своего (§4.6), поэтому порог — как у реапера расшифровок.
    max_age = settings.session_ai_timeout * 2
    try:
        removed = await asyncio.to_thread(artifacts.purge_temp, max_age_sec=max_age)
    except OSError:  # noqa: BLE001 — уборка не должна блокировать старт
        logging.getLogger(__name__).warning("Не удалось очистить tmp артефактов")
        return
    if removed:
        logging.getLogger(__name__).warning(
            "Удалено %d брошенных файлов расшифровки из tmp", removed
        )


async def _reap_stale_sessions() -> None:
    """Старт приложения: снять «transcribing», оставшиеся от прошлого процесса."""
    from .db import AsyncSessionLocal
    from .services.session_ai import reap_stale

    if AsyncSessionLocal is None:
        return
    try:
        async with AsyncSessionLocal() as db:
            await reap_stale(db)
    except Exception:  # noqa: BLE001 — уборка не должна блокировать старт
        logging.getLogger(__name__).exception("Реапер расшифровок не отработал")


def create_app() -> FastAPI:
    app = FastAPI(title="Clinical Compass API", version="0.1.0", lifespan=lifespan)

    # F6: за nginx — доверяем X-Forwarded-For/X-Real-IP для audit_log.ip.
    from starlette.middleware.gzip import GZipMiddleware
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    register_handlers(app)

    from .routers import admin, adverse, assistant, auth, chat, checkins, clinician, consent, diary, drugs, emias_admin, escalations, kb_admin, notifications, profile, push, screenings, session_ai, session_documents, sessions

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
    app.include_router(adverse.router, prefix="/api/v1")
    app.include_router(push.router, prefix="/api/v1")
    app.include_router(notifications.router, prefix="/api/v1")
    app.include_router(profile.router, prefix="/api/v1")
    app.include_router(sessions.router, prefix="/api/v1")
    app.include_router(session_documents.router, prefix="/api/v1")
    app.include_router(session_ai.router, prefix="/api/v1")

    @app.get("/health")
    def health() -> dict:
        # rate_limit: активное хранилище лимитера логинов (memory|redis) и число
        # отказов Redis — иначе молчаливая деградация общего бюджета невидима.
        return {"status": "ok", "rate_limit": ratelimit.backend_status()}

    return app


app = create_app()
