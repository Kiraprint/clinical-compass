# E5: diary. Контракт: docs/04 §3.1; тесты — docs/06 §3.
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import services
from app.db import get_db
from app.deps import require_consent
from app.models import User

router = APIRouter(tags=["diary"])


class DiaryBody(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    mood: int | None = Field(default=None, ge=1, le=10)
    sleep_h: float | None = Field(default=None, ge=0, le=12)
    tags: list[str] = Field(default_factory=list)


@router.post("/diary", status_code=201)
def create(body: DiaryBody, user: User = Depends(require_consent), db: Session = Depends(get_db)):
    entry = services.diary.create(db, user.id, body.content, body.mood, body.sleep_h, body.tags)
    return services.diary.to_dict(entry)


@router.get("/diary")
def list_diary(user: User = Depends(require_consent), db: Session = Depends(get_db)):
    return {"items": [services.diary.to_dict(e) for e in services.diary.list_for_patient(db, user.id)]}