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


async def _medication_titles(
    db: AsyncSession, medication_ids: set[uuid.UUID | None]
) -> dict[uuid.UUID, str]:
    """Названия назначений по ids — одним запросом на весь список событий."""
    ids = {i for i in medication_ids if i is not None}
    if not ids:
        return {}
    rows = (await db.execute(
        select(PatientMedication.id, PatientMedication.title).where(PatientMedication.id.in_(ids))
    )).all()
    return dict(rows)


async def _drug_refs(db: AsyncSession, drug_ids: set[str | None]) -> dict[str, dict]:
    """Фрагменты справочника ГРЛС по ids — одним запросом на весь список событий."""
    ids = {i for i in drug_ids if i}
    if not ids:
        return {}
    drugs = (await db.scalars(select(Drug).where(Drug.drug_id.in_(ids)))).all()
    return {d.drug_id: _drug_ref(d) for d in drugs}


def _drug_ref(d: Drug) -> dict:
    return {"drug_id": d.drug_id, "trade_name": d.trade_name, "adverse_events": d.adverse_events}


def _patient_dict(e: AdverseEvent, med_titles: dict[uuid.UUID, str]) -> dict:
    """Событие глазами пациента: без врачебных заметок и справочных текстов."""
    return {
        "id": str(e.id),
        "event_text": e.event_text,
        "severity": e.severity,
        "onset_date": e.onset_date.isoformat(),
        "status": e.status,
        "status_label": PATIENT_STATUS_LABELS[e.status],
        "medication_id": str(e.medication_id) if e.medication_id else None,
        "medication_title": med_titles.get(e.medication_id) if e.medication_id else None,
        "verified_at": e.verified_at.isoformat() if e.verified_at else None,
        "created_at": e.created_at.isoformat(),
    }


def _event_dict(
    e: AdverseEvent,
    patient_fio: str | None,
    med_titles: dict[uuid.UUID, str],
    drug_refs: dict[str, dict],
) -> dict:
    """Событие для врача: проверка, связь с назначением и справочником ГРЛС."""
    drug = drug_refs.get(e.drug_id) if e.drug_id else None
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
        "medication_title": med_titles.get(e.medication_id) if e.medication_id else None,
        "drug_id": e.drug_id,
        "drug_trade_name": drug["trade_name"] if drug else None,
        "reference": drug,
        "verification_note": e.verification_note,
        "verified_by": str(e.verified_by) if e.verified_by else None,
        "verified_at": e.verified_at.isoformat() if e.verified_at else None,
        "created_at": e.created_at.isoformat(),
    }


async def event_dicts(
    db: AsyncSession, events: list[AdverseEvent], fios: dict[uuid.UUID, str | None] | None = None
) -> list[dict]:
    """События для врача: 2 запроса (назначения + ГРЛС) на весь список, не на строку."""
    med_titles = await _medication_titles(db, {e.medication_id for e in events})
    drug_refs = await _drug_refs(db, {e.drug_id for e in events})
    return [_event_dict(e, (fios or {}).get(e.patient_id), med_titles, drug_refs) for e in events]


async def patient_event_dicts(db: AsyncSession, events: list[AdverseEvent]) -> list[dict]:
    """События глазами пациента: 1 запрос названий назначений на весь список."""
    med_titles = await _medication_titles(db, {e.medication_id for e in events})
    return [_patient_dict(e, med_titles) for e in events]


async def events_for_patient(db: AsyncSession, patient_id, limit: int = 20) -> list[dict]:
    """События пациента для карточки врача — одним запросом (было три).

    Назначение и справочник ГРЛС присоединяются джойном, а не батчем по ids:
    название назначения — EncryptedText, и расшифровка остаётся за слоем типов
    (json_agg вернул бы шифротекст и потребовал бы ручного pgp_sym_decrypt).
    """
    rows = (await db.execute(
        select(AdverseEvent, PatientMedication.title, Drug)
        .outerjoin(PatientMedication, PatientMedication.id == AdverseEvent.medication_id)
        .outerjoin(Drug, Drug.drug_id == AdverseEvent.drug_id)
        .where(AdverseEvent.patient_id == patient_id)
        .order_by(AdverseEvent.onset_date.desc(), AdverseEvent.created_at.desc())
        .limit(limit)
    )).all()
    med_titles = {e.medication_id: title for e, title, _ in rows if e.medication_id is not None}
    drug_refs = {d.drug_id: _drug_ref(d) for _, _, d in rows if d is not None}
    return [_event_dict(e, None, med_titles, drug_refs) for e, _, _ in rows]


