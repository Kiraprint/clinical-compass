# E5: clinician panel + timeline. Контракт: docs/04 §3.1; тесты — docs/06 §3.
from __future__ import annotations

import uuid
from datetime import date, datetime

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app import services
from app.db import get_db
from app.deps import require_clinician, require_consent
from app.models import User

router = APIRouter(tags=["clinician"])

class PrescribeBody(BaseModel):
    scales: list[str] = Field(min_length=1)

@router.put("/clinician/patients/{patient_id}/prescribed-scales")
async def prescribe(
    patient_id: uuid.UUID, body: PrescribeBody, request: Request,
    user: User = Depends(require_clinician), db: AsyncSession = Depends(get_db),
):
    """Врач назначает пациенту множество шкал скрининга (item 1, multi-scale). Рост-гейт 403 NO_ROSTER."""
    ip = request.client.host if request.client else None
    return await services.clinician.prescribe_scales(db, user.id, patient_id, body.scales, ip=ip)

class AssessmentBody(BaseModel):
    questionnaire_code: str
    answers: dict[str, int]

@router.post("/clinician/patients/{patient_id}/assessments")
async def create_assessment(
    patient_id: uuid.UUID, body: AssessmentBody, request: Request,
    user: User = Depends(require_clinician), db: AsyncSession = Depends(get_db),
):
    """Врач заполняет клиницистскую шкалу (HDRS-17). Roster-гейт + аудит CLINICIAN_ASSESSMENT."""
    ip = request.client.host if request.client else None
    return await services.clinician.create_clinician_assessment(
        db, user.id, patient_id, body.questionnaire_code, body.answers, ip=ip,
    )


@router.get("/clinician/patients")
async def patients(
    triage: str | None = Query(default=None, pattern="^(GREEN|YELLOW|RED)$"),
    user: User = Depends(require_clinician),
    db: AsyncSession = Depends(get_db),
):
    return {"items": await services.clinician.list_patients(db, user.id, triage)}


@router.get("/clinician/patients/{patient_id}/report")
async def report(patient_id: uuid.UUID, request: Request, user: User = Depends(require_clinician), db: AsyncSession = Depends(get_db)):
    ip = request.client.host if request.client else None
    return await services.clinician.report(db, user.id, patient_id, ip=ip)

class AdverseEventBody(BaseModel):
    event_text: str = Field(min_length=1)
    severity: str = Field(pattern="^(mild|moderate|severe)$")
    onset_date: date
    suspected_drug: str | None = None
    action_taken: str | None = Field(default=None, pattern="^(none|monitoring|dose_reduced|stopped|referred)$")

@router.post("/clinician/patients/{patient_id}/adverse-events", status_code=201)
async def create_adverse_event(
    patient_id: uuid.UUID, body: AdverseEventBody, request: Request,
    user: User = Depends(require_clinician), db: AsyncSession = Depends(get_db),
):
    """Регистрация нежелательного явления (задача кейса 3). Roster-гейт + аудит."""
    ip = request.client.host if request.client else None
    return await services.clinician.create_adverse_event(
        db, user.id, patient_id, body.event_text, body.severity, body.onset_date,
        body.suspected_drug, body.action_taken, ip=ip,
    )

@router.get("/clinician/patients/{patient_id}/adverse-events")
async def list_adverse_events(
    patient_id: uuid.UUID,
    user: User = Depends(require_clinician), db: AsyncSession = Depends(get_db),
):
    return {"items": await services.clinician.list_adverse_events(db, user.id, patient_id)}


class ContactsBody(BaseModel):
    # Строки, а не date/строгие типы: нормализация и границы — в services.contacts
    # (422 PHONE_INVALID / BIRTH_DATE_INVALID вместо общего VALIDATION).
    phone: str | None = None
    birth_date: str | None = None

@router.get("/clinician/patients/{patient_id}/contacts")
async def get_patient_contacts(
    patient_id: uuid.UUID, request: Request,
    user: User = Depends(require_clinician), db: AsyncSession = Depends(get_db),
):
    """FR-22: контакты закреплённого пациента. Roster + consent + аудит READ_PATIENT_CONTACTS."""
    ip = request.client.host if request.client else None
    return await services.contacts.clinician_read(db, user.id, patient_id, ip=ip)

