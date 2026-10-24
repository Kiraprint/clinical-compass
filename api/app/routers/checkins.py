# E4: чек-ин. Контракт: docs/16 §4; FR-6 — docs/04 §3.1.
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import require_consent
from app.models import User
from app.services import checkin as checkin_service

router = APIRouter(tags=["checkins"])


class AnswersBody(BaseModel):
    # Значения валидируются в сервисе: bool и не-целые должны давать
    # CHECKIN_OUT_OF_RANGE, а не VALIDATION от pydantic.
    answers: dict[str, Any]


@router.get("/checkins/today")
async def today(user: User = Depends(require_consent), db: AsyncSession = Depends(get_db)):
    return await checkin_service.today(db, user.id)


@router.get("/checkins/history")
async def history(
    days: int = Query(default=90, ge=1, le=365),
    user: User = Depends(require_consent),
    db: AsyncSession = Depends(get_db),
):
    """Тренд чек-инов: агрегат по завершённым сеансам (FR-33), без сырых ответов."""
    return {"items": await checkin_service.history(db, user.id, days)}


@router.post("/checkins")
async def start(
    response: Response,
    user: User = Depends(require_consent),
    db: AsyncSession = Depends(get_db),
):
    body, created = await checkin_service.start(db, user.id)
    response.status_code = 201 if created else 200
    return body


@router.post("/checkins/{session_id}/answers")
async def submit(
    session_id: uuid.UUID,
    body: AnswersBody,
    user: User = Depends(require_consent),
    db: AsyncSession = Depends(get_db),
):
    return await checkin_service.submit(db, user.id, session_id, body.answers)
