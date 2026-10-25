# ИИ-агент врача: гибридный поиск по базе знаний + ответ с цитатами.
# FR-21: сессии (треды) ассистента с persistent-контекстом и мультипациентом.
# Контракт: docs/04 §3.1 (расширение), docs/07, docs/08; тесты — docs/06 §3.
from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app import services
from app.config import settings
from app.db import get_db
from app.deps import require_clinician
from app.errors import Problem
from app.enrichment import review as kb_review
from app.models import User
from app.services import assistant_chat

router = APIRouter(tags=["assistant"])

class AskBody(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    topic: str | None = Field(default=None, max_length=40)  # ручной фильтр: ШКАЛЫ|ТРИАЖ|РЕКОМЕНДАЦИИ|ЛЕКАРСТВА (иначе LLM-роутер)
    limit: int = Field(default=5, ge=1, le=20)
    patient_id: uuid.UUID | None = Field(default=None)  # @-упоминание пациента → контекст в агент

@router.post("/assistant/ask")
async def ask(
    body: AskBody,
    request: Request,
    user: User = Depends(require_clinician),
    db: AsyncSession = Depends(get_db),
):
    """Справочный ответ врачу по базе знаний. Только CLINICIAN (roster не нужен:
    корпус — открытый не-ПДн контент).

    При patient_id (функция «@пациент»): загружается компактный контекст пациента
    (services.clinician.patient_context — roster-gate + согласие + audit
    ASK_ASSISTANT_WITH_PATIENT, спецкатегория ПДн 152-ФЗ) и подмешивается в ответ
    как опора; в Qdrant не попадает. Запрос без пациента аудируется ASK_ASSISTANT.
    Общий RAG-путь с тредами — assistant_chat.answer_query (FR-21).
    """
    ip = request.client.host if request.client else None
    patient_ids = [body.patient_id] if body.patient_id is not None else []
    result = await assistant_chat.answer_query(
        db,
        user.id,
        body.query,
        topic=body.topic,
        limit=body.limit,
        patient_ids=patient_ids,
        ip=ip,
    )
    if not patient_ids:
        await services.audit.record(db, user.id, "ASK_ASSISTANT", "kb", object_id=None, ip=ip)
        await db.commit()
    return result

@router.get("/assistant/status")
def status(user: User = Depends(require_clinician)):
    """Состояние базы знаний: число проиндексированных чанков."""
    return {
        "collection": settings.qdrant_collection,
        "points": services.kb.count_points(services.agent.qdrant_client()),
    }

# --- FR-21: сессии ассистента (треды) --------------------------------------

class ThreadBody(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    patient_ids: list[uuid.UUID] | None = Field(default=None)

class ThreadPatchBody(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    patient_ids: list[uuid.UUID] | None = Field(default=None)

class ThreadMessageBody(BaseModel):
    content: str = Field(min_length=1, max_length=2000)
    patient_ids: list[uuid.UUID] | None = Field(default=None)

def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None

@router.post("/assistant/threads", status_code=201)
async def create_thread(
    body: ThreadBody,
    user: User = Depends(require_clinician),
    db: AsyncSession = Depends(get_db),
):
    """Новый диалог: title + область пациентов (roster-гейт, ≤10)."""
    return await assistant_chat.create_thread(db, user.id, body.title, body.patient_ids)

@router.get("/assistant/threads")
async def list_threads(user: User = Depends(require_clinician), db: AsyncSession = Depends(get_db)):
    """Свои диалоги: id, title, patient_ids, updated_at, message_count."""
    return {"items": await assistant_chat.list_threads(db, user.id)}

@router.get("/assistant/threads/{thread_id}")
async def get_thread(
    thread_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_clinician),
    db: AsyncSession = Depends(get_db),
):
    """Тред с историей сообщений и цитатами. Чужой → 404 THREAD_NOT_FOUND."""
    return await assistant_chat.get_thread_detail(db, user.id, thread_id, ip=_ip(request))

@router.patch("/assistant/threads/{thread_id}")
async def update_thread(
    thread_id: uuid.UUID,
    body: ThreadPatchBody,
    user: User = Depends(require_clinician),
    db: AsyncSession = Depends(get_db),
):
    """Переименование и/или замена области пациентов треда."""
    return await assistant_chat.update_thread(
        db, user.id, thread_id, title=body.title, patient_ids=body.patient_ids
    )

@router.delete("/assistant/threads/{thread_id}", status_code=204)
async def delete_thread(
    thread_id: uuid.UUID,
    user: User = Depends(require_clinician),
    db: AsyncSession = Depends(get_db),
):
    """Удаление треда; сообщения и привязки удаляются каскадом."""
    await assistant_chat.delete_thread(db, user.id, thread_id)
    return Response(status_code=204)

@router.post("/assistant/threads/{thread_id}/messages")
async def post_message(
    thread_id: uuid.UUID,
    body: ThreadMessageBody,
    request: Request,
    user: User = Depends(require_clinician),
    db: AsyncSession = Depends(get_db),
):
    """Вопрос врача в тред → ответ ассистента; история и карты пациентов
    учитываются, оба сообщения персистируются."""
    return await assistant_chat.post_message(
        db,
        user.id,
        thread_id,
        body.content,
        patient_ids=body.patient_ids,
        ip=_ip(request),
    )

# --- КБ: персональные исключения источников (docs/15 §6) -------------------

class SourceExclusionBody(BaseModel):
    excluded: bool = True

@router.get("/assistant/sources")
async def sources(user: User = Depends(require_clinician), db: AsyncSession = Depends(get_db)):
    """Опубликованные источники КБ + персональные исключения врача (docs/15 §6)."""
    works = await kb_review.list_works(db, status="approved", limit=200)
    excluded = await kb_review.excluded_ids(db, user.id)
    return {
        "sources": [
            kb_review.serialize(work, excluded=work.guideline_id in excluded) for work in works
        ],
        "excluded_guideline_ids": sorted(excluded),
    }

@router.post("/assistant/sources/{guideline_id}/exclude")
async def exclude_source(
    guideline_id: str,
    body: SourceExclusionBody,
    user: User = Depends(require_clinician),
    db: AsyncSession = Depends(get_db),
):
    """Скрыть/вернуть источник в выдаче этого врача.

    Источник не удаляется из общей КБ: решение персональное и обратимое.
    """
    if not guideline_id or len(guideline_id) > 96:
        raise Problem(400, "BAD_GUIDELINE_ID", "Некорректный идентификатор источника.")
    work = await kb_review.get_work_by_guideline(db, guideline_id)
    if work is None or work.status != "approved":
        raise Problem(404, "SOURCE_NOT_FOUND", "Источник не найден в КБ.")
    excluded = await kb_review.set_exclusion(db, user.id, guideline_id, body.excluded)
    await db.commit()
    return {"excluded_guideline_ids": excluded}


# --- FR-36: персональное ревью статей врачом («Библиотека») -----------------

class ClinicianDecisionBody(BaseModel):
    decision: Literal["approved", "rejected"]
    # Свободный текст не хранится и не аудируется (152-ФЗ): параметр в контракте
    # для UI, сервер проверяет длину и отбрасывает.
    note: str | None = Field(default=None, max_length=500)


@router.get("/clinician/kb/works")
async def clinician_kb_works(
    status: Literal["new", "approved", "rejected", "all"] = "new",
    kind: Literal["article", "guideline"] | None = None,
    limit: int = 100,
    user: User = Depends(require_clinician),
    db: AsyncSession = Depends(get_db),
):
    """Опубликованные работы КБ + моё персональное решение (docs/04 FR-36).

    `new` — опубликовано админом, но я ещё не решил; `approved`/`rejected` —
    мои решения; `all` — все опубликованные. Только своя область: clinician_id
    всегда из токена.
    """
    rows = await kb_review.list_clinician_works(
        db,
        user.id,
        status=status,
        kind=kind,
        limit=min(max(limit, 1), 200),
    )
    return {
        "works": [
            kb_review.serialize_for_clinician(work, approval) for work, approval in rows
        ]
    }


_DECISION_ERROR_STATUS = {"KB_WORK_NOT_FOUND": 404, "KB_WORK_NOT_PUBLISHED": 409}


@router.post("/clinician/kb/works/{work_id}/decision")
async def clinician_kb_decision(
    work_id: uuid.UUID,
    body: ClinicianDecisionBody,
    user: User = Depends(require_clinician),
    db: AsyncSession = Depends(get_db),
):
    """Одобрить/отклонить статью для себя: видимость в моих ответах ассистента.

    Публикация в индекс (админский approve) не меняется. Повтор того же решения
    идемпотентен: 200, reviewed_at не меняется.
    """
    try:
        work, approval = await kb_review.set_decision(
            db, user.id, work_id, body.decision, note=body.note
        )
    except kb_review.ApprovalError as exc:
        raise Problem(_DECISION_ERROR_STATUS.get(exc.code, 400), exc.code, str(exc)) from exc
    await db.commit()
    return kb_review.serialize_for_clinician(work, approval)