@router.patch("/clinician/patients/{patient_id}/contacts")
async def update_patient_contacts(
    patient_id: uuid.UUID, body: ContactsBody, request: Request,
    user: User = Depends(require_clinician), db: AsyncSession = Depends(get_db),
):
    """FR-22: врач правит контакты. Roster + consent + аудит UPDATE_PATIENT_CONTACTS (без ПДн)."""
    ip = request.client.host if request.client else None
    return await services.contacts.clinician_update(db, user.id, patient_id, body.model_dump(exclude_unset=True), ip=ip)


@router.get("/clinician/patients/{patient_id}/adherence")
async def adherence_summary(
    patient_id: uuid.UUID, days: int = Query(default=14, ge=1, le=90),
    user: User = Depends(require_clinician), db: AsyncSession = Depends(get_db),
):
    """Агрегат приверженности лечению (задача кейса 7). Roster-гейт."""
    return await services.clinician.adherence_summary(db, user.id, patient_id, days)

class VisitBody(BaseModel):
    visited_at: datetime
    visit_type: str = Field(pattern="^(in_person|online|phone)$")
    duration_min: int | None = Field(default=None, ge=1, le=480)
    notes: str = Field(min_length=1)

@router.post("/clinician/patients/{patient_id}/visits", status_code=201)
async def create_visit(
    patient_id: uuid.UUID, body: VisitBody, request: Request,
    user: User = Depends(require_clinician), db: AsyncSession = Depends(get_db),
):
    """Запись приёма врач-пациент (задача кейса 6, п. «приём»). Roster + аудит VISIT."""
    ip = request.client.host if request.client else None
    return await services.clinician.create_visit(
        db, user.id, patient_id,
        body.visited_at, body.visit_type, body.duration_min, body.notes, ip=ip,
    )

@router.get("/clinician/patients/{patient_id}/visits")
async def list_visits(
    patient_id: uuid.UUID,
    user: User = Depends(require_clinician), db: AsyncSession = Depends(get_db),
):
    """История приёмов пациента (задача кейса 6, п. «приём»). Roster-гейт."""
    return {"items": await services.clinician.list_visits(db, user.id, patient_id)}

@router.get("/clinician/patients/{patient_id}/emias")
async def emias_summary(
    patient_id: uuid.UUID, request: Request,
    user: User = Depends(require_clinician), db: AsyncSession = Depends(get_db),
):
    """Блок «ЕМИАС» (FR-25): связка, история назначений, число приёмов. Roster-гейт."""
    ip = request.client.host if request.client else None
    return await services.clinician.emias_summary(db, user.id, patient_id, ip=ip)

@router.delete("/clinician/patients/{patient_id}/visits/{visit_id}", status_code=204)
async def delete_visit(
    patient_id: uuid.UUID, visit_id: uuid.UUID, request: Request,
    user: User = Depends(require_clinician), db: AsyncSession = Depends(get_db),
):
    """Удаление ошибочной записи приёма (исправление опечаток). Roster + аудит VISIT_DELETE."""
    ip = request.client.host if request.client else None
    await services.clinician.delete_visit(db, user.id, patient_id, visit_id, ip=ip)

