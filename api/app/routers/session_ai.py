"""FR-39 (docs/18 §4.3, PR P3): ИИ-ассистент сессии — HTTP-слой.

Только разбор запроса и коды ответов: roster/согласие/IDOR, шифрование,
деперсонализация и аудит живут в `app.services.session_ai`. Ручной протокол,
подпись и передача в ЕМИАС — `routers/sessions.py`, здесь их нет.
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app import services
from app.db import get_db
from app.deps import require_clinician
from app.models import User

router = APIRouter(tags=["sessions"])

# FR-42 (docs/18 §4.3): роли участников. `Literal` — чтобы мусор в теле давал
# 422 VALIDATION до обращения к БД; `unknown` нужен, когда врач не уверен.
SpeakerRole = Literal["clinician", "patient", "unknown"]


class SpeakerRolesBody(BaseModel):
    """`{"roles": {"A": "clinician", "B": "patient"}}` — метка кластера → роль."""

    roles: dict[str, SpeakerRole] = Field(min_length=1, max_length=2)

def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.post("/clinician/sessions/{session_id}/audio", status_code=201)
async def upload_audio(
    session_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_clinician),
    db: AsyncSession = Depends(get_db),
):
    """Загрузка записи приёма: 201 — артефакт; 422 без согласия, 413/415 — формат.

    Тело не объявляется как `UploadFile`: Starlette спулил бы загрузку >1 МиБ
    открытым текстом в /tmp. Сервис читает `request.stream()` сам (§4.6).
    """
    return await services.session_ai.upload_audio(
        db, user.id, session_id, request, ip=_ip(request)
    )


@router.post("/clinician/sessions/{session_id}/transcribe", status_code=202)
async def transcribe(
    session_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_clinician),
    db: AsyncSession = Depends(get_db),
):
    """Запуск расшифровки в фоне: 202 `{ai_status}`; 409 AUDIO_MISSING / AI_BUSY."""
    return await services.session_ai.transcribe(db, user.id, session_id, ip=_ip(request))


@router.get("/clinician/sessions/{session_id}/transcript")
async def transcript(
    session_id: uuid.UUID,
    user: User = Depends(require_clinician),
    db: AsyncSession = Depends(get_db),
):
    """Расшифровка (уже без ПДн) + метаданные; 404 TRANSCRIPT_MISSING."""
    return await services.session_ai.transcript(db, user.id, session_id)


@router.patch("/clinician/sessions/{session_id}/transcript/speakers")
async def set_speaker_roles(
    session_id: uuid.UUID,
    body: SpeakerRolesBody,
    request: Request,
    user: User = Depends(require_clinician),
    db: AsyncSession = Depends(get_db),
):
    """Роли реплик: 200 — расшифровка с ролями; 422 дубли/чужие метки; 404/403.

    PATCH, а не PUT: врач меняет только разметку участников, текст расшифровки
    и метрики маскирования остаются прежними. После вызова
    `speaker_roles_assumed=false` — роли подтверждены человеком.
    """
    return await services.session_ai.set_speaker_roles(
        db, user.id, session_id, dict(body.roles), ip=_ip(request)
    )


@router.post("/clinician/sessions/{session_id}/protocol/draft", status_code=201)
async def draft_protocol(
    session_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_clinician),
    db: AsyncSession = Depends(get_db),
):
    """Черновик протокола ИИ: 201 Protocol (`source='llm'`, `needs_review`)."""
    return await services.session_ai.draft(db, user.id, session_id, ip=_ip(request))


# FR-44 (docs/18 §10): диалог по сессии. Верхняя граница длины — общий потолок
# pydantic (8000), фактический лимит — CC_SESSION_CHAT_MESSAGE_CHARS в сервисе
# (422 MESSAGE_TOO_LONG), чтобы его можно было менять без рестарта схемы.
class ChatBody(BaseModel):
    """`{"message": "..."}` — вопрос врача агенту по этому приёму."""

    message: str = Field(min_length=1, max_length=8000)


@router.post("/clinician/sessions/{session_id}/chat")
async def session_chat(
    session_id: uuid.UUID,
    body: ChatBody,
    request: Request,
    user: User = Depends(require_clinician),
    db: AsyncSession = Depends(get_db),
):
    """Вопрос агенту по приёму: ответ с цитатами `[n]` и `proposal` без мутаций.

    503 SESSION_AI_DISABLED / MODEL_NOT_CONFIGURED, 404 чужая сессия,
    403 NO_ROSTER / CONSENT_REQUIRED, 422 MESSAGE_TOO_LONG,
    409 CHAT_LIMIT_REACHED. Завершение приёма чат не блокирует.
    """
    return await services.session_agent.ask(
        db, user.id, session_id, body.message, ip=_ip(request)
    )


@router.get("/clinician/sessions/{session_id}/chat")
async def session_chat_history(
    session_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_clinician),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
):
    """История диалога по сессии (старые → новые); текст только врачу, не в аудит."""
    return await services.session_agent.history(db, user.id, session_id, limit=limit)

