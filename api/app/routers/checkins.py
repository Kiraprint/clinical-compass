# E4: checkins. Контракт: docs/04 §3.1; тесты — docs/06 §3.
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import services
from app.db import get_db
from app.deps import require_consent
from app.models import User

router = APIRouter(tags=["checkins"])


class AnswersBody(BaseModel):
    answers: dict[str, int]


@router.get("/checkins/today")
def today(user: User = Depends(require_consent), db: Session = Depends(get_db)):
    return services.screening.today_checkin(db, user.id)


@router.post("/checkins", status_code=201)
def complete(body: AnswersBody, user: User = Depends(require_consent), db: Session = Depends(get_db)):
    return services.screening.complete_checkin(db, user.id, body.answers)