class MedicationBody(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    dosage: str | None = Field(default=None, max_length=200)
    started_on: date | None = None
    ended_on: date | None = None
    status: str = "active"
    notes: str | None = Field(default=None, max_length=2000)
    # FR-27: режим приёма (1..6). Не задан — процент приверженности не считается.
    times_per_day: int | None = None

class MedicationPatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dosage: str | None = Field(default=None, max_length=200)
    status: str | None = None
    ended_on: date | None = None
    notes: str | None = Field(default=None, max_length=2000)
    times_per_day: int | None = None

class StopMedicationBody(BaseModel):
    ended_on: date | None = None

@router.get("/clinician/patients/{patient_id}/medications")
async def list_medications(
    patient_id: uuid.UUID,
    status: str | None = Query(default=None, pattern="^(active|completed|stopped|unknown)$"),
    active_only: bool = Query(default=False),
    user: User = Depends(require_clinician), db: AsyncSession = Depends(get_db),
):
    """Назначения пациента: импорт ЕМИАС + назначения врача (FR-23). Roster + consent."""
    return {
        "items": await services.medications.list_medications(
            db, user.id, patient_id, status=status, active_only=active_only,
        )
    }

@router.post("/clinician/patients/{patient_id}/medications", status_code=201)
async def create_medication(
    patient_id: uuid.UUID, body: MedicationBody, request: Request,
    user: User = Depends(require_clinician), db: AsyncSession = Depends(get_db),
):
    """Назначение препарата врачом. Roster + consent + аудит PRESCRIBE_MEDICATION."""
    ip = request.client.host if request.client else None
    return await services.medications.create_medication(
        db, user.id, patient_id,
        title=body.title, dosage=body.dosage, started_on=body.started_on,
        ended_on=body.ended_on, status=body.status, notes=body.notes,
        times_per_day=body.times_per_day, ip=ip,
    )

@router.patch("/clinician/patients/{patient_id}/medications/{med_id}")
async def update_medication(
    patient_id: uuid.UUID, med_id: uuid.UUID, body: MedicationPatchBody, request: Request,
    user: User = Depends(require_clinician), db: AsyncSession = Depends(get_db),
):
    """Правка дозировки/статуса/даты окончания/заметки. Аудит UPDATE_MEDICATION."""
    ip = request.client.host if request.client else None
    return await services.medications.update_medication(
        db, user.id, patient_id, med_id, body.model_dump(exclude_unset=True), ip=ip,
    )

@router.post("/clinician/patients/{patient_id}/medications/{med_id}/stop")
async def stop_medication(
    patient_id: uuid.UUID, med_id: uuid.UUID, request: Request,
    body: StopMedicationBody | None = None,
    user: User = Depends(require_clinician), db: AsyncSession = Depends(get_db),
):
    """Остановка приёма (status='stopped', ended_on — сегодня по умолчанию)."""
    ip = request.client.host if request.client else None
    return await services.medications.stop_medication(
        db, user.id, patient_id, med_id, ended_on=body.ended_on if body else None, ip=ip,
    )

@router.delete("/clinician/patients/{patient_id}/medications/{med_id}", status_code=204)
async def delete_medication(
    patient_id: uuid.UUID, med_id: uuid.UUID, request: Request,
    user: User = Depends(require_clinician), db: AsyncSession = Depends(get_db),
):
    """Удаление ошибочного назначения (только своя локальная строка). Аудит DELETE_MEDICATION."""
    ip = request.client.host if request.client else None
    await services.medications.delete_medication(db, user.id, patient_id, med_id, ip=ip)

@router.get("/clinician/patients/{patient_id}/history")
async def patient_history(
    patient_id: uuid.UUID, request: Request,
    types: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    before: str | None = Query(default=None),
    user: User = Depends(require_clinician), db: AsyncSession = Depends(get_db),
):
    """Лента карточки: приёмы, назначения, скрининги, НЯ (FR-23). Аудит READ_PATIENT_HISTORY."""
    ip = request.client.host if request.client else None
    return await services.patient_history.patient_history(
        db, user.id, patient_id, types=types, limit=limit, before=before, ip=ip,
    )

class ClinicianChatBody(BaseModel):
    content: str = Field(min_length=1)

@router.post("/clinician/patients/{patient_id}/chat", status_code=201)
async def clinician_chat_post(
    patient_id: uuid.UUID, body: ClinicianChatBody, request: Request,
    user: User = Depends(require_clinician), db: AsyncSession = Depends(get_db),
):
    """Врач пишет пациенту (задача кейса 6 — канал коммуникации). Roster + аудит."""
    ip = request.client.host if request.client else None
    return await services.clinician.clinician_post_chat(db, user.id, patient_id, body.content, ip=ip)

@router.get("/clinician/patients/{patient_id}/chat")
async def clinician_chat_list(
    patient_id: uuid.UUID,
    user: User = Depends(require_clinician), db: AsyncSession = Depends(get_db),
):
    """История переписки врача с пациентом (задача кейса 6). Roster-гейт."""
    return {"items": await services.clinician.clinician_list_chat(db, user.id, patient_id)}

class HistoryBody(BaseModel):
    text: str = Field(min_length=1)
    template_name: str | None = None

@router.post("/clinician/patients/{patient_id}/import-history")
async def import_history(
    patient_id: uuid.UUID, body: HistoryBody, request: Request,
    user: User = Depends(require_clinician), db: AsyncSession = Depends(get_db),
):
    """Разбор клинической истории в черновик полей (метрика кейса 2). Roster + аудит."""
    ip = request.client.host if request.client else None
    return await services.clinician.import_history(db, user.id, patient_id, body.text, body.template_name, ip=ip)



# --- FR-24: напоминания врача по пациентам ---------------------------------

class ReminderBody(BaseModel):
    patient_id: uuid.UUID
    kind: str = Field(pattern="^(call|write|appointment|custom)$")
    note: str | None = Field(default=None, max_length=2000)
    due_at: datetime

class ReminderPatchBody(BaseModel):
    note: str | None = Field(default=None, max_length=2000)
    due_at: datetime | None = None
    kind: str | None = Field(default=None, pattern="^(call|write|appointment|custom)$")

@router.get("/clinician/reminders")
async def list_reminders(
    status: str = Query(default="open", pattern="^(open|done|cancelled|all)$"),
    due: str | None = Query(default=None, pattern="^(overdue|today|week)$"),
    user: User = Depends(require_clinician),
    db: AsyncSession = Depends(get_db),
):
    """Напоминания врача: open по умолчанию, фильтр срока overdue/today/week."""
    return await services.reminders.list_reminders(db, user.id, status=status, due=due)

@router.post("/clinician/reminders", status_code=201)
async def create_reminder(
    body: ReminderBody,
    user: User = Depends(require_clinician),
    db: AsyncSession = Depends(get_db),
):
    """Создать напоминание по пациенту из roster (иначе 403 NO_ROSTER)."""
    return await services.reminders.create(
        db, user.id, body.patient_id, body.kind, body.note, body.due_at
    )

@router.patch("/clinician/reminders/{reminder_id}")
async def patch_reminder(
    reminder_id: uuid.UUID,
    body: ReminderPatchBody,
    user: User = Depends(require_clinician),
    db: AsyncSession = Depends(get_db),
):
    """Правка открытого напоминания (закрытое → 409 REMINDER_CLOSED)."""
    return await services.reminders.update(
        db,
        user.id,
        reminder_id,
        note=body.note,
        note_set="note" in body.model_fields_set,
        due_at=body.due_at,
        kind=body.kind,
    )

@router.post("/clinician/reminders/{reminder_id}/done")
async def complete_reminder(
    reminder_id: uuid.UUID,
    user: User = Depends(require_clinician),
    db: AsyncSession = Depends(get_db),
):
    return await services.reminders.complete(db, user.id, reminder_id)

@router.post("/clinician/reminders/{reminder_id}/cancel")
async def cancel_reminder(
    reminder_id: uuid.UUID,
    user: User = Depends(require_clinician),
    db: AsyncSession = Depends(get_db),
):
    return await services.reminders.cancel(db, user.id, reminder_id)

@router.get("/clinician/patients/{patient_id}/reminders")
async def patient_reminders(
    patient_id: uuid.UUID,
    status: str = Query(default="open", pattern="^(open|done|cancelled|all)$"),
    user: User = Depends(require_clinician),
    db: AsyncSession = Depends(get_db),
):
    """Напоминания по одному пациенту карточки (roster-гейт 403 NO_ROSTER)."""
    await services.clinician.require_roster(db, user.id, patient_id)
    return await services.reminders.list_reminders(db, user.id, status=status, patient_id=patient_id)

me_router = APIRouter(tags=["me"])


@me_router.get("/me/timeline")
async def timeline(days: int = Query(default=90, ge=1, le=365), user: User = Depends(require_consent), db: AsyncSession = Depends(get_db)):
    return await services.clinician.patient_timeline(db, user.id, days)

class AdherenceBody(BaseModel):
    # FR-27: отметка привязывается к активному назначению пациента. medication_id
    # необязателен только для исторических/внеплановых отметок — они вне расчёта.
    medication_id: uuid.UUID | None = None
    medication: str | None = None
    status: str = Field(pattern="^(taken|partial|missed)$")
    note: str | None = None

@me_router.get("/me/adherence")
async def my_adherence(
    request: Request, days: int = Query(default=14, ge=1, le=365),
    user: User = Depends(require_consent), db: AsyncSession = Depends(get_db),
):
    """Пациент читает свои отметки приверженности (экран «Сегодня», задача кейса 7)."""
    ip = request.client.host if request.client else None
    await services.audit.record(db, user.id, "READ_ADHERENCE", "patient", ip=ip)
    await db.commit()  # F6: аудит чтения своего PHI должен сохраняться
    return await services.clinician.my_adherence(db, user.id, days)


@me_router.post("/me/adherence", status_code=201)
async def record_adherence(
    body: AdherenceBody,
    user: User = Depends(require_consent), db: AsyncSession = Depends(get_db),
):
    """Пациент отмечает приверженность лечению (задача кейса 7). consent-гейт."""
    return await services.clinician.create_adherence(
        db, user.id, body.medication_id, body.medication, body.status, body.note,
    )