async def _validate_medication(
    db: AsyncSession, patient_id: uuid.UUID, medication_id: uuid.UUID | None
) -> None:
    """Назначение должно принадлежать этому пациенту.

    Статус назначения здесь не проверяется: событие могло начаться на уже
    отменённом препарате, и врач должен уметь связать его с историей.
    """
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
    onset_date: date,
    medication_id: uuid.UUID | None = None,
    ip: str | None = None,
) -> dict:
    """Пациент сообщает о НЯ: status='pending', severity пока не оценена (None).

    Тяжесть пациент не задаёт — это колонка врачебной проверки. Врачи ростера
    получают уведомление о любом сообщении: раньше пуш уходил только при
    severity='severe', а пациентская форма её больше не присылает.
    """
    await _validate_medication(db, patient.id, medication_id)
    ev = AdverseEvent(
        patient_id=patient.id, clinician_id=None,
        event_text=event_text, severity=None, onset_date=onset_date,
        medication_id=medication_id, reported_by="patient", status="pending",
    )
    db.add(ev)
    await db.flush()
    await audit_record(db, patient.id, "ADVERSE_REPORT", "patient", str(patient.id), ip=ip)
    # Текст без ПДн: ни имени, ни описания события.
    await push.notify_adverse_event_clinicians(db, patient.id)
    await db.commit()
    return (await patient_event_dicts(db, [ev]))[0]


async def list_patient_events(db: AsyncSession, patient_id: uuid.UUID, limit: int = 100) -> list[dict]:
    rows = (await db.scalars(
        select(AdverseEvent)
        .where(AdverseEvent.patient_id == patient_id)
        .order_by(AdverseEvent.created_at.desc(), AdverseEvent.id.desc())
        .limit(limit)
    )).all()
    return await patient_event_dicts(db, list(rows))


async def list_queue(
    db: AsyncSession, clinician_id: uuid.UUID, status: str | None = None, limit: int = 100
) -> list[dict]:
    """Очередь НЯ по пациентам ростера врача, новые сверху (не больше `limit`)."""
    if status is not None and status not in STATUSES:
        raise Problem(422, "VALIDATION", f"Неизвестный статус: {status}")
    q = (
        select(AdverseEvent, User.fio)
        .join(ClinicianRoster, ClinicianRoster.patient_id == AdverseEvent.patient_id)
        .join(User, User.id == AdverseEvent.patient_id)
        .where(ClinicianRoster.clinician_id == clinician_id)
        .order_by(AdverseEvent.created_at.desc(), AdverseEvent.id.desc())
        .limit(limit)
    )
    if status is not None:
        q = q.where(AdverseEvent.status == status)
    rows = (await db.execute(q)).all()
    events = [e for e, _ in rows]
    return await event_dicts(db, events, {e.patient_id: fio for e, fio in rows})


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
            return (await event_dicts(db, [ev], {ev.patient_id: patient.fio if patient else None}))[0]
        raise Problem(409, "ADVERSE_ALREADY_VERIFIED", "Нежелательное явление уже проверено")
    # Подтверждение — клиническое решение, оно обязано нести оценку тяжести:
    # у пациента severity всегда NULL, и «подтверждено без тяжести» оставило бы
    # очередь с пустой колонкой. Идемпотентный повтор уже принятого решения
    # тяжести не требует — он ничего не меняет.
    if status == "confirmed" and severity is None:
        raise Problem(422, "SEVERITY_REQUIRED", "Для подтверждения нужна оценка тяжести")

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
    return (await event_dicts(db, [ev], {ev.patient_id: patient.fio if patient else None}))[0]
