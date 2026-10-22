"""Назначения препаратов в карточке (FR-23).

Одна таблица patient_medications на два источника: импорт ЕМИАС ('emias',
read-only) и назначения врача ('clinician'). Доступ — roster + активное
согласие пациента (152-ФЗ), как у остальных клиницистских разделов карты.
PHI (title/dosage/notes) лежит в EncryptedText; в аудит пишется только факт
действия и id пациента — без текста назначения.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import Problem
from app.models import PatientMedication

from .audit import record as audit_record
from .clinician import MOSCOW, require_patient_consent, require_roster

ALL_STATUSES = ("active", "completed", "stopped", "unknown")
# 'unknown' — статус импорта ЕМИАС, врач его выставить не может.
CLINICIAN_STATUSES = ("active", "completed", "stopped")
EDITABLE_FIELDS = ("dosage", "status", "ended_on", "notes")

def _today() -> date:
    return datetime.now(MOSCOW).date()

def _require_access(db: Session, clinician_id, patient_id) -> None:
    require_roster(db, clinician_id, patient_id)
    require_patient_consent(db, patient_id)

def _get(db: Session, patient_id, med_id) -> PatientMedication:
    med = db.get(PatientMedication, med_id)
    # IDOR: назначение другого пациента неотличимо от несуществующего.
    if med is None or med.patient_id != patient_id:
        raise Problem(404, "NOT_FOUND", "Назначение не найдено")
    return med

def _get_mutable(db: Session, clinician_id, patient_id, med_id) -> PatientMedication:
    """Строка, которую врач вправе менять: своя локальная.

    Импорт ЕМИАС read-only → 409 MEDICATION_IMPORTED; локальное назначение
    другого врача → 403 NOT_OWNER (карточка может быть закреплена за несколькими).
    """
    med = _get(db, patient_id, med_id)
    if med.source != "clinician":
        raise Problem(409, "MEDICATION_IMPORTED", "Назначение импортировано из ЕМИАС — только чтение")
    if med.clinician_id != clinician_id:
        raise Problem(403, "NOT_OWNER", "Назначение оформлено другим врачом — только чтение")
    return med

def _validate(
    title: str, status: str, started_on: date | None, ended_on: date | None,
) -> tuple[str, date | None, date | None]:
    title = (title or "").strip()
    if not title:
        raise Problem(422, "VALIDATION", "Название препарата обязательно")
    if status not in CLINICIAN_STATUSES:
        raise Problem(422, "VALIDATION", f"Статус должен быть одним из: {', '.join(CLINICIAN_STATUSES)}")
    if started_on is not None and started_on > _today():
        raise Problem(422, "VALIDATION", "Дата начала приёма не может быть в будущем")
    if started_on is not None and ended_on is not None and ended_on < started_on:
        raise Problem(422, "VALIDATION", "Дата окончания раньше даты начала")
    return title, started_on, ended_on

def medication_dict(med: PatientMedication, actor_id=None) -> dict:
    """JSON для UI. editable — можно ли править этому врачу (своя локальная строка)."""
    return {
        "id": str(med.id),
        "title": med.title,
        "dosage": med.dosage,
        "started_on": med.started_on.isoformat() if med.started_on else None,
        "ended_on": med.ended_on.isoformat() if med.ended_on else None,
        "status": med.status,
        "notes": med.notes,
        "source": med.source,
        "clinician_id": str(med.clinician_id) if med.clinician_id else None,
        "editable": med.source == "clinician" and actor_id is not None and med.clinician_id == actor_id,
        "created_at": med.created_at.isoformat(),
    }

def list_medications(
    db: Session, clinician_id, patient_id, status: str | None = None, active_only: bool = False,
) -> list[dict]:
    """Назначения пациента (новые первыми). Roster + consent."""
    _require_access(db, clinician_id, patient_id)
    if status is not None and status not in ALL_STATUSES:
        raise Problem(422, "VALIDATION", f"Неизвестный статус: {status}")
    query = select(PatientMedication).where(PatientMedication.patient_id == patient_id)
    if active_only:
        query = query.where(PatientMedication.status == "active")
    elif status is not None:
        query = query.where(PatientMedication.status == status)
    query = query.order_by(
        PatientMedication.started_on.desc().nullslast(),
        PatientMedication.created_at.desc(),
        PatientMedication.id.desc(),
    )
    return [medication_dict(m, clinician_id) for m in db.scalars(query).all()]

def create_medication(
    db: Session, clinician_id, patient_id, *,
    title: str, dosage: str | None = None, started_on: date | None = None,
    ended_on: date | None = None, status: str = "active", notes: str | None = None,
    ip: str | None = None,
) -> dict:
    """Назначение препарата врачом. Roster + consent + аудит PRESCRIBE_MEDICATION."""
    _require_access(db, clinician_id, patient_id)
    title, started_on, ended_on = _validate(title, status, started_on, ended_on)
    med = PatientMedication(
        patient_id=patient_id, clinician_id=clinician_id, title=title,
        dosage=(dosage or "").strip() or None, started_on=started_on, ended_on=ended_on,
        status=status, notes=(notes or "").strip() or None, source="clinician",
    )
    db.add(med)
    db.flush()
    audit_record(db, clinician_id, "PRESCRIBE_MEDICATION", "patient", str(patient_id), ip=ip)
    db.commit()
    return medication_dict(med, clinician_id)

def update_medication(
    db: Session, clinician_id, patient_id, med_id, changes: dict, ip: str | None = None,
) -> dict:
    """Правка дозировки/статуса/даты окончания/заметки. Аудит UPDATE_MEDICATION."""
    _require_access(db, clinician_id, patient_id)
    med = _get_mutable(db, clinician_id, patient_id, med_id)
    unknown = set(changes) - set(EDITABLE_FIELDS)
    if unknown:
        raise Problem(422, "VALIDATION", f"Поля не редактируются: {', '.join(sorted(unknown))}")
    status = changes.get("status", med.status)
    started_on = med.started_on
    ended_on = changes.get("ended_on", med.ended_on)
    _validate(med.title, status, started_on, ended_on)
    if "dosage" in changes:
        med.dosage = (changes["dosage"] or "").strip() or None
    if "notes" in changes:
        med.notes = (changes["notes"] or "").strip() or None
    med.status = status
    med.ended_on = ended_on
    audit_record(db, clinician_id, "UPDATE_MEDICATION", "patient", str(patient_id), ip=ip)
    db.commit()
    return medication_dict(med, clinician_id)

def stop_medication(
    db: Session, clinician_id, patient_id, med_id, ended_on: date | None = None,
    ip: str | None = None,
) -> dict:
    """Остановка приёма: status='stopped', ended_on по умолчанию — сегодня."""
    _require_access(db, clinician_id, patient_id)
    med = _get_mutable(db, clinician_id, patient_id, med_id)
    ended = ended_on or _today()
    if med.started_on is not None and ended < med.started_on:
        raise Problem(422, "VALIDATION", "Дата окончания раньше даты начала")
    med.status = "stopped"
    med.ended_on = ended
    audit_record(db, clinician_id, "STOP_MEDICATION", "patient", str(patient_id), ip=ip)
    db.commit()
    return medication_dict(med, clinician_id)

def delete_medication(
    db: Session, clinician_id, patient_id, med_id, ip: str | None = None,
) -> None:
    """Удаление ошибочного назначения. Только своя локальная строка."""
    _require_access(db, clinician_id, patient_id)
    med = _get_mutable(db, clinician_id, patient_id, med_id)
    db.delete(med)
    audit_record(db, clinician_id, "DELETE_MEDICATION", "patient", str(patient_id), ip=ip)
    db.commit()
