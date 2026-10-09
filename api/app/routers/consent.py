# E3: consent. Контракт: docs/04 §3.1; тесты — docs/06 §3.
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_patient
from app.errors import Problem
from app.models import Consent, User

router = APIRouter(prefix="/consent", tags=["consent"])


class ConsentBody(BaseModel):
    version: int = Field(ge=1)


def _active(db: Session, user_id) -> Consent | None:
    return db.scalar(select(Consent).where(Consent.user_id == user_id, Consent.revoked_at.is_(None)))


@router.post("", status_code=201)
def create(body: ConsentBody, user: User = Depends(require_patient), db: Session = Depends(get_db)):
    if _active(db, user.id):
        raise Problem(409, "CONFLICT", "Активное согласие уже подписано")
    consent = Consent(user_id=user.id, version=body.version, text_hash=f"consent-v{body.version}")
    db.add(consent)
    db.commit()
    return {"id": str(consent.id), "version": consent.version, "revoked_at": consent.revoked_at}


@router.get("")
def get_active(user: User = Depends(require_patient), db: Session = Depends(get_db)):
    consent = _active(db, user.id)
    if consent is None:
        raise Problem(404, "NOT_FOUND", "Активное согласие не найдено")
    return {
        "id": str(consent.id),
        "version": consent.version,
        "signed_at": consent.signed_at.isoformat(),
        "revoked_at": consent.revoked_at,
    }


@router.post("/revoke", status_code=204)
def revoke(user: User = Depends(require_patient), db: Session = Depends(get_db)):
    consent = _active(db, user.id)
    if consent is None:
        raise Problem(409, "CONFLICT", "Нет активного согласия для отзыва")
    consent.revoked_at = datetime.now(timezone.utc)
    db.commit()