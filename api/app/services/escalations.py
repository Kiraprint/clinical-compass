"""Очередь открытых эскалаций (docs/16 §3, docs/04 §3).

Общая очередь для врачей и администратора: сигнал чек-ина не теряется, если у
пациента ещё нет roster-врача. Чтение аудируется (152-ФЗ, спецкатегории ПДн).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CheckinSession, Escalation, User

from .audit import record as audit_record


def list_open(db: Session, actor_id: uuid.UUID, ip: str | None = None) -> dict:
    """Открытые (resolved_at IS NULL) эскалации, новые сверху."""
    rows = db.execute(
        select(Escalation, User.fio, CheckinSession.safety)
        .join(User, User.id == Escalation.patient_id)
        .outerjoin(CheckinSession, CheckinSession.id == Escalation.checkin_session_id)
        .where(Escalation.resolved_at.is_(None))
        .order_by(Escalation.created_at.desc().nulls_last(), Escalation.id.desc())
    ).all()
    items = [
        {
            "id": str(e.id),
            "patient_id": str(e.patient_id),
            "patient_fio": fio,
            "kind": e.kind,
            "safety": safety,
            "checkin_session_id": str(e.checkin_session_id) if e.checkin_session_id else None,
            "notified_at": e.notified_clinician_at.isoformat() if e.notified_clinician_at else None,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e, fio, safety in rows
    ]
    audit_record(db, actor_id, "READ_ESCALATIONS", "escalation", ip=ip)
    db.commit()
    return {"items": items}
