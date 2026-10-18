# E5: clinician panel + timeline. Контракт: docs/04 §3.1; тесты — docs/06 §3.
from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import services
from app.db import get_db
from app.deps import require_clinician, require_consent
from app.models import User

router = APIRouter(tags=["clinician"])

class PrescribeBody(BaseModel):
    scales: list[str] = Field(min_length=1)

@router.put("/clinician/patients/{patient_id}/prescribed-scales")
def prescribe(
    patient_id: uuid.UUID, body: PrescribeBody,
    user: User = Depends(require_clinician), db: Session = Depends(get_db),
):
    """Врач назначает пациенту множество шкал скрининга (item 1, multi-scale). Рост-гейт 403 NO_ROSTER."""
    return services.clinician.prescribe_scales(db, user.id, patient_id, body.scales)


@router.get("/clinician/patients")
def patients(
    triage: str | None = Query(default=None, pattern="^(GREEN|YELLOW|RED)$"),
    user: User = Depends(require_clinician),
    db: Session = Depends(get_db),
):
    return {"items": services.clinician.list_patients(db, user.id, triage)}


@router.get("/clinician/patients/{patient_id}/report")
def report(patient_id: uuid.UUID, user: User = Depends(require_clinician), db: Session = Depends(get_db)):
    return services.clinician.report(db, user.id, patient_id)

class AdverseEventBody(BaseModel):
    event_text: str = Field(min_length=1)
    severity: str = Field(pattern="^(mild|moderate|severe)$")
    onset_date: date
    suspected_drug: str | None = None
    action_taken: str | None = Field(default=None, pattern="^(none|monitoring|dose_reduced|stopped|referred)$")

@router.post("/clinician/patients/{patient_id}/adverse-events", status_code=201)
def create_adverse_event(
    patient_id: uuid.UUID, body: AdverseEventBody,
    user: User = Depends(require_clinician), db: Session = Depends(get_db),
):
    """Регистрация нежелательного явления (задача кейса 3). Roster-гейт + аудит."""
    return services.clinician.create_adverse_event(
        db, user.id, patient_id, body.event_text, body.severity, body.onset_date,
        body.suspected_drug, body.action_taken,
    )

@router.get("/clinician/patients/{patient_id}/adverse-events")
def list_adverse_events(
    patient_id: uuid.UUID,
    user: User = Depends(require_clinician), db: Session = Depends(get_db),
):
    return {"items": services.clinician.list_adverse_events(db, user.id, patient_id)}


me_router = APIRouter(tags=["me"])


@me_router.get("/me/timeline")
def timeline(days: int = Query(default=90, ge=1, le=365), user: User = Depends(require_consent), db: Session = Depends(get_db)):
    return services.clinician.patient_timeline(db, user.id, days)