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

@router.get("/clinician/patients/{patient_id}/adherence")
def adherence_summary(
    patient_id: uuid.UUID, days: int = Query(default=14, ge=1, le=90),
    user: User = Depends(require_clinician), db: Session = Depends(get_db),
):
    """Агрегат приверженности лечению (задача кейса 7). Roster-гейт."""
    return services.clinician.adherence_summary(db, user.id, patient_id, days)

class ClinicianChatBody(BaseModel):
    content: str = Field(min_length=1)

@router.post("/clinician/patients/{patient_id}/chat", status_code=201)
def clinician_chat_post(
    patient_id: uuid.UUID, body: ClinicianChatBody,
    user: User = Depends(require_clinician), db: Session = Depends(get_db),
):
    """Врач пишет пациенту (задача кейса 6 — канал коммуникации). Roster + аудит."""
    return services.clinician.clinician_post_chat(db, user.id, patient_id, body.content)

@router.get("/clinician/patients/{patient_id}/chat")
def clinician_chat_list(
    patient_id: uuid.UUID,
    user: User = Depends(require_clinician), db: Session = Depends(get_db),
):
    """История переписки врача с пациентом (задача кейса 6). Roster-гейт."""
    return {"items": services.clinician.clinician_list_chat(db, user.id, patient_id)}

class HistoryBody(BaseModel):
    text: str = Field(min_length=1)
    template_name: str | None = None

@router.post("/clinician/patients/{patient_id}/import-history")
def import_history(
    patient_id: uuid.UUID, body: HistoryBody,
    user: User = Depends(require_clinician), db: Session = Depends(get_db),
):
    """Разбор клинической истории в черновик полей (метрика кейса 2). Roster + аудит."""
    return services.clinician.import_history(db, user.id, patient_id, body.text, body.template_name)


me_router = APIRouter(tags=["me"])


@me_router.get("/me/timeline")
def timeline(days: int = Query(default=90, ge=1, le=365), user: User = Depends(require_consent), db: Session = Depends(get_db)):
    return services.clinician.patient_timeline(db, user.id, days)

class AdherenceBody(BaseModel):
    medication: str | None = None
    status: str = Field(pattern="^(taken|partial|missed)$")
    note: str | None = None

@me_router.post("/me/adherence", status_code=201)
def record_adherence(
    body: AdherenceBody,
    user: User = Depends(require_consent), db: Session = Depends(get_db),
):
    """Пациент отмечает приверженность лечению (задача кейса 7). consent-гейт."""
    return services.clinician.create_adherence(db, user.id, body.medication, body.status, body.note)