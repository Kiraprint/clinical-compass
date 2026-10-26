"""FR-39 (docs/18 §4.3, PR P3): ИИ-ассистент сессии — HTTP-слой.

Только разбор запроса и коды ответов: roster/согласие/IDOR, шифрование,
деперсонализация и аудит живут в `app.services.session_ai`. Ручной протокол,
подпись и передача в ЕМИАС — `routers/sessions.py`, здесь их нет.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app import services
from app.db import get_db
from app.deps import require_clinician
from app.models import User

router = APIRouter(tags=["sessions"])


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.post("/clinician/sessions/{session_id}/audio", status_code=201)
async def upload_audio(
    session_id: uuid.UUID,
    request: Request,
    file: UploadFile = File(...),
    user: User = Depends(require_clinician),
    db: AsyncSession = Depends(get_db),
):
    """Загрузка записи приёма: 201 — артефакт; 422 без согласия, 413/415 — формат."""
    return await services.session_ai.upload_audio(
        db, user.id, session_id, file, ip=_ip(request)
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


@router.post("/clinician/sessions/{session_id}/protocol/draft", status_code=201)
async def draft_protocol(
    session_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_clinician),
    db: AsyncSession = Depends(get_db),
):
    """Черновик протокола ИИ: 201 Protocol (`source='llm'`, `needs_review`)."""
    return await services.session_ai.draft(db, user.id, session_id, ip=_ip(request))
