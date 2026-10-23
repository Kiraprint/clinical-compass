# E4: чек-ин. Контракт: docs/16 §4; FR-6 — docs/04 §3.1.
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

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
def today(user: User = Depends(require_consent), db: Session = Depends(get_db)):
    return checkin_service.today(db, user.id)


@router.post("/checkins")
def start(
    response: Response,
    user: User = Depends(require_consent),
    db: Session = Depends(get_db),
):
    body, created = checkin_service.start(db, user.id)
    response.status_code = 201 if created else 200
    return body


@router.post("/checkins/{session_id}/answers")
def submit(
    session_id: uuid.UUID,
    body: AnswersBody,
    user: User = Depends(require_consent),
    db: Session = Depends(get_db),
):
    return checkin_service.submit(db, user.id, session_id, body.answers)
