"""Нежелательные явления: сообщение пациента и проверка врачом (FR-28).

Пациент сообщает о НЯ (status='pending'), врач закреплённого ростера проверяет:
подтверждает/отклоняет, уточняет тяжесть, связывает с назначением и препаратом
справочника ГРЛС. Свободный текст (event_text, suspected_drug, verification_note)
и название назначения — спецкатегория ПДн: EncryptedText, в аудит и уведомления
не попадает. Аудит пишет только факт действия и id объекта.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import Problem
from app.models import AdverseEvent, ClinicianRoster, Drug, PatientMedication, User

from . import push
from .audit import record as audit_record
from .clinician import require_patient_consent

STATUSES = ("pending", "confirmed", "rejected")
PATIENT_STATUS_LABELS = {
    "pending": "на проверке",
    "confirmed": "проверено врачом",
    "rejected": "отклонено",
}
ACTION_TAKEN = ("none", "monitoring", "dose_reduced", "stopped", "referred")


async def _medication_title(db: AsyncSession, medication_id: uuid.UUID | None) -> str | None:
    if medication_id is None:
        return None
    med = await db.get(PatientMedication, medication_id)
    return med.title if med is not None else None


async def _drug_ref(db: AsyncSession, drug_id: str | None) -> dict | None:
    if drug_id is None:
        return None
    drug = (await db.scalars(select(Drug).where(Drug.drug_id == drug_id))).one_or_none()
    if drug is None:
        return None
    return {"drug_id": drug.drug_id, "trade_name": drug.trade_name, "adverse_events": drug.adverse_events}


async def _patient_dict(db: AsyncSession, e: AdverseEvent) -> dict:
    """Событие глазами пациента: без врачебных заметок и справочных текстов."""
    return {
        "id": str(e.id),
        "event_text": e.event_text,
        "severity": e.severity,
        "onset_date": e.onset_date.isoformat(),
        "status": e.status,
        "status_label": PATIENT_STATUS_LABELS[e.status],
        "medication_id": str(e.medication_id) if e.medication_id else None,
        "medication_title": await _medication_title(db, e.medication_id),
        "verified_at": e.verified_at.isoformat() if e.verified_at else None,
        "created_at": e.created_at.isoformat(),
    }


async def event_dict(db: AsyncSession, e: AdverseEvent, patient_fio: str | None = None) -> dict:
    """Событие для врача: проверка, связь с назначением и справочником ГРЛС."""
    drug = await _drug_ref(db, e.drug_id)
    return {
        "id": str(e.id),
        "patient_id": str(e.patient_id),
        "patient_fio": patient_fio,
        "event_text": e.event_text,
        "severity": e.severity,
        "onset_date": e.onset_date.isoformat(),
        "status": e.status,
        "reported_by": e.reported_by,
        "suspected_drug": e.suspected_drug,
        "action_taken": e.action_taken,
        "medication_id": str(e.medication_id) if e.medication_id else None,
        "medication_title": await _medication_title(db, e.medication_id),
        "drug_id": e.drug_id,
        "drug_trade_name": drug["trade_name"] if drug else None,
        "reference": drug,
        "verification_note": e.verification_note,
        "verified_by": str(e.verified_by) if e.verified_by else None,
        "verified_at": e.verified_at.isoformat() if e.verified_at else None,
        "created_at": e.created_at.isoformat(),
    }


async def _validate_medication(
    db: AsyncSession, patient_id: uuid.UUID, medication_id: uuid.UUID | None
) -> None:
    """Назначение должно быть активным назначением этого пациента."""
    if medication_id is None:
        return
    med = await db.get(PatientMedication, medication_id)
    # Чужое назначение неотличимо от несуществующего (IDOR).
    if med is None or med.patient_id != patient_id:
        raise Problem(422, "MEDICATION_NOT_FOUND", "Назначение не найдено у пациента")


async def _validate_drug(db: AsyncSession, drug_id: str | None) -> None:
    if drug_id is None:
        return
    exists = await db.scalar(select(Drug.drug_id).where(Drug.drug_id == drug_id))
    if exists is None:
        raise Problem(404, "DRUG_NOT_FOUND", "Препарат не найден в справочнике ГРЛС")


async def report_patient_event(
    db: AsyncSession,
    patient: User,
    *,
    event_text: str,
    severity: str,
    onset_date: date,
    medication_id: uuid.UUID | None = None,
    ip: str | None = None,
) -> dict:
    """Пациент сообщает о НЯ: status='pending', проверки ещё не было."""
    await _validate_medication(db, patient.id, medication_id)
    ev = AdverseEvent(
        patient_id=patient.id, clinician_id=None,
        event_text=event_text, severity=severity, onset_date=onset_date,
        medication_id=medication_id, reported_by="patient", status="pending",
    )
    db.add(ev)
    await db.flush()
    await audit_record(db, patient.id, "ADVERSE_REPORT", "patient", str(patient.id), ip=ip)
    if severity == "severe":
        # Текст без ПДн: ни имени, ни описания события.
        await push.notify_adverse_event_clinicians(db, patient.id)
    await db.commit()
    return await _patient_dict(db, ev)


async def list_patient_events(db: AsyncSession, patient_id: uuid.UUID) -> list[dict]:
    rows = (await db.scalars(
        select(AdverseEvent)
        .where(AdverseEvent.patient_id == patient_id)
        .order_by(AdverseEvent.created_at.desc(), AdverseEvent.id.desc())
    )).all()
    return [await _patient_dict(db, e) for e in rows]


async def list_queue(db: AsyncSession, clinician_id: uuid.UUID, status: str | None = None) -> list[dict]:
    """Очередь НЯ по пациентам ростера врача, новые сверху."""
    if status is not None and status not in STATUSES:
        raise Problem(422, "VALIDATION", f"Неизвестный статус: {status}")
    q = (
        select(AdverseEvent, User.fio)
        .join(ClinicianRoster, ClinicianRoster.patient_id == AdverseEvent.patient_id)
        .join(User, User.id == AdverseEvent.patient_id)
        .where(ClinicianRoster.clinician_id == clinician_id)
        .order_by(AdverseEvent.created_at.desc(), AdverseEvent.id.desc())
    )
    if status is not None:
        q = q.where(AdverseEvent.status == status)
    return [await event_dict(db, e, fio) for e, fio in (await db.execute(q)).all()]


async def _get_for_clinician(
    db: AsyncSession, clinician_id: uuid.UUID, patient_id: uuid.UUID, event_id: uuid.UUID
) -> AdverseEvent:
    """404 для чужого пациента и чужого события — без утечки существования."""
    roster = await db.get(ClinicianRoster, (clinician_id, patient_id))
    if roster is None:
        raise Problem(404, "ADVERSE_NOT_FOUND", "Нежелательное явление не найдено")
    ev = await db.get(AdverseEvent, event_id)
    if ev is None or ev.patient_id != patient_id:
        raise Problem(404, "ADVERSE_NOT_FOUND", "Нежелательное явление не найдено")
    await require_patient_consent(db, patient_id)
    return ev


async def verify_event(
    db: AsyncSession,
    clinician_id: uuid.UUID,
    patient_id: uuid.UUID,
    event_id: uuid.UUID,
    *,
    status: str,
    severity: str | None = None,
    suspected_drug: str | None = None,
    medication_id: uuid.UUID | None = None,
    drug_id: str | None = None,
    action_taken: str | None = None,
    verification_note: str | None = None,
    ip: str | None = None,
) -> dict:
    """Врач подтверждает/отклоняет сообщение пациента.

    Повтор с тем же решением идемпотентен (возвращается текущая строка без
    изменений и без новой записи аудита); смена уже принятого решения → 409.
    """
    ev = await _get_for_clinician(db, clinician_id, patient_id, event_id)
    if severity is not None and severity not in ("mild", "moderate", "severe"):
        raise Problem(422, "VALIDATION", "Неизвестная тяжесть")
    if action_taken is not None and action_taken not in ACTION_TAKEN:
        raise Problem(422, "VALIDATION", "Неизвестное действие")
    await _validate_drug(db, drug_id)
    await _validate_medication(db, patient_id, medication_id)

    patient = await db.get(User, patient_id)
    if ev.status != "pending":
        if ev.status == status:
            return await event_dict(db, ev, patient.fio if patient else None)
        raise Problem(409, "ADVERSE_ALREADY_VERIFIED", "Нежелательное явление уже проверено")

    ev.status = status
    ev.verified_by = clinician_id
    ev.verified_at = datetime.now(timezone.utc)
    if severity is not None:
        ev.severity = severity
    if suspected_drug is not None:
        ev.suspected_drug = suspected_drug
    if medication_id is not None:
        ev.medication_id = medication_id
    if drug_id is not None:
        ev.drug_id = drug_id
    if action_taken is not None:
        ev.action_taken = action_taken
    if verification_note is not None:
        ev.verification_note = verification_note
    await audit_record(db, clinician_id, "ADVERSE_VERIFY", "adverse_event", str(ev.id), ip=ip)
    await db.commit()
    return await event_dict(db, ev, patient.fio if patient else None)
