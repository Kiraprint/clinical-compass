"""Нежелательные явления (FR-28): сообщение пациента и проверка врачом.

Контракт — docs/04 §3, тесты — docs/06 §3. Пациент видит только свои события и
статус проверки (без врачебных заметок), врач — очередь по своим пациентам и
фрагмент справочника ГРЛС (Drug.adverse_events).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app import services
from app.db import get_db
from app.deps import require_clinician, require_consent
from app.errors import Problem
from app.models import User

router = APIRouter(tags=["adverse"])

MOSCOW = ZoneInfo("Europe/Moscow")

def _moscow_today() -> date:
    """Сегодня по МСК: сервер живёт в UTC, а пациент — в московской зоне."""
    return datetime.now(MOSCOW).date()

class PatientEventBody(BaseModel):
    event_text: str = Field(min_length=1, max_length=1000)
    severity: str = Field(pattern="^(mild|moderate|severe)$")
    onset_date: date
    medication_id: uuid.UUID | None = None
    # Справочный препарат пациент не выбирает: связь ставит врач при проверке.

@router.post("/me/adverse-events", status_code=201)
async def report_adverse_event(
    body: PatientEventBody, request: Request,
    user: User = Depends(require_consent), db: AsyncSession = Depends(get_db),
):
    """Пациент сообщает о НЯ → status=pending, аудит ADVERSE_REPORT без текста."""
    if body.onset_date > _moscow_today():
        raise Problem(422, "VALIDATION", "Дата начала не может быть в будущем")
    ip = request.client.host if request.client else None
    return await services.adverse.report_patient_event(
        db, user, event_text=body.event_text, severity=body.severity,
        onset_date=body.onset_date, medication_id=body.medication_id, ip=ip,
    )

@router.get("/me/adverse-events")
async def my_adverse_events(
    user: User = Depends(require_consent), db: AsyncSession = Depends(get_db),
):
    """Свои НЯ со статусом проверки: проверено врачом / на проверке / отклонено."""
    return {"items": await services.adverse.list_patient_events(db, user.id)}

@router.get("/clinician/adverse-events")
async def adverse_queue(
    status: str | None = Query(default=None, pattern="^(pending|confirmed|rejected)$"),
    user: User = Depends(require_clinician), db: AsyncSession = Depends(get_db),
):
    """Очередь НЯ по пациентам ростера врача (по умолчанию все статусы)."""
    return {"items": await services.adverse.list_queue(db, user.id, status)}

class VerifyBody(BaseModel):
    status: str = Field(pattern="^(confirmed|rejected)$")
    severity: str | None = Field(default=None, pattern="^(mild|moderate|severe)$")
    suspected_drug: str | None = Field(default=None, max_length=128)
    medication_id: uuid.UUID | None = None
    drug_id: str | None = Field(default=None, max_length=64)
    action_taken: str | None = Field(default=None, pattern="^(none|monitoring|dose_reduced|stopped|referred)$")
    verification_note: str | None = Field(default=None, max_length=2000)

@router.patch("/clinician/patients/{patient_id}/adverse-events/{event_id}")
async def verify_adverse_event(
    patient_id: uuid.UUID, event_id: uuid.UUID, body: VerifyBody, request: Request,
    user: User = Depends(require_clinician), db: AsyncSession = Depends(get_db),
):
    """Проверка НЯ: подтвердить/отклонить, уточнить тяжесть и связь с препаратом.

    Повтор с тем же статусом идемпотентен; смена принятого решения → 409
    ADVERSE_ALREADY_VERIFIED. Чужой пациент/событие → 404 ADVERSE_NOT_FOUND.
    """
    ip = request.client.host if request.client else None
    return await services.adverse.verify_event(
        db, user.id, patient_id, event_id,
        status=body.status, severity=body.severity, suspected_drug=body.suspected_drug,
        medication_id=body.medication_id, drug_id=body.drug_id,
        action_taken=body.action_taken, verification_note=body.verification_note, ip=ip,
    )
