# E5: clinician panel + timeline. Контракт: docs/04 §3.1; тесты — docs/06 §3.
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import services
from app.db import get_db
from app.deps import require_clinician, require_consent
from app.models import User

router = APIRouter(tags=["clinician"])

class PrescribeBody(BaseModel):
    scale: str = Field(min_length=1)

@router.put("/clinician/patients/{patient_id}/prescribed-scale")
def prescribe(
    patient_id: uuid.UUID, body: PrescribeBody,
    user: User = Depends(require_clinician), db: Session = Depends(get_db),
):
    """Врач назначает пациенту шкалу скрининга (item 1). Рост-гейт 403 NO_ROSTER."""
    return services.clinician.prescribe_scale(db, user.id, patient_id, body.scale)


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


me_router = APIRouter(tags=["me"])


@me_router.get("/me/timeline")
def timeline(days: int = Query(default=90, ge=1, le=365), user: User = Depends(require_consent), db: Session = Depends(get_db)):
    return services.clinician.patient_timeline(db, user.id, days)