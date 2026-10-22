# FR-22: контакты в собственном профиле пациента. Контракт: docs/04 §3.1; тесты — docs/06 §3.
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import services
from app.db import get_db
from app.deps import require_consent
from app.models import User

router = APIRouter(prefix="/profile", tags=["profile"])


class ContactsBody(BaseModel):
    # Строки: нормализация/границы — services.contacts (422 PHONE_INVALID / BIRTH_DATE_INVALID).
    phone: str | None = None
    birth_date: str | None = None


@router.get("/contacts")
def get_contacts(request: Request, user: User = Depends(require_consent), db: Session = Depends(get_db)):
    """Свои контакты (152-ФЗ: без активного согласия — 403 CONSENT_REQUIRED). Аудит READ_OWN_CONTACTS."""
    ip = request.client.host if request.client else None
    return services.contacts.own_read(db, user, ip=ip)


@router.patch("/contacts")
def update_contacts(
    body: ContactsBody, request: Request,
    user: User = Depends(require_consent), db: Session = Depends(get_db),
):
    """Правка только собственных контактов. Аудит UPDATE_OWN_CONTACTS (без ПДн в payload)."""
    ip = request.client.host if request.client else None
    return services.contacts.own_update(db, user, body.model_dump(exclude_unset=True), ip=ip)
